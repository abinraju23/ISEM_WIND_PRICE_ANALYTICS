# ISEM Wind and Price Analysis

A small, dependency-free Python pipeline for comparing EirGrid all-island wind output with
SEMOpx day-ahead electricity prices. It validates and caches source data, joins the series
on UTC half-hours, reports summary statistics, and writes an SVG scatter chart. It can also
fit a descriptive linear model that adjusts for demand and a monthly European gas-price proxy.

The chart describes an association over the available observations. It is not a causal
estimate or a price forecast.

## Requirements

- Python 3.9 or later
- No third-party Python packages
- SEMOpx day-ahead price exports, downloaded manually
- Internet access only when fetching EirGrid months that are not already cached

## Run the pipeline

Run these commands from the project directory.

### 1. Fetch EirGrid data

```text
python fetch_eirgrid.py
```

By default this requests the 12 most recent complete calendar months. To fetch an inclusive
month range instead:

```text
python fetch_eirgrid.py 2025-10 2026-09
```

Responses are cached under `data/raw/`; cached months are reused on later runs. The endpoint
is used by EirGrid's Smart Grid Dashboard but is not a documented public API.

### 2. Build the system dataset

```text
python build_dataset.py
```

This reads the cached EirGrid JSON files, records validation exceptions and time gaps under
`reports/`, and rebuilds the `system_15min` table in `isem.db`. The script reports monthly
coverage and wind's share of demand. Review its coverage and exception output before using
the data.

### 3. Add SEMOpx day-ahead prices

Download the day-ahead market results from [SEMOpx market data](https://www.semopx.com/market-data/market-results).
For each delivery day, select the table view and save or combine the exported rows as files
under `data/prices_raw/`. Create that directory if it does not exist. The loader accepts
tab-, comma-, or whitespace-separated rows and skips headers it cannot parse.

The loader uses the date, time, and EUR/MWh columns. It assumes the SEMOpx timestamps are
UTC; its output includes a local-time self-check to help spot a timezone mismatch. Price
exports are not fetched automatically by this project.

### 4. Load the monthly gas-price proxy

```text
python load_gas_prices.py
```

This finds the latest monthly workbook linked from the [World Bank Commodity Markets page](https://www.worldbank.org/en/research/commodity-markets),
caches it under `data/raw/`, and loads the **Natural gas, Europe** series into
`gas_price_monthly` in the SQLite database. This is a monthly regional benchmark in
USD/MMBtu—not an Irish or half-hourly TTF price. Use `python load_gas_prices.py --refresh`
to fetch and load the latest workbook again.

### 5. Load prices and create the comparison

```text
python load_prices.py
python join_and_chart.py
```

`load_prices.py` rebuilds the `dam_price_30min` table from every file in `data/prices_raw/`.
`join_and_chart.py` converts EirGrid's naive Dublin-local timestamps to UTC, averages pairs
of complete 15-minute wind periods into half-hours, and joins those observations to prices.
It excludes ambiguous/nonexistent local timestamps around clock changes and reports the
exclusions. The chart remains an unadjusted wind-price comparison. A separate linear model
estimates the association with price for wind and demand (both in MW) and the monthly gas
proxy, using only half-hours with all four values available.

The final step prints Pearson correlation and mean price by wind decile, then writes
`wind_vs_price.svg` in the project directory. The model's coefficients are descriptive
adjusted associations; they do not establish that changing wind, demand, or gas prices causes
the reported change in electricity price.

## Data and interpretation notes

- The EirGrid dashboard publishes wind in 15-minute periods; SEMOpx day-ahead prices are
  half-hourly. Wind is averaged to half-hours only when both quarter-hours are present.
- EirGrid timestamps are local time without a UTC offset; SEMOpx timestamps are treated as
  UTC. The autumn clock-change hour cannot be disambiguated from EirGrid's timestamps and is
  excluded rather than guessed.
- The model uses demand from EirGrid and a **monthly European gas-price proxy** from the
  World Bank. Carrying a monthly value across half-hours cannot capture intraday gas-market
  changes, and the proxy is not a precise Irish or TTF settlement price.
- The model does not include plant availability, interconnector flows, weather, or time-based
  effects. Its half-hourly observations are also time-dependent, so the reported fit is not a
  causal estimate and does not provide independent-observation uncertainty bounds.
- The scripts rebuild their respective SQLite tables when run. Keep the source JSON cache and
  price exports and cached World Bank workbook if you need to reproduce a run.
- `isem.db` is generated locally and is ignored by Git. The checked-in `data/raw/` JSON files
  are source snapshots; additional fetched months can be retained there to reproduce an
  analysis.

## Run the tests

The tests use only Python's standard library and do not make network requests or modify the
project database:

```text
python -m unittest discover -s tests -v
```
