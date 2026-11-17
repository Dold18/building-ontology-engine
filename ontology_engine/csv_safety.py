"""CSV text v1: apostrophe escape unsafe spreadsheet text; decode exactly once."""
VERSION = 'apostrophe-text-v1'


def encode_cell(value):
    # Numeric Python values remain numeric. Numeric-looking strings are still text.
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip()
    prefix = value[:len(value) - len(stripped)]
    if value.startswith("'") or stripped.startswith(('=', '+', '-', '@')) or any(c in prefix for c in '\t\r\n'):
        return "'" + value
    return value


def decode_cell(value):
    if isinstance(value, str) and value.startswith("'"):
        candidate = value[1:]
        if encode_cell(candidate) == value:
            return candidate
    return value


def decode_row(row):
    return {k: decode_cell(v) for k, v in row.items()}
