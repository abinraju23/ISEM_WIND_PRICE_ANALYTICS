# -*- coding: utf-8 -*-
"""Load SEMOpx day-ahead prices from manual exports into SQLite.

Standard library only.

HOW TO GET THE DATA
-------------------
https://www.semopx.com/market-data/market-results — pick "Day-Ahead", pick a delivery date,
switch from Chart to Table, and copy the table. One delivery day per file, or paste several
into one file; this loader does not care. Save under data/prices_raw/ with any name.

The table's columns are:

    Date  Time  EUR/MWh  NI Volume (MWh)  NI Net position (MWh)  ROI Volume (MWh)  ROI Net position (MWh)

Only Date, Time and EUR/MWh are used. The parser accepts tabs, commas or runs of spaces as the
separator, skips any header row, and tolerates a currency symbol or thousands separators.

THE TIMEZONE, WHICH IS THE THING MOST LIKELY TO BREAK THE JOIN
--------------------------------------------------------------
For delivery day Sunday 18 October 2026 the table's first row is timestamped **17-10 23:00**,
not 18-10 00:00. Irish local time in October before the clock change is UTC+1, so 23:00 UTC on
the 17th IS midnight local on the 18th. **SEMOpx labels these periods in UTC.** EirGrid's
dashboard, by contrast, publishes naive LOCAL time.

So the two sources are on different clocks, and joining them on the raw strings would be wrong
by an hour for most of the year and right only in winter. This loader stores UTC. `join.py`
converts the EirGrid side to UTC and joins there.

`ASSUME_UTC = False` flips it if that reading is ever wrong. The loader prints, per delivery
date, the local-time span the rows cover; under the correct assumption that span is
00:00 to 23:30 local. If you see 01:00 to 00:30, flip the flag.

Usage:
    python load_prices.py
"""
import glob
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    DUBLIN = ZoneInfo('Europe/Dublin')
except ImportError:                                     # Python < 3.9
    DUBLIN = None

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, 'data', 'prices_raw')
DB = os.path.join(HERE, 'isem.db')

ASSUME_UTC = True

SPLIT = re.compile(r'\t+|,|\s{2,}|\s+')
NUMBER = re.compile(r'^-?[\d,]*\.?\d+$')
DATE_FORMATS = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%d-%b-%Y', '%d %b %Y']


def parse_date(text):
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_line(line):
    """Return (naive_datetime, price) or None if the line is not a data row."""
    text = line.strip()
    parts = ([p.strip() for p in text.split('\t') if p.strip()]
             if '\t' in text else [p for p in SPLIT.split(text) if p])
    if len(parts) < 3:
        return None

    day = parse_date(parts[0])
    if day is None:
        return None

    try:
        clock = datetime.strptime(parts[1], '%H:%M').time()
    except ValueError:
        return None

    raw_price = parts[2].replace(',', '').replace('€', '').replace('EUR', '')
    if not NUMBER.match(raw_price.replace(',', '')):
        return None
    try:
        price = float(raw_price)
    except ValueError:
        return None

    return datetime.combine(day, clock), price


def main():
    files = sorted(glob.glob(os.path.join(RAW, '*')))
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        sys.exit('no price exports in %s\n'
                 'Export at least one delivery day from SEMOpx and save it there.' % RAW)

    rows = {}
    skipped = 0
    by_file = []
    for path in files:
        kept_here = 0
        with open(path, 'r', encoding='utf-8-sig', errors='replace') as fh:
            for line in fh:
                parsed = parse_line(line)
                if parsed is None:
                    skipped += 1
                    continue
                naive, price = parsed
                stamp = naive.replace(tzinfo=timezone.utc) if ASSUME_UTC else naive
                # A duplicate timestamp across files is a re-export of the same day, not an
                # error. Last write wins and the count is reported.
                if stamp in rows:
                    skipped += 1
                rows[stamp] = price
                kept_here += 1
        by_file.append((os.path.basename(path), kept_here))

    if not rows:
        sys.exit('parsed no price rows — check that the files contain the table, '
                 'not the page around it')

    stamps = sorted(rows)
    conn = sqlite3.connect(DB)
    try:
        conn.execute('DROP TABLE IF EXISTS dam_price_30min')
        conn.execute('''CREATE TABLE dam_price_30min (
                            utc_time    TEXT PRIMARY KEY,
                            eur_per_mwh REAL)''')
        conn.executemany('INSERT INTO dam_price_30min VALUES (?,?)',
                         [(s.strftime('%Y-%m-%d %H:%M:%S'), rows[s]) for s in stamps])
        conn.commit()
    finally:
        conn.close()

    print('Files read')
    for name, kept in by_file:
        print('  %-44s %4d rows' % (name, kept))
    print('\nPrice rows loaded   %d' % len(rows))
    print('Lines skipped       %d  (headers, blanks, re-exported duplicates)' % skipped)
    print('UTC span            %s to %s' % (stamps[0], stamps[-1]))
    prices = [rows[s] for s in stamps]
    print('EUR/MWh             min %.2f   max %.2f   mean %.2f'
          % (min(prices), max(prices), sum(prices) / len(prices)))

    if DUBLIN is not None and ASSUME_UTC:
        # The self-check described at the top of this file.
        spans = defaultdict(list)
        for stamp in stamps:
            local = stamp.astimezone(DUBLIN)
            spans[local.date()].append(local.strftime('%H:%M'))
        print('\nLocal-time span per delivery date (expect 00:00 to 23:30)')
        for day in sorted(spans)[:10]:
            times = spans[day]
            flag = '' if (min(times) == '00:00') else '   <-- check ASSUME_UTC'
            print('  %s  %s to %s  (%d periods)%s'
                  % (day, min(times), max(times), len(times), flag))
        if len(spans) > 10:
            print('  ... %d more dates' % (len(spans) - 10))
    elif DUBLIN is None:
        print('\nzoneinfo unavailable, so the local-time self-check was skipped.')


if __name__ == '__main__':
    main()
