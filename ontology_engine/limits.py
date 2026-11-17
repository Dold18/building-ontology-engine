"""Bounded tabular execution v1. Conservative token upper bound is UTF-8 bytes."""
from typing import Optional
from pydantic import Field
from .models import Model
from .errors import EngineError

class Limits(Model):
    max_engineering_results: int = Field(default=200_000, ge=1, le=1_000_000)
    max_engineering_operations: int = Field(default=1_000_000, ge=1, le=5_000_000)
    max_batch_candidates: int = Field(default=128, ge=1, le=512)
    max_context_records: int = Field(default=24, ge=1, le=128)
    max_batch_targets: int = Field(default=8, ge=1, le=64)
    max_batches: int = Field(default=64, ge=1, le=256)
    max_field_chars: int = Field(default=1024, ge=16, le=4096)
    max_context_fields: int = Field(default=32, ge=1, le=128)
    max_retrieval_operations: int = Field(default=200_000, ge=1, le=2_000_000)
    max_total_input_tokens: int = Field(default=1_000_000, ge=1, le=10_000_000)
    max_total_output_tokens: int = Field(default=32768, ge=1, le=1_000_000)
    max_candidate_observations: int = Field(default=100_000, ge=1, le=500_000)
    max_conflict_comparisons: int = Field(default=100_000, ge=1, le=1_000_000)
    max_files: int = Field(default=100, ge=1, le=1000)
    max_file_bytes: int = Field(default=20_000_000, ge=1, le=200_000_000)
    max_total_bytes: int = Field(default=50_000_000, ge=1, le=500_000_000)
    max_worksheets: int = Field(default=32, ge=1, le=128)
    max_expanded_bytes: int = Field(default=100_000_000, ge=1, le=500_000_000)
    max_rows: int = Field(default=50_000, ge=1, le=500_000)
    max_columns: int = Field(default=256, ge=1, le=4096)
    max_cells: int = Field(default=1_000_000, ge=1, le=5_000_000)
    max_context_bytes: int = Field(default=300_000, ge=1, le=2_000_000)
    max_input_tokens: int = Field(default=300_000, ge=1, le=2_000_000)
    max_output_tokens: int = Field(default=4096, ge=1, le=32768)
    max_response_bytes: int = Field(default=2_000_000, ge=1, le=10_000_000)
    max_requests: int = Field(default=1, ge=0, le=64)
    max_run_seconds: int = Field(default=300, ge=1, le=3600)
    max_cost_usd: Optional[float] = Field(default=None, ge=0, le=100)
    input_usd_per_million: Optional[float] = Field(default=None, ge=0)
    output_usd_per_million: Optional[float] = Field(default=None, ge=0)

    def require(self, condition, resource):
        if not condition: raise EngineError('BUDGET_EXCEEDED', 'Resource limit exceeded: ' + resource, False)

    def check_files(self, paths):
        self.require(len(paths) <= self.max_files, 'file count')
        self.require(all(p.stat().st_size <= self.max_file_bytes for p in paths), 'file bytes')
        self.check_total_bytes(sum(p.stat().st_size for p in paths))

    def check_file_bytes(self, content): self.require(len(content) <= self.max_file_bytes, 'file bytes')
    def check_total_bytes(self, count): self.require(count <= self.max_total_bytes, 'total bytes')

    def request(self, payload):
        self.require(self.max_requests >= 1, 'provider request count')
        size = len(payload.encode('utf-8'))
        self.require(size <= self.max_context_bytes, 'context bytes')
        self.require(size <= self.max_input_tokens, 'conservative input token upper bound (UTF-8 bytes)')
        if self.max_cost_usd is not None:
            if self.input_usd_per_million is None or self.output_usd_per_million is None:
                raise EngineError('COST_NOT_EVALUABLE', 'Cost cap requires operator-supplied input/output pricing; no request sent.')
            estimate = (size * self.input_usd_per_million + self.max_output_tokens * self.output_usd_per_million) / 1_000_000
            self.require(estimate <= self.max_cost_usd, 'operator-priced cost upper bound')


def load_limits(path=None):
    from pathlib import Path
    return Limits.model_validate_json(Path(path).read_text()) if path else Limits()


def check_workbook(content, limits):
    from io import BytesIO
    from zipfile import ZipFile
    from defusedxml.ElementTree import fromstring
    from openpyxl.utils.cell import range_boundaries
    with ZipFile(BytesIO(content)) as archive:
        infos = archive.infolist()
        limits.require(len(infos) <= 1000, 'archive members')
        limits.require(sum(i.file_size for i in infos) <= limits.max_expanded_bytes, 'expanded workbook bytes')
        sheets = [i for i in infos if i.filename.startswith('xl/worksheets/') and i.filename.endswith('.xml')]
        limits.require(len(sheets) <= limits.max_worksheets, 'worksheets')
        total = 0
        for info in sheets:
            root = fromstring(archive.read(info))
            for dimension in root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}dimension'):
                _, _, columns, rows = range_boundaries(dimension.attrib['ref'])
                limits.require(rows <= limits.max_rows and columns <= limits.max_columns, 'declared worksheet dimensions')
                total += rows * columns
            limits.require(total <= limits.max_cells, 'declared workbook cells')
