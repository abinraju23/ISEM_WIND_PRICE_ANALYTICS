"""Download the World Bank monthly European natural-gas price series and load it into SQLite.

Standard library only. The workbook is cached under data/raw/ so a later run can be
reproduced without downloading it again.

Usage:
    python load_gas_prices.py
    python load_gas_prices.py --refresh
"""
import os
import math
import re
import sqlite3
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from posixpath import dirname, normpath, join

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, 'data', 'raw')
WORKBOOK = os.path.join(RAW, 'world_bank_commodity_monthly.xlsx')
DB = os.path.join(HERE, 'isem.db')
COMMODITY_PAGE = 'https://www.worldbank.org/en/research/commodity-markets'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
              'ISEM-wind-price-analysis/1.0')

MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
MONTH_PATTERN = re.compile(r'^(\d{4})M(0[1-9]|1[0-2])$')
PINK_SHEET_PATTERN = re.compile(
    r'https?://thedocs\.worldbank\.org/en/doc/[^"\'<>\s]+/'
    r'CMO-Pink-Sheet-[^"\'<>\s]+\.pdf'
)


def request_bytes(url):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError('HTTP %s while requesting %s' % (exc.code, url))
    except urllib.error.URLError as exc:
        raise RuntimeError('network error while requesting %s: %s' % (url, exc.reason))


def latest_workbook_url():
    """Find the workbook paired with the latest Pink Sheet linked by the World Bank."""
    html = request_bytes(COMMODITY_PAGE).decode('utf-8', errors='replace')
    match = PINK_SHEET_PATTERN.search(html)
    if match is None:
        raise RuntimeError('could not find the latest Pink Sheet link on %s' % COMMODITY_PAGE)
    return join(dirname(match.group(0)), 'CMO-Historical-Data-Monthly.xlsx')


def _column_index(cell_ref):
    letters = ''.join(char for char in cell_ref if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + ord(char.upper()) - ord('A') + 1
    return index


def _cell_text(cell, shared_strings):
    cell_type = cell.get('t')
    if cell_type == 'inlineStr':
        return ''.join(node.text or '' for node in cell.findall(
            './/{%s}t' % MAIN_NS))
    value = cell.find('{%s}v' % MAIN_NS)
    if value is None or value.text is None:
        return ''
    if cell_type == 's':
        return shared_strings[int(value.text)]
    return value.text


def parse_workbook(payload):
    """Return (month-to-price mapping, workbook update note) from a Pink Sheet XLSX."""
    try:
        archive = zipfile.ZipFile(BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise RuntimeError('World Bank response is not a valid XLSX workbook') from exc

    with archive:
        try:
            workbook = ET.fromstring(archive.read('xl/workbook.xml'))
            relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        except (KeyError, ET.ParseError) as exc:
            raise RuntimeError('World Bank workbook is missing its workbook metadata') from exc

        relation_targets = {
            relation.get('Id'): relation.get('Target')
            for relation in relationships.findall('{%s}Relationship' % PKG_REL_NS)
        }
        sheet_path = None
        for sheet in workbook.findall('.//{%s}sheet' % MAIN_NS):
            if sheet.get('name') == 'Monthly Prices':
                target = relation_targets.get(sheet.get('{%s}id' % REL_NS))
                if target:
                    target = target.lstrip('/')
                    sheet_path = (normpath(target) if target.startswith('xl/')
                                  else normpath(join('xl', target)))
                    break
        if sheet_path is None:
            raise RuntimeError('World Bank workbook has no Monthly Prices worksheet')

        try:
            shared_root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
        except KeyError:
            shared_strings = []
        except ET.ParseError as exc:
            raise RuntimeError('World Bank workbook has invalid shared strings') from exc
        else:
            shared_strings = [
                ''.join(node.text or '' for node in item.findall('.//{%s}t' % MAIN_NS))
                for item in shared_root.findall('{%s}si' % MAIN_NS)
            ]

        try:
            worksheet = ET.fromstring(archive.read(sheet_path))
        except (KeyError, ET.ParseError) as exc:
            raise RuntimeError('World Bank workbook has an unreadable Monthly Prices sheet') from exc

        rows = worksheet.findall('.//{%s}sheetData/{%s}row' % (MAIN_NS, MAIN_NS))
        gas_column = None
        update_note = ''
        for row in rows[:10]:
            for cell in row.findall('{%s}c' % MAIN_NS):
                value = _cell_text(cell, shared_strings).strip()
                if value == 'Natural gas, Europe':
                    gas_column = _column_index(cell.get('r', ''))
                if value.startswith('Updated on '):
                    update_note = value
        if gas_column is None:
            raise RuntimeError('World Bank workbook has no Natural gas, Europe column')

        prices = {}
        for row in rows:
            cells = {
                _column_index(cell.get('r', '')): _cell_text(cell, shared_strings).strip()
                for cell in row.findall('{%s}c' % MAIN_NS)
            }
            month_match = MONTH_PATTERN.match(cells.get(1, ''))
            if month_match is None:
                continue
            month = '%s-%s' % (month_match.group(1), month_match.group(2))
            raw_price = cells.get(gas_column, '')
            if not raw_price:
                continue
            try:
                price = float(raw_price)
            except ValueError as exc:
                raise RuntimeError('invalid gas price for %s in World Bank workbook'
                                   % month) from exc
            if not math.isfinite(price):
                raise RuntimeError('non-finite gas price for %s in World Bank workbook'
                                   % month)
            prices[month] = price

    if not prices:
        raise RuntimeError('World Bank workbook contains no monthly gas-price observations')
    return prices, update_note


def get_workbook(force=False):
    if os.path.isfile(WORKBOOK) and not force:
        with open(WORKBOOK, 'rb') as fh:
            payload = fh.read()
        parse_workbook(payload)
        return payload, 'cache'

    url = latest_workbook_url()
    payload = request_bytes(url)
    parse_workbook(payload)
    os.makedirs(RAW, exist_ok=True)
    temporary = WORKBOOK + '.tmp'
    try:
        with open(temporary, 'wb') as fh:
            fh.write(payload)
        os.replace(temporary, WORKBOOK)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
    return payload, 'network'


def main(argv):
    if argv not in ([], ['--refresh']):
        sys.exit('usage: python load_gas_prices.py [--refresh]')

    payload, source = get_workbook(force=argv == ['--refresh'])
    prices, update_note = parse_workbook(payload)

    conn = sqlite3.connect(DB)
    try:
        conn.execute('DROP TABLE IF EXISTS gas_price_monthly')
        conn.execute('''CREATE TABLE gas_price_monthly (
                            month            TEXT PRIMARY KEY,
                            usd_per_mmbtu    REAL NOT NULL)''')
        conn.executemany(
            'INSERT INTO gas_price_monthly VALUES (?,?)',
            sorted(prices.items()),
        )
        conn.commit()
    finally:
        conn.close()

    months = sorted(prices)
    print('World Bank Natural gas, Europe: %d monthly observations (%s)'
          % (len(months), source))
    if update_note:
        print('Source workbook: %s' % update_note)
    print('Coverage: %s to %s' % (months[0], months[-1]))
    print('Loaded gas_price_monthly into %s (USD/MMBtu)' % DB)


if __name__ == '__main__':
    main(sys.argv[1:])
