"""Stable serialization of generated test/derived workbooks, never original source files."""
import re
from io import BytesIO
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED


def save_reproducible_workbook(workbook, path):
    buffer = BytesIO()
    workbook.save(buffer)
    with ZipFile(BytesIO(buffer.getvalue())) as original:
        with ZipFile(path, 'w', ZIP_DEFLATED) as frozen:
            for name in sorted(original.namelist()):
                content = original.read(name)
                if name == 'docProps/core.xml':
                    content = re.sub(rb'(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)',
                                     rb'\g<1>2000-01-01T00:00:00Z\g<2>', content)
                info = ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                frozen.writestr(info, content)
