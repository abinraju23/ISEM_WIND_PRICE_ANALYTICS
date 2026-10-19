# -*- coding: utf-8 -*-
"""Join wind to day-ahead price in UTC, measure the relationship, draw the chart.

Standard library only. No pandas, no matplotlib — the SVG is written by hand, which keeps the
repo dependency-free and renders directly in a GitHub README.

THE TWO DECISIONS THAT CHANGE THE ANSWER, both stated in the output and both belonging in the
README rather than buried here.

1. RESOLUTION. Wind is published every 15 minutes, day-ahead price every 30. Averaging wind up
   to half-hours and forward-filling price down to quarter-hours give different answers.
   This averages wind UP, and requires BOTH quarter-hours to be present before it emits a
   half-hour. Forward-filling price would invent price observations that were never published,
   which is the worse of the two errors.

2. TIME BASE. EirGrid publishes naive LOCAL time; SEMOpx publishes UTC (see load_prices.py).
   Joining the raw strings is wrong by an hour for most of the year. This converts the EirGrid
   side to UTC and joins there, which also removes the clock-change ambiguity from the join
   itself rather than papering over it:
     - On the October clock change the local hour 01:00-01:59 occurs TWICE. A naive local
       timestamp from EirGrid cannot say which, so those periods are EXCLUDED and counted.
     - On the March change that local hour does not exist. Any such row is a feed error and is
       also excluded and counted.

Usage:
    python join_and_chart.py
"""
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    DUBLIN = ZoneInfo('Europe/Dublin')
except ImportError:
    sys.exit('this needs zoneinfo (Python 3.9+) to convert local time to UTC correctly')

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'isem.db')
REPORTS = os.path.join(HERE, 'reports')
CHART = os.path.join(HERE, 'wind_vs_price.svg')

AXIS = '#888888'
POINT = '#3b82f6'
TREND = '#ef4444'


# ---------------------------------------------------------------- time handling

def local_to_utc(naive):
    """Convert a naive Europe/Dublin datetime to UTC.

    Returns (utc_datetime, None) when unambiguous, or (None, reason) when it is not.
    """
    first = naive.replace(tzinfo=DUBLIN, fold=0)
    second = naive.replace(tzinfo=DUBLIN, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None, 'ambiguous_clock_back'
    utc = first.astimezone(timezone.utc)
    # A local time that does not exist (spring forward) does not round-trip.
    if utc.astimezone(DUBLIN).replace(tzinfo=None) != naive:
        return None, 'nonexistent_clock_forward'
    return utc, None


# ---------------------------------------------------------------- statistics

def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def deciles(pairs):
    """Mean price per wind decile. More robust than r, and easier to read off a chart."""
    ordered = sorted(pairs)
    size = len(ordered)
    out = []
    for i in range(10):
        lo, hi = size * i // 10, size * (i + 1) // 10
        chunk = ordered[lo:hi]
        if not chunk:
            continue
        out.append((sum(w for w, _ in chunk) / len(chunk),
                    sum(p for _, p in chunk) / len(chunk),
                    len(chunk)))
    return out


# ---------------------------------------------------------------- chart

def nice_step(span, target=6):
    if span <= 0:
        return 1
    rough = span / float(target)
    power = 10 ** math.floor(math.log10(rough))
    for mult in (1, 2, 5, 10):
        if rough <= mult * power:
            return mult * power
    return 10 * power


def svg_scatter(pairs, trend, r, path, subtitle):
    W, H = 760, 470
    L, R, T, B = 78, 24, 34, 60
    pw, ph = W - L - R, H - T - B

    xs = [w for w, _ in pairs]
    ys = [p for _, p in pairs]
    xmin, xmax = 0.0, max(xs) * 1.02
    ymin, ymax = min(ys), max(ys)
    pad = (ymax - ymin) * 0.06 or 1.0
    ymin, ymax = ymin - pad, ymax + pad

    def px(w):
        return L + (w - xmin) / (xmax - xmin) * pw

    def py(p):
        return T + ph - (p - ymin) / (ymax - ymin) * ph

    parts = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" '
             'height="%d" font-family="system-ui, -apple-system, Segoe UI, sans-serif">'
             % (W, H, W, H)]
    parts.append('<title>All-island wind output against SEM day-ahead price</title>')

    # Gridlines and ticks
    xstep = nice_step(xmax - xmin)
    tick = math.ceil(xmin / xstep) * xstep
    while tick <= xmax:
        x = px(tick)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="0.5" opacity="0.3"/>' % (x, T, x, T + ph, AXIS))
        parts.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" '
                     'text-anchor="middle">%s</text>' % (x, T + ph + 18, AXIS, int(tick)))
        tick += xstep

    ystep = nice_step(ymax - ymin)
    tick = math.ceil(ymin / ystep) * ystep
    while tick <= ymax:
        y = py(tick)
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="0.5" opacity="0.3"/>' % (L, y, L + pw, y, AXIS))
        parts.append('<text x="%.1f" y="%.1f" fill="%s" font-size="11" '
                     'text-anchor="end">%s</text>' % (L - 8, y + 4, AXIS, int(tick)))
        tick += ystep

    # Zero line, because negative prices are a real and interesting outcome
    if ymin < 0 < ymax:
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                     'stroke-width="1" opacity="0.7"/>' % (L, py(0), L + pw, py(0), AXIS))

    # Axes
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1"/>' % (L, T + ph, L + pw, T + ph, AXIS))
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 'stroke-width="1"/>' % (L, T, L, T + ph, AXIS))

    # Points
    for w, p in pairs:
        parts.append('<circle cx="%.1f" cy="%.1f" r="2.4" fill="%s" opacity="0.32"/>'
                     % (px(w), py(p), POINT))

    # Decile trend
    if len(trend) > 1:
        path_pts = ' '.join('%.1f,%.1f' % (px(w), py(p)) for w, p, _ in trend)
        parts.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2.5"/>'
                     % (path_pts, TREND))
        for w, p, _ in trend:
            parts.append('<circle cx="%.1f" cy="%.1f" r="3.6" fill="%s"/>'
                         % (px(w), py(p), TREND))

    # Labels
    parts.append('<text x="%.1f" y="%.1f" fill="%s" font-size="12" text-anchor="middle">'
                 'All-island wind output (MW), half-hourly</text>'
                 % (L + pw / 2.0, H - 16, AXIS))
    parts.append('<text x="16" y="%.1f" fill="%s" font-size="12" text-anchor="middle" '
                 'transform="rotate(-90 16 %.1f)">Day-ahead price (EUR/MWh)</text>'
                 % (T + ph / 2.0, AXIS, T + ph / 2.0))
    head = 'Wind output against day-ahead price'
    if r is not None:
        head += '   (Pearson r = %.2f)' % r
    parts.append('<text x="%d" y="20" fill="%s" font-size="13" font-weight="600">%s</text>'
                 % (L, AXIS, head))
    parts.append('<text x="%d" y="%d" fill="%s" font-size="10.5" opacity="0.85">%s</text>'
                 % (L, H - 34, AXIS, subtitle))
    parts.append('<text x="%.1f" y="20" fill="%s" font-size="11" text-anchor="end">'
                 'red line: mean price per wind decile</text>' % (L + pw, TREND))
    parts.append('</svg>')

    with open(path, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(parts))


