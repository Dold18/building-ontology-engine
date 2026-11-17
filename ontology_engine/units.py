"""Metadata consistency only: never infer missing units or convert readings."""
from .vocabulary import POINT_NAMES

UNIT_ALIASES = {'°c': 'degC', 'celsius': 'degC', 'degc': 'degC', '°f': 'degF',
                'fahrenheit': 'degF', 'degf': 'degF', 'pa': 'Pa', 'kpa': 'kPa',
                '%': 'percent', 'pct': 'percent', 'bool': 'boolean', 'kw': 'kW', 'kwh': 'kWh'}
UNITS = {'temperature': {'degC', 'degF', 'K'}, 'pressure': {'Pa', 'kPa', 'bar', 'psi', 'inH2O'},
         'position': {'percent', 'fraction'}, 'status': {'boolean'},
         'power': {'W', 'kW', 'MW'}, 'energy': {'Wh', 'kWh', 'MWh'},
         'flow': {'cfm', 'L/s', 'm3/s', 'm3/h'}}


def normalize_unit(value):
    return UNIT_ALIASES.get(value.strip().lower(), value.strip())


def unit_problem(fields, point_class=None):
    unit = normalize_unit(fields.get('units', ''))
    if not unit:
        return None
    cls = point_class or fields.get('point_class') or POINT_NAMES.get(fields.get('description', '').lower(), '')
    cls = cls.lower()
    quantity = next((q for q, term in [('temperature', 'temperature'), ('pressure', 'pressure'),
                ('position', 'position'), ('status', 'status'), ('power', 'power'),
                ('energy', 'energy'), ('flow', 'flow')] if term in cls), None)
    if quantity and unit not in UNITS[quantity]:
        return f'Unit {unit!r} is not in the supported {quantity} unit set; verify metadata, no conversion applied.'
    return None
