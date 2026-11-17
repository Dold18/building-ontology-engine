"""Bounded, local-only CSV/XLSX worksheet inspection for guided onboarding.

Inspection does not persist source files, call a provider, accept an ontology claim,
or change the versioned InputConfig. Suggestions are deterministic naming heuristics
and must be reviewed before inclusion in a separate /api/upload operation.
"""
import base64
import csv
import hashlib
import io
import re
from pathlib import Path
from openpyxl import load_workbook

from .ingest import REQUIRED
from .config import CANONICAL
from .limits import Limits, check_workbook

SAFE_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
MAX_FILE = 2_000_000
MAX_TOTAL = 8_000_000
MAX_TABLES = 32
MAX_COLUMNS = 64
MAX_ROWS = 6
MAX_CELL = 160
MAX_ERRORS = 8

ALIASES = {
    "point_id": ("point_id", "pointname", "point_name", "pointtag", "point_tag",
                 "tag", "object_id", "objectname", "point"),
    "description": ("description", "point_description", "desc", "object_description"),
    "equipment_id": ("equipment_id", "equipment", "equip_id", "asset_id",
                     "equipment_tag", "equip_tag"),
    "equipment_type": ("equipment_type", "equip_type", "asset_type"),
    "point_class": ("point_class", "brick_class", "brick_point_class"),
    "units": ("units", "unit", "uom", "engineering_units"),
    "value": ("value", "reading", "present_value"),
    "from_equipment": ("from_equipment", "source_equipment", "from_asset"),
    "to_equipment": ("to_equipment", "target_equipment", "to_asset"),
    "predicate": ("predicate", "relation", "relationship"),
    "connection_id": ("connection_id", "relationship_id"),
    "measurement_id": ("measurement_id", "measurement_tag"),
    "timestamp": ("timestamp", "time_stamp", "date_time"),
    "quantity_kind": ("quantity_kind", "quantity"),
    "sample_rate": ("sample_rate", "sample_interval"),
    "check_id": ("check_id", "thermal_check_id"),
    "inlet_point_id": ("inlet_point_id", "inlet_sensor"),
    "outlet_point_id": ("outlet_point_id", "outlet_sensor"),
    "system_id": ("system_id", "system", "system_tag"),
    "naming_hint": ("naming_hint", "tag_prefix"),
    "drawing_ref": ("drawing_ref", "drawing_reference", "sheet_ref"),
    "medium": ("medium", "fluid", "flow_medium"),
}


def _normalize(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def suggest_columns(headers):
    """No fuzzy semantic guesswork: match explicit known aliases only."""
    proposed = {}
    used = set()
    for canonical, aliases in ALIASES.items():
        for header in headers:
            if header in used:
                continue
            if _normalize(header) in {_normalize(alias) for alias in aliases}:
                proposed[canonical] = header
                used.add(header)
                break
    return proposed


def suggest_kind(filename, sheet, columns):
    name = Path(filename).stem.lower()
    sheet_name = (sheet or "").lower()
    known = (name, sheet_name)
    for explicit in known:
        if explicit in REQUIRED and explicit != "unknown":
            return explicit
    found = set(columns)
    if {"from_equipment", "to_equipment"} <= found:
        return "connections"
    if {"check_id", "equipment_id"} <= found:
        return "thermal_checks"
    if {"measurement_id", "point_id"} <= found:
        return "measurements"
    if "point_id" in found:
        return "points"
    if "equipment_id" in found:
        return "equipment"
    return "unknown"


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float, bool)):
        value = str(value)
    elif not isinstance(value, str):
        value = value.isoformat() if hasattr(value, "isoformat") else str(value)
    return value[:MAX_CELL]


