"""Explicit, versioned mappings for unfamiliar source filenames/columns/worksheets."""
import json
from pathlib import Path
from typing import Dict, List, Literal, Optional
from pydantic import Field, model_validator
from .models import Model

Kind = Literal['points', 'equipment', 'connections', 'measurements', 'thermal_checks', 'unknown']
CANONICAL = {'point_id', 'description', 'equipment_id', 'units', 'value', 'point_class',
             'equipment_type', 'from_equipment', 'to_equipment', 'connection_id',
             'predicate', 'medium', 'source_description', 'status', 'ownership_scope', 'ownership_exclusive',
             'connection_scope', 'direction_exclusive', 'medium_exclusive', 'controller_id', 'channel_id',
             'measurement_role', 'parent_equipment_id', 'system_id', 'naming_hint', 'correlation_with',
             'drawing_ref', 'telemetry_start', 'timezone', 'sample_rate', 'measurement_id', 'quantity_kind',
             'timestamp', 'quality', 'unsigned_flow', 'operating_state', 'check_id', 'inlet_point_id', 'outlet_point_id',
             'flow_point_id', 'power_point_id', 'density', 'density_units', 'specific_heat', 'specific_heat_units',
             'mode', 'assumptions', 'window_start', 'window_end', 'sample_period_seconds', 'max_gap_seconds', 'min_samples', 'tolerance_fraction'}

class TableMapping(Model):
    file: str
    kind: Kind
    sheet: Optional[str] = None
    header_row: int = Field(default=1, ge=1)
    columns: Dict[str, str] = Field(default_factory=dict)

    @model_validator(mode='after')
    def safe_mapping(self):
        if Path(self.file).name != self.file or self.file in {'.', '..'}:
            raise ValueError('Mapping files must be basenames inside the input directory')
        if not set(self.columns) <= CANONICAL:
            raise ValueError('Unknown canonical mapping field')
        if len({v.strip().lower() for v in self.columns.values()}) != len(self.columns):
            raise ValueError('One source column cannot map to multiple canonical fields')
        return self

class InputConfig(Model):
    version: Literal[1] = 1
    inference_mode: Literal['legacy', 'contextual'] = 'legacy'
    tables: List[TableMapping] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_mappings(self):
        keys = [(t.file, t.sheet) for t in self.tables]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate file/sheet mapping')
        for file, sheet in keys:
            if sheet is not None and (file, None) in keys:
                raise ValueError('Overlapping workbook-wide and sheet mappings')
        return self


def load_config(path=None):
    return InputConfig.model_validate_json(Path(path).read_text(encoding='utf-8')) if path else InputConfig()
