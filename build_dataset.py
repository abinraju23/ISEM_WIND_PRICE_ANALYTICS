# -*- coding: utf-8 -*-
"""Validate the cached EirGrid months, pivot to one row per timestamp, load SQLite, report.

Standard library only. Every validation rule is named, counted and written to an exceptions
report; nothing is dropped silently.

Usage:
    python build_dataset.py            # reads data/raw/*.json, writes isem.db and reports/
"""
import calendar
import datetime as dt
import glob
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, 'data', 'raw')
REPORTS = os.path.join(HERE, 'reports')
DB = os.path.join(HERE, 'isem.db')

# EirGrid's response field names do NOT match the names you query with: you ask for
# 'windactual' and 'demandactual', and get these back. Mapped explicitly on purpose.
FIELD_MAP = {
    'WIND_ACTUAL': 'wind_mw',
    'SYSTEM_DEMAND': 'demand_mw',
    'SOLAR_ACTUAL': 'solar_mw',
}

# A loose upper sanity bound, not a real capacity figure. SET THIS from EirGrid's published
# installed wind capacity for the period before you quote anything from it; it exists to catch
# a decimal-point error in the feed, not to make a claim about the system.
WIND_CEILING_MW = 10000

TIME_FORMAT = '%d-%b-%Y %H:%M:%S'


def load_raw():
    """Yield (timestamp, field, value) for every row in every cached month."""
    files = sorted(glob.glob(os.path.join(RAW, 'eirgrid_*.json')))
    if not files:
        sys.exit('no cached months in %s — run fetch_eirgrid.py first' % RAW)
    for path in files:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        for row in data.get('Rows', []):
            yield row.get('EffectiveTime'), row.get('FieldName'), row.get('Value'), path


