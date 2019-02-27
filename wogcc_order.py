# (c) 2019 dwt | tds LLC
# dependencies:
#   beautifulsoup4
#   request
#   html5lib

import requests
from collections import namedtuple
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from time import sleep
from random import uniform
from sys import argv, stderr, exit

USER_AGENT = UserAgent().firefox # impersonate a (recent) Firefox browser
DELAY_MIN = 0.78 # minimum between-requests delay (seconds)
DELAY_MAX = 2.1 # minimum between-requests delay (seconds)

BASE_URL = 'http://pipeline.wyo.gov'
FIND_BY_KW_YEAR_URL = f'{BASE_URL}/FindOrderYearKeyword.cfm'
FIND_BY_KW_YEAR_ACTION_URL = f'{BASE_URL}/findorderssYear.cfm'

EVIL_BLOCKER_SIG = b'window["bobcmn"]'

EXPECTED_HEADER_ROW = [
    'Cause',
    'Order',
    'Docket',
    'Heard By',
    'Field',
    'Applicant',
    'Formation',
    'Matter',
    'Status'
]

EXPECTED_HEADER_COLUMN = [
    'Disposition',
    'Hearing Date',
    'Applicant',
    'Field',
    'Formation',
    'Matter',
    'Filed Under',
    'Application'
]

SearchResult = namedtuple('SearchResult', [
    'docket',
    'cardurl',
    'heard_by',
    'field',
    'applicant',
    'formation',
    'matter',
    'status'
])

OrderCard = namedtuple('OrderCard', [
    'disposition',
    'hearing_date',
    'applicant',
    'field',
    'formation',
    'matter',
    'filed_under'
])

def delay():
    sleep(uniform(DELAY_MIN, DELAY_MAX))

def orders(session, field, year):
    # hit search page to get our session cookies...
    res = session.get(FIND_BY_KW_YEAR_URL)
    res.raise_for_status()

    # issue search by keyword and year
    delay()
    session.headers.update({'Referer': FIND_BY_KW_YEAR_URL})
    res = session.post(FIND_BY_KW_YEAR_ACTION_URL, {
        'oops': 1,
        'WELLFL': field,
        'cYear': year
    })
    res.raise_for_status()

    ref = FIND_BY_KW_YEAR_ACTION_URL
    while True:
        try:
            for rec in order_page_records(res.content):
                if type(rec) == SearchResult:
                    yield (rec, ref)
                elif rec is None:
                    # no more pages
                    return
                else:
                    # follow next page link
                    delay()
                    print(f'FOLLOWING LINK: {rec}', file=stderr)
                    session.headers.update({'Referer': ref})
                    res = session.get(f'{BASE_URL}{rec}')
                    res.raise_for_status()
                    ref = rec
        except Exception as e:
            print(f'failure parsing result page (see contents.log):\n{e}',
                    file=stderr)
            if EVIL_BLOCKER_SIG in res.content:
                print('F5 anti-scraping block page likely encountered; '
                        'consider waiting a bit and trying again',
                        file=stderr)
            with open('contents.log', 'wb') as f:
                f.write(res.content)
            raise

def order_page_records(content):
    # the 'built-in' html.parser is not able to correctly handle the
    #   results page, so use html5lib
    parsed = BeautifulSoup(content, 'html5lib')
    # second table on the page should be the results table
    tbl = parsed.find_all('table')[1]
    for i, row in enumerate(tbl.find_all('tr')):
        if i == 0:
            # <tr><td>...</td><td><a ...><a href="xxx">...</td></tr>
            next_link = row.find_all('td')[1].find_all(recursive=False)[1]
            if next_link.name == 'a':
                nexturl = next_link['href']
            # the last page just has a blank image in place of the "next" link
            else:
                nexturl = None
        elif i == 1:
            hdr = [td.contents[0] for td in row.find_all('td')]
            if hdr[2:] != EXPECTED_HEADER_ROW:
                raise ValueError('invalid results page')
        else:
            # ALL result sets have a row of blank images
            if row.td.has_attr('colspan'):
                continue
            strongs = [td.strong for td in row.find_all('td')]
            _, _, _, _, d, h, fld, a, frm, m, s = [
                    s.contents[0] if s.contents else ''
                    for s in strongs]
            yield SearchResult(
                d.contents[0].strip(),
                d['href'],
                h.strip(),
                fld.strip(),
                a.strip(),
                frm.strip(),
                m.strip(),
                s.strip()
            )

    yield nexturl

def order_card(session, search_result, referer):
    session.headers.update({'Referer': referer})
    delay()
    res = session.get(f'{BASE_URL}{search_result.cardurl}')
    res.raise_for_status()
    try:
        card, app_url, ex_url = order_card_record(res.content)
        return card, app_url, ex_url
    except Exception as e:
        print(f'failure parsing cardfile page (see contents.log):\n{e}',
                file=stderr)
        if EVIL_BLOCKER_SIG in res.content:
            print('F5 anti-scraping block page likely encountered; '
                    'consider waiting a bit and trying again',
                    file=stderr)
        with open('contents.log', 'wb') as f:
            f.write(res.content)
        raise

def order_card_record(content):
    parsed = BeautifulSoup(content, 'html5lib')
    tbl = parsed.table # first and only table
    rows = tbl.find_all('tr')[1:]
    hdr = [tr.td.strong.contents[0] for tr in rows]
    if hdr != EXPECTED_HEADER_COLUMN:
        raise ValueError('invalid cardfile page')
    meta = list()
    for tr in rows[:-1]:
        tds = tr.find_all('td')
        if len(tds) < 2:
            meta.append('')
        else:
            meta.append(tds[1].p.strong.contents[0].strip())
    pdfs = rows[-1].find_all('td')[1:]
    if len(pdfs) == 2:
        return OrderCard(*meta), pdfs[0].a['href'], pdfs[1].a['href']
    elif len(pdfs) == 1:
        return OrderCard(*meta), pdfs[0].a['href'], None
    else:
        return OrderCard(*meta), None, None

def save_order_pdf(session, url, referer, dst):
    session.headers.update({'Referer': referer})
    delay()
    res = session.get(url)
    res.raise_for_status()
    with open(dst, 'wb') as f:
        f.write(res.content)

def main(argv):
    if len(argv) != 3:
        print(f'Usage: {argv[0] if argv else wogcc_order} field year',
                file=stderr)
        return 0

    try:
        # use a persistent session to accumulate cookies;
        #   we'll handle Referer headers explicitly
        with requests.Session() as s:
            s.headers.update({'User-Agent': USER_AGENT})
            for o, ref in orders(s, argv[1], argv[2]):
                print(o)
                card, ap, ex = order_card(s, o, ref)
                print(card)
                if ap:
                    ap_file = f'{o.docket}_ap.pdf'
                    print(ap_file)
                    save_order_pdf(s, ap, f'{BASE_URL}{o.cardurl}', ap_file)
                if ex:
                    ex_file = f'{o.docket}_ex.pdf'
                    print(ex_file)
                    save_order_pdf(s, ex,
                            f'{BASE_URL}{o.cardurl}', ex_file)

    except requests.exceptions.HTTPError as e:
        print(f'Bad status from HTTP request:\n{e}', file=stderr)
        return 1
    except:
        print(f'Unable to complete scraping', file=stderr)
        return 1

    return 0

if __name__ == '__main__':
    exit(main(argv))