# ---------------------------------------------------------------- main

def main():
    if not os.path.exists(DB):
        sys.exit('no %s — run fetch_eirgrid.py then build_dataset.py first' % DB)

    conn = sqlite3.connect(DB)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for needed in ('system_15min', 'dam_price_30min'):
            if needed not in tables:
                sys.exit('table %s missing — run %s first'
                         % (needed, 'build_dataset.py' if needed == 'system_15min'
                            else 'load_prices.py'))
        wind_rows = conn.execute(
            'SELECT effective_time, wind_mw FROM system_15min '
            'WHERE wind_mw IS NOT NULL ORDER BY effective_time').fetchall()
        price_rows = conn.execute(
            'SELECT utc_time, eur_per_mwh FROM dam_price_30min ORDER BY utc_time').fetchall()
    finally:
        conn.close()

    counts = defaultdict(int)

    # Local -> UTC, excluding what cannot be placed on the clock.
    quarters = {}
    for text, value in wind_rows:
        naive = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
        utc, reason = local_to_utc(naive)
        if utc is None:
            counts[reason] += 1
            continue
        quarters[utc] = value
    counts['wind_quarters_placed'] = len(quarters)

    # Average wind up to half-hours, requiring both quarters.
    halves = {}
    for utc, value in quarters.items():
        if utc.minute in (0, 30):
            mate = utc + timedelta(minutes=15)
            if mate in quarters:
                halves[utc] = (value + quarters[mate]) / 2.0
            else:
                counts['halfhour_dropped_missing_quarter'] += 1
    counts['wind_halfhours'] = len(halves)

    prices = {datetime.strptime(t, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc): p
              for t, p in price_rows}
    counts['price_halfhours'] = len(prices)

    pairs = sorted((halves[k], prices[k]) for k in set(halves) & set(prices))
    counts['joined'] = len(pairs)

    print('Join on UTC half-hours')
    for key in ('wind_quarters_placed', 'ambiguous_clock_back', 'nonexistent_clock_forward',
                'halfhour_dropped_missing_quarter', 'wind_halfhours', 'price_halfhours',
                'joined'):
        print('  %-34s %d' % (key, counts[key]))

    if counts['ambiguous_clock_back']:
        print('  -> the clock-back hour was excluded rather than guessed. Expect 4 quarter-'
              'hours per field per year.')

    if len(pairs) < 20:
        print('\nOnly %d joined periods. Export more delivery days from SEMOpx before reading '
              'anything into this.' % len(pairs))
        if not pairs:
            return

    xs = [w for w, _ in pairs]
    ys = [p for _, p in pairs]
    r = pearson(xs, ys)
    trend = deciles(pairs)

    print('\nPrice by wind decile (mean wind MW -> mean price EUR/MWh, n)')
    for w, p, n in trend:
        print('  %7.0f  ->  %8.2f   (%d)' % (w, p, n))

    if r is not None:
        print('\nPearson r = %.3f over %d half-hours' % (r, len(pairs)))
    if len(trend) > 1:
        print('Lowest-wind decile mean price  %.2f' % trend[0][1])
        print('Highest-wind decile mean price %.2f' % trend[-1][1])
        print('Difference                     %.2f EUR/MWh' % (trend[-1][1] - trend[0][1]))

    span = '%s to %s UTC' % (min(set(halves) & set(prices)), max(set(halves) & set(prices)))
    subtitle = ('%d half-hours, %s. Wind: EirGrid Smart Grid Dashboard (15-min, averaged to '
                '30-min). Price: SEMOpx day-ahead.' % (len(pairs), span))
    os.makedirs(REPORTS, exist_ok=True)
    svg_scatter(pairs, trend, r, CHART, subtitle)
    print('\nWrote %s' % CHART)
    print('\nWhat this does NOT establish: it is an association over one window in one season, '
          'not a causal estimate and not a forecast. Gas price, demand, interconnector flows '
          'and plant availability all move price and none of them is in this chart.')


if __name__ == '__main__':
    main()
