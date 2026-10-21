import datetime as dt
import io
import json
import os
import random
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing, redirect_stdout
from unittest import mock

import build_dataset
import fetch_eirgrid
import join_and_chart
import load_gas_prices
import load_prices


class GasPriceTests(unittest.TestCase):
    def test_parse_workbook_reads_european_gas_column_and_months(self):
        workbook = io.BytesIO()
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr(
                "xl/workbook.xml",
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Monthly Prices" sheetId="1" r:id="rId1"/>'
                '</sheets></workbook>',
            )
            archive.writestr(
                "xl/_rels/workbook.xml.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                'Type="worksheet"/></Relationships>',
            )
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData>'
                '<row r="4"><c r="A4" t="inlineStr"><is><t>Updated on October 02, 2026'
                '</t></is></c></row>'
                '<row r="5"><c r="I5" t="inlineStr"><is><t>Natural gas, Europe</t>'
                '</is></c></row>'
                '<row r="6"><c r="A6" t="inlineStr"><is><t>2025M10</t></is></c>'
                '<c r="I6"><v>10.89</v></c></row>'
                '<row r="7"><c r="A7" t="inlineStr"><is><t>2025M11</t></is></c>'
                '<c r="I7"><v>10.42</v></c></row>'
                '</sheetData></worksheet>',
            )

        prices, update_note = load_gas_prices.parse_workbook(workbook.getvalue())

        self.assertEqual(prices, {"2025-10": 10.89, "2025-11": 10.42})
        self.assertEqual(update_note, "Updated on October 02, 2026")


class BuildDatasetTests(unittest.TestCase):
    def test_main_records_validation_exceptions_and_loads_valid_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = os.path.join(temp_dir, "raw")
            reports_dir = os.path.join(temp_dir, "reports")
            db_path = os.path.join(temp_dir, "isem.db")
            os.makedirs(raw_dir)
            rows = [
                {"EffectiveTime": "01-Aug-2026 12:00:00", "FieldName": "WIND_ACTUAL",
                 "Value": 1000},
                {"EffectiveTime": "01-Aug-2026 12:00:00", "FieldName": "SYSTEM_DEMAND",
                 "Value": 2000},
                {"EffectiveTime": "01-Aug-2026 12:00:00", "FieldName": "SOLAR_ACTUAL",
                 "Value": -1},
                {"EffectiveTime": "01-Aug-2026 12:00:00", "FieldName": "WIND_ACTUAL",
                 "Value": 1001},
                {"EffectiveTime": "01-Aug-2026 12:15:00", "FieldName": "SYSTEM_DEMAND",
                 "Value": None},
                {"EffectiveTime": "01-Aug-2026 12:30:00", "FieldName": "WIND_ACTUAL",
                 "Value": -1},
                {"EffectiveTime": "01-Aug-2026 12:45:00", "FieldName": "WIND_ACTUAL",
                 "Value": 20000},
                {"EffectiveTime": "not-a-time", "FieldName": "WIND_ACTUAL", "Value": 100},
                {"EffectiveTime": "01-Aug-2026 13:00:00", "FieldName": "UNKNOWN",
                 "Value": 100},
            ]
            with open(os.path.join(raw_dir, "eirgrid_2026-08.json"), "w",
                      encoding="utf-8") as source:
                json.dump({"Rows": rows}, source)

            output = io.StringIO()
            with mock.patch.object(build_dataset, "RAW", raw_dir), \
                    mock.patch.object(build_dataset, "REPORTS", reports_dir), \
                    mock.patch.object(build_dataset, "DB", db_path), \
                    redirect_stdout(output):
                build_dataset.main()

            with closing(sqlite3.connect(db_path)) as conn:
                loaded = conn.execute(
                    "SELECT effective_time, wind_mw, demand_mw, solar_mw "
                    "FROM system_15min"
                ).fetchall()
            with open(os.path.join(reports_dir, "exceptions.csv"), encoding="utf-8") as fh:
                exceptions = fh.read()

            self.assertEqual(
                loaded,
                [("2026-08-01 12:00:00", 1000, 2000, None)],
            )
            for reason in (
                "negative value treated as missing",
                "duplicate timestamp (clock change)",
                "null value",
                "wind above sanity ceiling",
                "unparseable timestamp",
                "unknown field name",
            ):
                self.assertIn(reason, exceptions)
            self.assertIn("Rows        read 9, kept 2", output.getvalue())


