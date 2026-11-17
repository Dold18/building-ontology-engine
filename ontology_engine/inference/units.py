"""Narrow unit-glyph normalization that preserves the original text.

The accepted engine alias table maps U+00B0 spellings (e.g. a degree sign plus F) to degF but does not recognize the
masculine-ordinal variant U+00BA (the same two-character spelling with the wrong glyph), found in some historical point exports. This layer maps EXACTLY that variant and nothing else, and always keeps the source text.
"""
from dataclasses import dataclass

RULE_ID = 'unit-glyph-ordinal-degree-v1'
_EXACT = {'ºf': 'degF', 'ºc': 'degC'}  # after strip + casefold


@dataclass(frozen=True)
class UnitResult:
    original: str
    normalized: str
    rule_id: str
    changed: bool


def normalize_unit_variant(raw, engine_normalized=None):
    """Return the engine-normalized unit unless `raw` is exactly an ordinal-indicator degree spelling.

    `engine_normalized` is what the accepted engine produced (so '°F' -> 'degF' stays the engine's decision).
    """
    text = '' if raw is None else str(raw)
    base = text.strip() if engine_normalized is None else engine_normalized
    mapped = _EXACT.get(text.strip().casefold())
    if mapped is not None and text.strip() == text.strip().replace('º', 'º'):
        return UnitResult(text, mapped, RULE_ID, True)
    return UnitResult(text, base, 'engine-alias-table' if base != text.strip() else 'none', False)
