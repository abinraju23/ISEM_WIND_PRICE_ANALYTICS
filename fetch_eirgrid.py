# -*- coding: utf-8 -*-
"""Fetch wind and demand from EirGrid's Smart Grid Dashboard, one calendar month per call.

Standard library only: no pandas, no requests. Every response is cached to disk, so a re-run
costs nothing and the analysis is reproducible from the cache alone.

Usage:
    python fetch_eirgrid.py                 # the last 12 complete months
    python fetch_eirgrid.py 2025-10 2026-09 # an explicit inclusive range

Source: EirGrid Smart Grid Dashboard (https://www.smartgriddashboard.com/). The endpoint is the
dashboard's own and is not a documented public API, so this requests months sequentially with a
pause between them and caches everything rather than re-fetching.
"""
import calendar
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = 'https://www.smartgriddashboard.com/api/chart/'
REGION = 'ALL'            # 'ALL' all-island, 'ROI' Ireland, 'NI' Northern Ireland
AREAS = 'windactual,demandactual'
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'raw')
PAUSE_SECONDS = 2.0
TIMEOUT = 60

# The dashboard renders in a browser, and a bare urllib User-Agent is sometimes refused.
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ISEM-wind-price-analysis/1.0',
    'Accept': 'application/json, text/plain, */*',
}

MONTH_ABBR = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def month_bounds(year, month):
    """('01-Sep-2026', '30-Sep-2026') — the form the endpoint expects."""
    last = calendar.monthrange(year, month)[1]
    stamp = '%%02d-%s-%d' % (MONTH_ABBR[month], year)
    return stamp % 1, stamp % last


def month_url(year, month):
    date_from, date_to = month_bounds(year, month)
    return ('%s?region=%s&chartType=default&dateRange=month&dateFrom=%s&dateTo=%s&areas=%s'
            % (BASE, REGION, date_from, date_to, AREAS))


def cache_path(year, month):
    return os.path.join(CACHE, 'eirgrid_%d-%02d.json' % (year, month))


def fetch_month(year, month, force=False):
    """Return the parsed JSON for one month, from cache when present."""
    path = cache_path(year, month)
    if os.path.exists(path) and not force:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh), 'cache'

    url = month_url(year, month)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        raise RuntimeError('HTTP %s for %d-%02d\n%s' % (exc.code, year, month, url))
    except urllib.error.URLError as exc:
        raise RuntimeError('network error for %d-%02d: %s' % (year, month, exc.reason))

    data = json.loads(payload)
    if 'Rows' not in data:
        raise RuntimeError('unexpected payload for %d-%02d: top-level keys %s'
                           % (year, month, sorted(data)))

    os.makedirs(CACHE, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh)
    return data, 'network'


def months_back(n, today=None):
    """The n most recent COMPLETE calendar months, oldest first."""
    today = today or dt.date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(n):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append((year, month))
    return list(reversed(out))


def parse_arg_months(argv):
    def one(text):
        year, month = text.split('-')
        return int(year), int(month)

    start, end = one(argv[0]), one(argv[1])
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        year, month = cur
        cur = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def main(argv):
    if len(argv) == 2:
        wanted = parse_arg_months(argv)
    elif not argv:
        wanted = months_back(12)
    else:
        sys.exit('usage: python fetch_eirgrid.py [YYYY-MM YYYY-MM]')

    total = 0
    for i, (year, month) in enumerate(wanted):
        data, source = fetch_month(year, month)
        rows = len(data.get('Rows', []))
        total += rows
        print('%d-%02d  %6d rows  (%s)' % (year, month, rows, source))
        if source == 'network' and i < len(wanted) - 1:
            time.sleep(PAUSE_SECONDS)

    print('\n%d months, %d rows cached in %s' % (len(wanted), total, CACHE))


if __name__ == '__main__':
    main(sys.argv[1:])