def summarize_table(file, sheet, rows):
    visible = rows[:MAX_ROWS]
    width = max((i + 1 for row in visible for i, value in enumerate(row[:MAX_COLUMNS])
                 if value is not None and str(value).strip()), default=0)
    rows = [list(r[:width]) for r in visible]
    if not rows:
        return {"sheet": sheet, "header_row": 1, "headers": [], "sample_rows": [],
                "proposed_kind": "unknown", "proposed_columns": {},
                "missing_required": [], "invalid_headers": True,
                "warnings": ["Empty worksheet or CSV"]}
    raw_headers = [str(x).strip() if x is not None else "" for x in rows[0]]
    headers = raw_headers[:MAX_COLUMNS]
    warnings = []
    if not any(headers):
        warnings.append("First row has no column names; configure a different header row")
    if any(not header for header in headers):
        warnings.append("Blank header column; must be resolved before a reliable mapping")
    if len(set(h.lower() for h in headers)) != len(headers):
        warnings.append("Duplicate header names; resolve before upload")
    proposed = suggest_columns([h for h in headers if h])
    kind = suggest_kind(file, sheet, proposed)
    missing = [key for key in REQUIRED[kind] if key not in proposed]
    if kind == "unknown":
        warnings.append("Table type cannot be safely identified from header names")
    if missing:
        warnings.append("Required identifiers absent from proposals: " + ", ".join(missing))
    if len(rows[0]) > MAX_COLUMNS:
        warnings.append("Preview limited to first 64 columns")
    sample = [[_cell(value) for value in row[:len(headers)]]
              for row in rows[1:MAX_ROWS]]
    return {"sheet": sheet, "header_row": 1, "headers": headers,
            "sample_rows": sample, "proposed_kind": kind,
            "proposed_columns": proposed, "missing_required": missing,
            "invalid_headers": (not all(headers) or len({h.lower() for h in headers}) != len(headers)),
            "warnings": warnings[:MAX_ERRORS]}


def inspect_upload(body):
    if not isinstance(body, dict) or set(body) != {"files"}:
        raise ValueError("INSPECTION_SCHEMA")
    items = body["files"]
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("INSPECTION_FILE_LIMIT")
    total = 0
    seen = set()
    files = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "data"}:
            raise ValueError("INSPECTION_FILE_SCHEMA")
        name = item["name"]
        if (not isinstance(name, str) or not SAFE_FILE.fullmatch(name)
                or name in {".", ".."} or name in seen
                or Path(name).suffix.lower() not in (".csv", ".xlsx")):
            raise ValueError("INSPECTION_FILENAME")
        seen.add(name)
        encoded = item["data"]
        if not isinstance(encoded, str) or len(encoded) > 4 * MAX_FILE // 3 + 4:
            raise ValueError("INSPECTION_FILE_LIMIT")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError("INSPECTION_ENCODING") from None
        if not raw or len(raw) > MAX_FILE:
            raise ValueError("INSPECTION_FILE_LIMIT")
        total += len(raw)
        if total > MAX_TOTAL:
            raise ValueError("INSPECTION_TOTAL_LIMIT")
        tables = []
        if name.lower().endswith(".csv"):
            if b"\x00" in raw:
                raise ValueError("INSPECTION_BINARY")
            try:
                source = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise ValueError("INSPECTION_CSV_UTF8") from None
            try:
                reader = csv.reader(io.StringIO(source, newline=""), strict=True)
                rows = []
                for _, row in zip(range(MAX_ROWS), reader):
                    if len(row) > 256:
                        raise ValueError("INSPECTION_COLUMN_LIMIT")
                    rows.append(row)
            except csv.Error:
                raise ValueError("INSPECTION_CSV_FORMAT") from None
            tables.append(summarize_table(name, None, rows))
        else:
            try:
                check_workbook(raw, Limits())
                workbook = load_workbook(io.BytesIO(raw), read_only=True,
                                         data_only=False, keep_links=False)
                try:
                    if len(workbook.worksheets) > MAX_TABLES:
                        raise ValueError("INSPECTION_WORKSHEET_LIMIT")
                    for sheet in workbook.worksheets:
                        rows = list(sheet.iter_rows(min_row=1, max_row=MAX_ROWS,
                                                    max_col=MAX_COLUMNS, values_only=True))
                        tables.append(summarize_table(name, sheet.title, rows))
                finally:
                    workbook.close()
            except ValueError:
                raise
            except Exception:
                # Workbook diagnostic is stable and never includes source data.
                raise ValueError("INSPECTION_WORKBOOK") from None
        files.append({"name": name, "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest(),
                      "tables": tables})
    return {"files": files, "bytes": total, "file_count": len(files),
            "canonical_fields": sorted(CANONICAL),
            "required_by_kind": {key: list(value) for key, value in REQUIRED.items()},
            "sample_only": True, "processed_locally": True,
            "warning": "Deterministic column-name suggestions, not validated engineering interpretations. " +
                       "Only the first five data rows per worksheet are displayed, and no AI inference was run."}