class FetchEirGridTests(unittest.TestCase):
    def test_month_bounds_handles_leap_year(self):
        self.assertEqual(
            fetch_eirgrid.month_bounds(2024, 2),
            ("01-Feb-2024", "29-Feb-2024"),
        )

    def test_month_url_contains_requested_month_and_series(self):
        url = fetch_eirgrid.month_url(2026, 9)

        self.assertIn("dateFrom=01-Sep-2026", url)
        self.assertIn("dateTo=30-Sep-2026", url)
        self.assertIn("areas=windactual,demandactual", url)

    def test_months_back_returns_complete_months_oldest_first(self):
        self.assertEqual(
            fetch_eirgrid.months_back(3, today=dt.date(2026, 1, 10)),
            [(2025, 10), (2025, 11), (2025, 12)],
        )

    def test_parse_arg_months_includes_both_endpoints(self):
        self.assertEqual(
            fetch_eirgrid.parse_arg_months(["2025-11", "2026-02"]),
            [(2025, 11), (2025, 12), (2026, 1), (2026, 2)],
        )


class PriceParsingTests(unittest.TestCase):
    def test_parse_date_accepts_documented_formats(self):
        self.assertEqual(load_prices.parse_date("2026-10-18"), dt.date(2026, 10, 18))
        self.assertEqual(load_prices.parse_date("18/10/2026"), dt.date(2026, 10, 18))

    def test_parse_line_handles_tab_separated_currency_and_thousands(self):
        parsed = load_prices.parse_line("18/10/2026\t00:00\t€1,234.50\t10")

        self.assertEqual(
            parsed,
            (dt.datetime(2026, 10, 18, 0, 0), 1234.5),
        )

    def test_parse_line_skips_header_or_invalid_price(self):
        self.assertIsNone(load_prices.parse_line("Date\tTime\tEUR/MWh"))
        self.assertIsNone(load_prices.parse_line("2026-10-18 00:00 not-a-price"))