def main():
    counts = Counter()
    exceptions = []
    series = defaultdict(dict)          # timestamp -> {column: value}
    seen = Counter()                    # (timestamp, field) -> how many times

    for raw_time, field, value, path in load_raw():
        counts['rows_read'] += 1

        column = FIELD_MAP.get(field)
        if column is None:
            counts['rule_unknown_field'] += 1
            exceptions.append((raw_time, field, value, 'unknown field name', path))
            continue

        try:
            stamp = dt.datetime.strptime(raw_time, TIME_FORMAT)
        except (TypeError, ValueError):
            counts['rule_unparseable_timestamp'] += 1
            exceptions.append((raw_time, field, value, 'unparseable timestamp', path))
            continue

        # Rule 1: null means the period has not happened yet, or was not published.
        if value is None:
            counts['rule_null_value'] += 1
            exceptions.append((raw_time, field, value, 'null value', path))
            continue

        # Rule 2: a negative value is a sentinel, not a measurement. The solar series carries
        # -1 overnight. Generation cannot be negative, so this is missing data.
        if value < 0:
            counts['rule_negative_sentinel'] += 1
            exceptions.append((raw_time, field, value, 'negative value treated as missing', path))
            continue

        # Rule 3: implausible magnitude — a feed error, not weather.
        if column == 'wind_mw' and value > WIND_CEILING_MW:
            counts['rule_wind_above_ceiling'] += 1
            exceptions.append((raw_time, field, value, 'wind above sanity ceiling', path))
            continue

        # Rule 4: the same timestamp and field arriving twice. This is expected exactly once a
        # year, on the October clock change, when 01:00-02:00 local time occurs twice. It is a
        # known exception rather than an error, and it is recorded rather than resolved: the
        # feed publishes local time with no UTC offset, so the two hours are indistinguishable.
        key = (stamp, column)
        seen[key] += 1
        if seen[key] > 1:
            counts['rule_duplicate_timestamp'] += 1
            exceptions.append((raw_time, field, value, 'duplicate timestamp (clock change)', path))
            continue

        series[stamp][column] = value
        counts['rows_kept'] += 1

    stamps = sorted(series)
    if not stamps:
        sys.exit('nothing survived validation — check the cache and the rules above')

    # Rule 5: gaps. The feed is 15-minute, so consecutive timestamps should differ by 15 min.
    gaps = []
    for earlier, later in zip(stamps, stamps[1:]):
        delta = (later - earlier).total_seconds() / 60.0
        if delta > 15:
            gaps.append((earlier, later, delta))
    counts['rule_gaps_found'] = len(gaps)

    # Rule 6: coverage. The endpoint returns a ~30-day window, NOT a calendar month, so a
    # 31-day month comes back one day short and the missing day sits at the start. A gap check
    # alone will not catch it when two fetched windows happen to abut, so count the periods
    # actually present per month against the number the calendar says there should be.
    per_month = Counter(s.strftime('%Y-%m') for s in stamps)
    coverage = []
    for key in sorted(per_month):
        year, month = (int(p) for p in key.split('-'))
        expected = calendar.monthrange(year, month)[1] * 96
        got = per_month[key]
        coverage.append((key, got, expected))
        if got < expected:
            counts['rule_month_incomplete'] += 1

    # Only periods with BOTH wind and demand can be used for a share calculation.
    usable = [(s, series[s]['wind_mw'], series[s]['demand_mw']) for s in stamps
              if 'wind_mw' in series[s] and 'demand_mw' in series[s] and series[s]['demand_mw'] > 0]
    counts['periods_with_wind_and_demand'] = len(usable)

    os.makedirs(REPORTS, exist_ok=True)
    conn = sqlite3.connect(DB)
    try:
        conn.execute('DROP TABLE IF EXISTS system_15min')
        conn.execute('''CREATE TABLE system_15min (
                            effective_time TEXT PRIMARY KEY,
                            wind_mw        REAL,
                            demand_mw      REAL,
                            solar_mw       REAL,
                            wind_share     REAL)''')
        rows = []
        for stamp in stamps:
            rec = series[stamp]
            wind, demand = rec.get('wind_mw'), rec.get('demand_mw')
            share = (wind / demand) if (wind is not None and demand) else None
            rows.append((stamp.isoformat(sep=' '), wind, demand, rec.get('solar_mw'), share))
        conn.executemany('INSERT INTO system_15min VALUES (?,?,?,?,?)', rows)
        conn.commit()
    finally:
        conn.close()

    with open(os.path.join(REPORTS, 'exceptions.csv'), 'w', encoding='utf-8', newline='') as fh:
        fh.write('effective_time,field,value,rule,source_file\n')
        for raw_time, field, value, rule, path in exceptions:
            fh.write('%s,%s,%s,%s,%s\n' % (raw_time, field, value, rule, os.path.basename(path)))

    with open(os.path.join(REPORTS, 'gaps.csv'), 'w', encoding='utf-8', newline='') as fh:
        fh.write('gap_starts_after,next_period,minutes\n')
        for earlier, later, minutes in gaps:
            fh.write('%s,%s,%.0f\n' % (earlier, later, minutes))

    shares = [wind / demand for _, wind, demand in usable]
    print('Period      %s to %s' % (stamps[0], stamps[-1]))
    print('Rows        read %d, kept %d' % (counts['rows_read'], counts['rows_kept']))
    for rule in sorted(k for k in counts if k.startswith('rule_')):
        print('  %-32s %d' % (rule[5:], counts[rule]))
    print('Periods usable (wind + demand)  %d' % len(usable))
    print('\nCoverage by month (periods present / calendar expects)')
    for key, got, expected in coverage:
        flag = '' if got >= expected else '   <-- SHORT by %d periods' % (expected - got)
        print('  %s  %5d / %5d%s' % (key, got, expected, flag))
    if shares:
        total_wind = sum(w for _, w, _ in usable)
        total_demand = sum(d for _, _, d in usable)
        print('\nWind as a share of demand')
        print('  energy-weighted over the period   %.1f%%' % (100.0 * total_wind / total_demand))
        print('  mean of half-hourly shares        %.1f%%' % (100.0 * statistics.fmean(shares)))
        best = max(usable, key=lambda r: r[1] / r[2])
        worst = min(usable, key=lambda r: r[1] / r[2])
        print('  highest single period             %.1f%%  at %s' % (100.0 * best[1] / best[2], best[0]))
        print('  lowest single period              %.1f%%  at %s' % (100.0 * worst[1] / worst[2], worst[0]))
    print('\nWrote %s and %s' % (DB, REPORTS))
    print('NOTE: the energy-weighted share is the honest headline figure. The mean of period '
          'shares over-weights low-demand nights and will read higher.')


if __name__ == '__main__':
    main()
