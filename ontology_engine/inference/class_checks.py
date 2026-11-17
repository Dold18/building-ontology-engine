"""Deterministic plausibility checks for a proposed point class against documented source facts (FR-04/FR-08).

A HARD finding means the class is incompatible with the documented unit or BAS kind: the class is not accepted, is not
propagated to other rows, and the item is treated as unresolved (eligible for escalation). A SOFT finding is a warning that
travels with the candidate for review (role suggested by the name, writable sensor, unusual medium for the equipment family).
Checks never promote anything and never establish correctness; they only screen.
"""
import re
from ..vocabulary import canonical_point_class, point_role

VERSION = 'class-checks-v1'
QUANTITY_TOKENS = [('Temperature', 'temperature'), ('Air_Flow', 'air_flow'), ('Water_Flow', 'water_flow'), ('Humidity', 'humidity'),
                   ('CO2', 'concentration'), ('Static_Pressure', 'pressure'), ('Differential_Pressure', 'pressure'), ('Pressure', 'pressure'),
                   ('Position', 'ratio'), ('Speed', 'ratio'), ('Frequency', 'frequency')]
UNIT_QUANTITY = {'degF': 'temperature', 'ºF': 'temperature', 'cfm': 'air_flow', 'ft/min': 'velocity', 'percent': 'ratio', '%RH': 'humidity',
                 'ppm': 'concentration', 'in/wc': 'pressure', 'psi': 'pressure', 'gal/min': 'water_flow', 'gal': 'volume', 'V': 'voltage',
                 'BTU': 'energy', 'kW-hr': 'energy', 's': 'time'}
COMPATIBLE = {('humidity', 'ratio'), ('ratio', 'ratio')}
# Name-role cues. Words are matched case-insensitively; the short 'Sp'/'SP' suffix only in its exact BAS spellings, so
# that e.g. 'BSP' (building static pressure) is not read as a setpoint.
NAME_ROLES = [('alarm', re.compile(r'(alm|alarm|fail|fault|jam|broken)', re.I)),
              ('parameter', re.compile(r'((^|_)(min|max|Min|Max|MIN|MAX)(?=[A-Z_0-9]|$)|limit)')),
              ('setpoint', re.compile(r'(setpt|setpoint|stpt)', re.I)),
              ('setpoint', re.compile(r'([a-z0-9]Sp$|[a-z0-9]Sp_|_SP$|_Sp$)')),
              ('status', re.compile(r'(sts|status)($|_)', re.I)),
              ('command', re.compile(r'(cmd|command|ovrd|override|_out$|enable$|enb$)', re.I))]
COMPATIBLE_ROLES = {('setpoint', 'parameter'), ('parameter', 'setpoint'), ('command', 'mode'), ('status', 'mode'), ('mode', 'command'), ('mode', 'status')}
WATER_CLASSES = re.compile(r'(Water|Hot_Water|Chilled_Water)')
AIR_SIDE_FAMILIES = {'TB', 'AV', 'EV', 'VAV'}
AIR_ONLY_FAMILIES = {'AV', 'EV'}      # lab airflow / hood valves: no hydronic side at all


def class_quantity(cls):
    cls = canonical_point_class(cls)
    return next((q for token, q in QUANTITY_TOKENS if token in cls), None)


def name_role(name):
    return next((role for role, rx in NAME_ROLES if rx.search(name or '')), None)


def check(cls, name, units, kind, family=None):
    """[(code, 'hard'|'soft')] for a proposed class against one row's documented facts. Empty means no finding."""
    cls = canonical_point_class(cls)
    if cls in ('', 'unknown', 'Point'):
        return []
    out = []
    q, have = class_quantity(cls), UNIT_QUANTITY.get(units or '')
    role = point_role(cls)
    if q and have and q != have and (q, have) not in COMPATIBLE:
        out.append(('UNIT_QUANTITY_CONFLICT', 'hard'))
    if 'Boolean' in (kind or '') and role in ('sensor', 'setpoint', 'parameter') and q in ('temperature', 'air_flow', 'water_flow', 'pressure', 'humidity', 'concentration'):
        out.append(('BOOLEAN_KIND_FOR_MEASURED_QUANTITY', 'hard'))
    expected = name_role(name)
    if expected and role and expected != role and (expected, role) not in COMPATIBLE_ROLES and role != 'point':
        out.append(('ROLE_SUGGESTED_BY_NAME_' + expected.upper(), 'soft'))
    if 'Writable' in (kind or '') and role == 'sensor':
        out.append(('WRITABLE_KIND_FOR_SENSOR', 'soft'))
    if family in AIR_SIDE_FAMILIES and WATER_CLASSES.search(cls) and have != 'water_flow' and 'Valve' not in cls and 'Coil' not in cls:
        out.append(('WATER_CLASS_IN_AIR_TERMINAL_CONTEXT', 'hard' if (have is not None or family in AIR_ONLY_FAMILIES) else 'soft'))
    return out


def hard(findings):
    return [code for code, severity in findings if severity == 'hard']
