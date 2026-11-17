"""FR-01 CSV/XLSX adapters: raw cells, sheet/line provenance and explicit failures."""
import csv
import hashlib
import io
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from urllib.parse import quote
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from .config import InputConfig
from .models import SourceRecord
from .units import normalize_unit
from .sources import snapshot_input
from .identity import uid, building_scope, unique_source_map
from .limits import Limits, check_workbook
from .errors import EngineError

REQUIRED = {'equipment': ('equipment_id',), 'points': ('point_id',),
            'connections': ('from_equipment', 'to_equipment'), 'measurements': ('measurement_id','point_id'),
            'thermal_checks': ('check_id','equipment_id'), 'unknown': ()}
TABLES = {kind + '.csv': required for kind, required in REQUIRED.items() if kind != 'unknown'}


def normalized(value):
    if value is None:
        return ''
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return str(value).strip()


def json_cell(value):
    return value.isoformat() if isinstance(value, (date, datetime, time)) else value


def ingest(directory: Path, config=None, snapshot=None, scope=None, limits=None):
    directory = Path(directory)
    config = config or InputConfig()
    limits = limits or Limits()
    total_cells = [0]
    snapshot = snapshot or snapshot_input(directory, limits)
    scope = building_scope(snapshot.root, scope)
    records, hashes, originals = [], {}, {}
    seen_mappings = set()

    def add(path, digest, sheet, row, end, values, headers, fields, kind, errors, cells=None):
        prefix = path.name if sheet is None else path.name + ':' + quote(sheet, safe='')
        raw = {str(i): {'column': json_cell(headers[i]) if i < len(headers) else None,
                        'value': json_cell(value)} for i, value in enumerate(values)}
        if cells:
            for i, cell in enumerate(cells):
                raw[str(i)].update(cell)
        record = SourceRecord(source_id=f'{prefix}:{row}', source_file=path.name,
            source_type=path.suffix.lower().lstrip('.') or 'unknown',
            table_kind=kind, sheet_name=sheet,
            source_location=(f'sheet {sheet}; ' if sheet is not None else '') + f'rows {row}-{end}',
            cell_locations={str(i): cell['coordinate'] for i, cell in enumerate(cells or [])},
            source_record_uid=uid('src', scope, path.name, digest, sheet, row),
            legacy_source_id=f'{prefix}:{row}', source_hash=digest, raw_value=[json_cell(v) for v in values], raw_record=raw,
            normalized_fields=fields, parse_status='error' if errors else 'ok', errors=errors)
        limits.require(len(records) < limits.max_rows, 'parsed rows')
        limits.require(len(values) <= limits.max_columns, 'parsed columns')
        total_cells[0] += len(values)
        limits.require(total_cells[0] <= limits.max_cells, 'parsed cells')
        records.append(record)

    def diagnostic(path, digest, message, sheet=None):
        add(path, digest, sheet, 'file', 'file', [], [], {}, 'unknown', [message])

    def consume(path, digest, sheet, rows):
        mapping = next((t for t in config.tables if t.file == path.name and t.sheet in (None, sheet)), None)
        if mapping:
            seen_mappings.add((mapping.file, mapping.sheet))
        header_row = mapping.header_row if mapping else 1
        headers, keys, kind, header_errors = [], [], 'unknown', []
        found_header = False
        data_rows = 0
        for start, end, values, cell_info, errors in rows:
            if start < header_row:
                add(path, digest, sheet, start, end, values, [], {}, 'unknown',
                    ['Preamble before configured header; retained without interpretation'], cell_info)
                continue
            if not found_header:
                found_header = True
                headers = values
                keys = [normalized(h).lower() for h in headers]
                aliases = {v.strip().lower(): k for k, v in (mapping.columns.items() if mapping else [])}
                keys = [aliases.get(k, k) for k in keys]
                kind = mapping.kind if mapping else path.stem.lower()
                if kind not in REQUIRED:
                    kind = (sheet or '').lower()
                if kind not in REQUIRED:
                    kind = ('points' if 'point_id' in keys else 'connections' if
                            {'from_equipment', 'to_equipment'} <= set(keys) else
                            'equipment' if 'equipment_id' in keys else 'unknown')
                if not headers or any(not k for k in keys) or len(set(keys)) != len(keys):
                    header_errors.append('Empty/duplicate/conflicting CSV/XLSX headers')
                if mapping and any(v.strip().lower() not in [normalized(h).lower() for h in headers]
                                   for v in mapping.columns.values()):
                    header_errors.append('Configured source column absent')
                if kind == 'unknown':
                    header_errors.append('Unrecognized table; configure file/sheet/column mappings')
                if any(k not in keys for k in REQUIRED[kind]):
                    header_errors.append('Missing required columns: ' + ','.join(REQUIRED[kind]))
                header_errors += errors
                if header_errors:
                    prefix = path.name if sheet is None else path.name + ':' + quote(sheet, safe='')
                    add(path, digest, sheet, start, end, headers, headers, {}, kind, header_errors, cell_info)
                    records[-1].source_id = prefix + ':header'
                continue
            fields = {k: normalized(v) for k, v in zip(keys, values)}
            data_rows += 1
            if 'units' in fields:
                fields['units'] = normalize_unit(fields['units'])
            errors = header_errors + errors
            if len(values) != len(headers):
                errors.append('Row width differs from header')
            if not any(normalized(v) for v in values):
                errors.append('Empty record')
            if any(not fields.get(k) for k in REQUIRED[kind]):
                errors.append('Empty required identifier')
            add(path, digest, sheet, start, end, values, headers, fields, kind, errors, cell_info)
        if not found_header:
            diagnostic(path, digest, 'Empty table or configured header row absent', sheet)
        elif not data_rows and not header_errors:
            add(path, digest, sheet, 'empty', 'empty', headers, headers, {}, kind,
                ['Table contains header but no data rows'])
            records[-1].source_location = (f'sheet {sheet}; ' if sheet else '') + f'header row {header_row}; no data rows'

    for filename, content in snapshot.files.items():
        path = directory / filename
        digest = hashlib.sha256(content).hexdigest()
        hashes[path.name], originals[path.name] = digest, content
        if path.name in {'SYNTHETIC.txt', 'README.md', 'DATA_PROVENANCE.json'}:
            continue
        if path.suffix.lower() == '.csv':
            try:
                decoded = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                diagnostic(path, digest, 'Invalid UTF-8; original bytes preserved in sources/')
                continue
            reader = csv.reader(io.StringIO(decoded, newline=''), strict=True)

            def csv_rows():
                while True:
                    start = reader.line_num + 1
                    try:
                        values = next(reader)
                        errors = []
                    except StopIteration:
                        break
                    except csv.Error as exc:
                        values = decoded.splitlines(keepends=True)[start-1:reader.line_num]
                        errors = ['CSV parse failure: ' + str(exc)]
                    yield start, reader.line_num, values, None, errors
            consume(path, digest, None, csv_rows())
        elif path.suffix.lower() == '.xlsx':
            try:
                check_workbook(content, limits)
                workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
                try:
                    for sheet in workbook:
                        def xlsx_rows():
                            for n, cells in enumerate(sheet.iter_rows(), 1):
                                values = [cell.value for cell in cells]
                                info = [{'coordinate': f'{sheet.title}!{get_column_letter(i)}{n}',
                                         'data_type': getattr(cell, 'data_type', 'n'),
                                         'number_format': getattr(cell, 'number_format', 'General')}
                                        for i, cell in enumerate(cells, 1)]
                                errors = []
                                if any(getattr(cell, 'data_type', None) == 'f' for cell in cells):
                                    errors.append('Formula cell retained; formulas are not evaluated')
                                if any(getattr(cell, 'data_type', None) == 'e' for cell in cells):
                                    errors.append('Excel error cell')
                                yield n, n, values, info, errors
                        consume(path, digest, sheet.title, xlsx_rows())
                finally:
                    workbook.close()
            except EngineError:
                raise
            except Exception as exc:
                diagnostic(path, digest, 'XLSX parse failure: ' + type(exc).__name__)
        else:
            diagnostic(path, digest, 'Unsupported input format; provide extracted CSV/XLSX table')
    for mapping in config.tables:
        if (mapping.file, mapping.sheet) not in seen_mappings:
            path = directory / mapping.file
            diagnostic(path, hashes.get(mapping.file, ''), 'Configured file/worksheet absent: ' + str(mapping.sheet))
    if not any(Path(name).suffix.lower() in {'.csv', '.xlsx'} for name in hashes):
        return [], {}, {}
    for kind, key in [('points', 'point_id'), ('equipment', 'equipment_id')]:
        counts = Counter(r.normalized_fields.get(key) for r in records if r.table_kind == kind
                         and r.normalized_fields.get(key))
        for r in records:
            if r.table_kind == kind and counts[r.normalized_fields.get(key)] > 1:
                r.errors.append('Duplicate identifier')
                r.parse_status = 'error'
    counts = Counter(r.source_id for r in records)
    for r in records:
        if counts[r.source_id] > 1:
            r.source_id = r.source_record_uid
    unique_source_map(records)
    return records, hashes, originals
