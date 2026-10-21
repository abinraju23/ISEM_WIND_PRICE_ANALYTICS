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
    candidates = [naive.replace(tzinfo=DUBLIN, fold=fold) for fold in (0, 1)]
    valid = [candidate for candidate in candidates
             if candidate.astimezone(timezone.utc).astimezone(DUBLIN).replace(tzinfo=None)
             == naive]
    if not valid:
        return None, 'nonexistent_clock_forward'
    if len(valid) > 1 and valid[0].utcoffset() != valid[1].utcoffset():
        return None, 'ambiguous_clock_back'
    return valid[0].astimezone(timezone.utc), None


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


def multiple_regression(rows):
    """Fit price on wind, demand and monthly gas price using standardized normal equations."""
    if len(rows) <= 4:
        raise ValueError('need more than four joined periods to fit three predictors')

    names = ('wind', 'demand', 'gas')
    columns = [list(column) for column in zip(*(row[:3] for row in rows))]
    prices = [row[3] for row in rows]
    means = [sum(column) / len(column) for column in columns]
    scales = [math.sqrt(sum((value - mean) ** 2 for value in column) / len(column))
              for column, mean in zip(columns, means)]
    for name, scale in zip(names, scales):
        if scale == 0:
            raise ValueError('%s does not vary across the joined periods' % name)

    price_mean = sum(prices) / len(prices)
    price_scale = math.sqrt(sum((price - price_mean) ** 2 for price in prices) / len(prices))
    if price_scale == 0:
        raise ValueError('price does not vary across the joined periods')

    standardized = [
        [(value - mean) / scale for value in column]
        for column, mean, scale in zip(columns, means, scales)
    ]
    standardized_prices = [(price - price_mean) / price_scale for price in prices]
    gram = [
        [sum(x * y for x, y in zip(left, right)) for right in standardized]
        for left in standardized
    ]
    rhs = [sum(x * y for x, y in zip(column, standardized_prices))
           for column in standardized]

    size = len(gram)
    matrix = [gram[row][:] + [rhs[row]] for row in range(size)]
    threshold = len(rows) * 1e-12
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) <= threshold:
            raise ValueError('wind, demand and gas are too collinear to separate reliably')
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        pivot_value = matrix[column][column]
        for index in range(column, size + 1):
            matrix[column][index] /= pivot_value
        for row in range(size):
            if row == column:
                continue
            factor = matrix[row][column]
            for index in range(column, size + 1):
                matrix[row][index] -= factor * matrix[column][index]

    standardized_coefficients = [matrix[row][-1] for row in range(size)]
    coefficients = [
        coefficient * price_scale / scale
        for coefficient, scale in zip(standardized_coefficients, scales)
    ]
    intercept = price_mean - sum(coefficient * mean
                                 for coefficient, mean in zip(coefficients, means))
    predictions = [
        intercept + sum(coefficient * value
                        for coefficient, value in zip(coefficients, row[:3]))
        for row in rows
    ]
    residual_sum = sum((actual - predicted) ** 2
                       for actual, predicted in zip(prices, predictions))
    total_sum = sum((actual - price_mean) ** 2 for actual in prices)
    return intercept, coefficients, 1.0 - residual_sum / total_sum


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
        if 'gas_price_monthly' not in tables:
            sys.exit('table gas_price_monthly missing — run load_gas_prices.py first')
        wind_rows = conn.execute(
            'SELECT effective_time, wind_mw, demand_mw FROM system_15min '
            'WHERE wind_mw IS NOT NULL OR demand_mw IS NOT NULL '
            'ORDER BY effective_time').fetchall()
        price_rows = conn.execute(
            'SELECT utc_time, eur_per_mwh FROM dam_price_30min ORDER BY utc_time').fetchall()
        gas_rows = conn.execute(
            'SELECT month, usd_per_mmbtu FROM gas_price_monthly ORDER BY month').fetchall()
    finally:
        conn.close()

    counts = defaultdict(int)

    # Local -> UTC, excluding what cannot be placed on the clock.
    wind_quarters = {}
    demand_quarters = {}
    for text, wind, demand in wind_rows:
        naive = datetime.strptime(text, '%Y-%m-%d %H:%M:%S')
        utc, reason = local_to_utc(naive)
        if utc is None:
            counts[reason] += 1
            continue
        if wind is not None:
            wind_quarters[utc] = wind
        if demand is not None:
            demand_quarters[utc] = demand
    counts['wind_quarters_placed'] = len(wind_quarters)
    counts['demand_quarters_placed'] = len(demand_quarters)

    # Average wind up to half-hours, requiring both quarters.
    halves = {}
    for utc, value in wind_quarters.items():
        if utc.minute in (0, 30):
            mate = utc + timedelta(minutes=15)
            if mate in wind_quarters:
                halves[utc] = (value + wind_quarters[mate]) / 2.0
            else:
                counts['halfhour_dropped_missing_quarter'] += 1
    counts['wind_halfhours'] = len(halves)

    demand_halves = {}
    for utc, value in demand_quarters.items():
        if utc.minute in (0, 30):
            mate = utc + timedelta(minutes=15)
            if mate in demand_quarters:
                demand_halves[utc] = (value + demand_quarters[mate]) / 2.0
            else:
                counts['demand_halfhour_dropped_missing_quarter'] += 1
    counts['demand_halfhours'] = len(demand_halves)

    prices = {datetime.strptime(t, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc): p
              for t, p in price_rows}
    counts['price_halfhours'] = len(prices)
    gas_prices = dict(gas_rows)

    wind_price_times = set(halves) & set(prices)
    pairs = sorted((halves[k], prices[k]) for k in wind_price_times)
    counts['joined'] = len(pairs)
    model_rows = []
    for timestamp in sorted(wind_price_times & set(demand_halves)):
        month = timestamp.astimezone(DUBLIN).strftime('%Y-%m')
        if month not in gas_prices:
            counts['half_hours_without_monthly_gas'] += 1
            continue
        model_rows.append((halves[timestamp], demand_halves[timestamp],
                           gas_prices[month], prices[timestamp]))
    counts['adjusted_model_joined'] = len(model_rows)

    print('Join on UTC half-hours')
    for key in ('wind_quarters_placed', 'demand_quarters_placed',
                'ambiguous_clock_back', 'nonexistent_clock_forward',
                'halfhour_dropped_missing_quarter',
                'demand_halfhour_dropped_missing_quarter', 'wind_halfhours',
                'demand_halfhours', 'price_halfhours', 'joined',
                'half_hours_without_monthly_gas', 'adjusted_model_joined'):
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

    print('\nLinear price model (wind + demand + monthly European gas proxy)')
    try:
        intercept, coefficients, r_squared = multiple_regression(model_rows)
    except ValueError as exc:
        print('  Not calculated: %s' % exc)
    else:
        print('  Wind:   %+.2f EUR/MWh per 1,000 MW, holding demand and gas constant'
              % (coefficients[0] * 1000.0))
        print('  Demand: %+.2f EUR/MWh per 1,000 MW, holding wind and gas constant'
              % (coefficients[1] * 1000.0))
        print('  Gas:    %+.2f EUR/MWh per $1/MMBtu, holding wind and demand constant'
              % coefficients[2])
        print('  R-squared: %.3f over %d matched half-hours'
              % (r_squared, len(model_rows)))
        print('  Intercept: %.2f EUR/MWh' % intercept)

    span = '%s to %s UTC' % (min(set(halves) & set(prices)), max(set(halves) & set(prices)))
    subtitle = ('%d half-hours, %s. Wind: EirGrid Smart Grid Dashboard (15-min, averaged to '
                '30-min). Price: SEMOpx day-ahead.' % (len(pairs), span))
    os.makedirs(REPORTS, exist_ok=True)
    svg_scatter(pairs, trend, r, CHART, subtitle)
    print('\nWrote %s' % CHART)
    print('\nThe chart is an unadjusted association. The linear model adjusts for demand and a '
          'monthly European gas-price proxy, but it is not causal and not a forecast. It does '
          'not account for plant availability, interconnector flows, or time-dependent '
          'uncertainty; the monthly gas proxy cannot explain half-hourly gas-market movements.')


if __name__ == '__main__':
    main()