class TimeAndStatisticsTests(unittest.TestCase):
    def test_main_reports_demand_and_gas_adjusted_model(self):
        rng = random.Random(5)
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "isem.db")
            chart_path = os.path.join(temp_dir, "wind_vs_price.svg")
            with closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute(
                        "CREATE TABLE system_15min "
                        "(effective_time TEXT, wind_mw REAL, demand_mw REAL)"
                    )
                    conn.execute(
                        "CREATE TABLE dam_price_30min (utc_time TEXT, eur_per_mwh REAL)"
                    )
                    conn.execute(
                        "CREATE TABLE gas_price_monthly (month TEXT, usd_per_mmbtu REAL)"
                    )
                    gas_by_month = {
                        "2025-10": 10.0,
                        "2025-11": 12.0,
                        "2025-12": 14.0,
                        "2026-01": 16.0,
                    }
                    conn.executemany(
                        "INSERT INTO gas_price_monthly VALUES (?, ?)",
                        gas_by_month.items(),
                    )
                    system_rows = []
                    price_rows = []
                    for month in gas_by_month:
                        year, month_number = (int(part) for part in month.split("-"))
                        for day in (5, 12, 19):
                            utc = dt.datetime(
                                year, month_number, day, 12, tzinfo=dt.timezone.utc
                            )
                            local = utc.astimezone(join_and_chart.DUBLIN).replace(
                                tzinfo=None
                            )
                            wind = rng.uniform(1000, 5000)
                            demand = rng.uniform(3000, 7000)
                            gas = gas_by_month[month]
                            price = 80 - 0.01 * wind + 0.02 * demand + 2.5 * gas
                            for quarter in (0, 15):
                                stamp = local + dt.timedelta(minutes=quarter)
                                system_rows.append(
                                    (stamp.strftime("%Y-%m-%d %H:%M:%S"), wind, demand)
                                )
                            price_rows.append(
                                (utc.strftime("%Y-%m-%d %H:%M:%S"), price)
                            )
                    conn.executemany(
                        "INSERT INTO system_15min VALUES (?, ?, ?)", system_rows
                    )
                    conn.executemany(
                        "INSERT INTO dam_price_30min VALUES (?, ?)", price_rows
                    )

            output = io.StringIO()
            with mock.patch.object(join_and_chart, "DB", db_path), \
                    mock.patch.object(join_and_chart, "CHART", chart_path), \
                    redirect_stdout(output):
                join_and_chart.main()

            self.assertRegex(output.getvalue(), r"adjusted_model_joined\s+12")
            self.assertIn("Wind:   -10.00 EUR/MWh per 1,000 MW", output.getvalue())
            self.assertIn("Demand: +20.00 EUR/MWh per 1,000 MW", output.getvalue())
            self.assertIn("Gas:    +2.50 EUR/MWh per $1/MMBtu", output.getvalue())

    def test_main_averages_wind_quarters_and_joins_matching_prices(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "isem.db")
            chart_path = os.path.join(temp_dir, "wind_vs_price.svg")
            with closing(sqlite3.connect(db_path)) as conn:
                with conn:
                    conn.execute(
                        "CREATE TABLE system_15min "
                        "(effective_time TEXT, wind_mw REAL, demand_mw REAL)"
                    )
                    conn.execute(
                        "CREATE TABLE dam_price_30min (utc_time TEXT, eur_per_mwh REAL)"
                    )
                    conn.execute(
                        "CREATE TABLE gas_price_monthly (month TEXT, usd_per_mmbtu REAL)"
                    )
                    conn.executemany(
                        "INSERT INTO system_15min VALUES (?, ?, ?)",
                        [
                            ("2026-08-01 12:00:00", 100, 1000),
                            ("2026-08-01 12:15:00", 200, 1100),
                            ("2026-08-01 12:30:00", 300, 1200),
                            ("2026-08-01 12:45:00", 400, 1300),
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO dam_price_30min VALUES (?, ?)",
                        [
                            ("2026-08-01 11:00:00", 20),
                            ("2026-08-01 11:30:00", 10),
                        ],
                    )
                    conn.execute(
                        "INSERT INTO gas_price_monthly VALUES (?, ?)", ("2026-08", 10.0)
                    )

            output = io.StringIO()
            with mock.patch.object(join_and_chart, "DB", db_path), \
                    mock.patch.object(join_and_chart, "CHART", chart_path), \
                    redirect_stdout(output):
                join_and_chart.main()

            self.assertRegex(output.getvalue(), r"wind_halfhours\s+2")
            self.assertRegex(output.getvalue(), r"demand_halfhours\s+2")
            self.assertRegex(output.getvalue(), r"joined\s+2")
            self.assertRegex(output.getvalue(), r"adjusted_model_joined\s+2")
            self.assertIn("Not calculated: need more than four joined periods",
                          output.getvalue())
            with open(chart_path, encoding="utf-8") as chart:
                self.assertIn("<svg", chart.read())

    def test_local_to_utc_converts_unambiguous_summer_time(self):
        local = dt.datetime(2026, 8, 1, 12, 0)

        utc, reason = join_and_chart.local_to_utc(local)

        self.assertIsNone(reason)
        self.assertEqual(utc, dt.datetime(2026, 8, 1, 11, 0, tzinfo=dt.timezone.utc))

    def test_local_to_utc_rejects_ambiguous_clock_back_time(self):
        utc, reason = join_and_chart.local_to_utc(dt.datetime(2026, 10, 25, 1, 30))

        self.assertIsNone(utc)
        self.assertEqual(reason, "ambiguous_clock_back")

    def test_local_to_utc_rejects_nonexistent_clock_forward_time(self):
        utc, reason = join_and_chart.local_to_utc(dt.datetime(2026, 3, 29, 1, 30))

        self.assertIsNone(utc)
        self.assertEqual(reason, "nonexistent_clock_forward")

    def test_pearson_returns_expected_correlation_and_handles_constant_input(self):
        self.assertAlmostEqual(join_and_chart.pearson([1, 2, 3], [2, 4, 6]), 1.0)
        self.assertIsNone(join_and_chart.pearson([1, 1, 1], [2, 4, 6]))

    def test_multiple_regression_recovers_coefficients_from_controlled_data(self):
        rng = random.Random(12)
        rows = []
        for _ in range(30):
            wind = rng.uniform(1000, 5000)
            demand = rng.uniform(3000, 7000)
            gas = rng.uniform(5, 25)
            price = 80 - 0.01 * wind + 0.02 * demand + 2.5 * gas
            rows.append((wind, demand, gas, price))

        intercept, coefficients, r_squared = join_and_chart.multiple_regression(rows)

        self.assertAlmostEqual(intercept, 80)
        self.assertAlmostEqual(coefficients[0], -0.01)
        self.assertAlmostEqual(coefficients[1], 0.02)
        self.assertAlmostEqual(coefficients[2], 2.5)
        self.assertAlmostEqual(r_squared, 1.0)

    def test_multiple_regression_rejects_a_constant_gas_series(self):
        rows = [(float(i), float(i * i), 10.0, float(i * 3)) for i in range(6)]

        with self.assertRaisesRegex(ValueError, "gas does not vary"):
            join_and_chart.multiple_regression(rows)

    def test_deciles_are_ordered_and_cover_all_observations(self):
        pairs = [(float(wind), float(100 - wind)) for wind in range(10)]

        result = join_and_chart.deciles(pairs)

        self.assertEqual(len(result), 10)
        self.assertEqual(sum(count for _, _, count in result), len(pairs))
        self.assertEqual([count for _, _, count in result], [1] * 10)
        self.assertEqual([wind for wind, _, _ in result], sorted(wind for wind, _, _ in result))


if __name__ == "__main__":
    unittest.main()
