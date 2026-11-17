"""Read-only dataset inventory, including unsupported inputs; no network calls."""
import hashlib
from pathlib import Path
from .sources import snapshot_input


def inventory(directory, records, hashes, snapshot=None):
    entries = []
    snapshot = snapshot or snapshot_input(directory)
    for name, content in snapshot.files.items():
        path = Path(name)
        source_records = [r for r in records if r.source_file == path.name]
        sheets = sorted({r.sheet_name for r in source_records if r.sheet_name is not None})
        entries.append({'file': path.name, 'bytes': len(content),
            'sha256': hashes.get(path.name) or hashlib.sha256(content).hexdigest(),
            'format': path.suffix.lower().lstrip('.') or 'unknown',
            'worksheets': sheets, 'records': len(source_records),
            'table_kinds': sorted({r.table_kind for r in source_records}),
            'columns': sorted({str(cell['column']) for r in source_records
                               for cell in r.raw_record.values() if isinstance(cell, dict) and cell.get('column') is not None}),
            'parse_errors': sum(r.parse_status == 'error' for r in source_records),
            'telemetry_time_range': 'not_evaluated',
            'limitation': 'Metadata inventory only; format support determined by parser findings.'})
    return {'files': entries, 'sponsor_truth': 'unknown', 'external_processing': False}
