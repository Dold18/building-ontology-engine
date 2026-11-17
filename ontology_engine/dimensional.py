"""Bounded, explicit quantity-kind conversions; untrusted units are never expressions."""
import math
from functools import lru_cache
import pint

# Explicit spellings avoid evaluating arbitrary expressions supplied by source files.
UNITS = {
 'degC':('temperature','degC'), 'degF':('temperature','degF'), 'K':('temperature','kelvin'),
 'delta_degC':('temperature_difference','delta_degC'), 'delta_degF':('temperature_difference','delta_degF'),
 'delta_K':('temperature_difference','kelvin'),
 'Pa':('pressure','pascal'), 'kPa':('pressure','kilopascal'), 'bar':('pressure','bar'), 'psi':('pressure','psi'),
 'm3/s':('volume_flow','meter**3/second'), 'm3/h':('volume_flow','meter**3/hour'),
 'L/s':('volume_flow','liter/second'), 'cfm':('volume_flow','foot**3/minute'),
 'kg/s':('mass_flow','kilogram/second'),
 'W':('power','watt'), 'kW':('power','kilowatt'), 'MW':('power','megawatt'),
 'J':('energy','joule'), 'Wh':('energy','watt_hour'), 'kWh':('energy','kilowatt_hour'), 'MWh':('energy','megawatt_hour'),
 'kg/m3':('density','kilogram/meter**3'), 'J/(kg*K)':('specific_heat','joule/kilogram/kelvin'),
 'kJ/(kg*K)':('specific_heat','kilojoule/kilogram/kelvin'), 'boolean':('status','dimensionless')}
TARGETS={'temperature':'K','temperature_difference':'delta_K','pressure':'Pa','volume_flow':'m3/s',
 'mass_flow':'kg/s','power':'W','energy':'J','density':'kg/m3','specific_heat':'J/(kg*K)','status':'boolean'}

class QuantityError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)

@lru_cache(maxsize=1)
def registry():return pint.UnitRegistry()

def convert(value,unit,kind,target=None):
    if kind not in TARGETS:raise QuantityError('UNKNOWN_QUANTITY_KIND')
    if unit not in UNITS:raise QuantityError('UNKNOWN_UNIT')
    destination=target or TARGETS[kind]
    if UNITS[unit][0]!=kind or destination not in UNITS or UNITS[destination][0]!=kind:
        raise QuantityError('DIMENSION_OR_TEMPERATURE_KIND_MISMATCH')
    try:number=float(value)
    except (ValueError,TypeError,OverflowError):raise QuantityError('INVALID_NUMERIC_VALUE') from None
    if not math.isfinite(number):raise QuantityError('NONFINITE_VALUE')
    if kind=='status' and number not in (0,1):raise QuantityError('INVALID_STATUS_VALUE')
    result=float(registry().Quantity(number,UNITS[unit][1]).to(UNITS[destination][1]).magnitude)
    if not math.isfinite(result):raise QuantityError('NONFINITE_VALUE')
    return result
