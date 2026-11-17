"""Conservative supported subset of Brick 1.4.4, not a new vocabulary."""
from rdflib import Namespace
BRICK = Namespace('https://brickschema.org/schema/Brick#')
EQUIPMENT = {'Equipment', 'AHU', 'VAV', 'Fan', 'Pump', 'Boiler', 'Chiller', 'Heat_Exchanger', 'Meter', 'Electrical_Meter', 'HVAC_Zone'}
LOCATIONS = {'HVAC_Zone'}
# Legacy EQUIPMENT is the supported schedule class set, not RDF superclass membership.
SCHEDULE_CLASSES = EQUIPMENT
POINTS = {'Point', 'Supply_Air_Temperature_Sensor', 'Outside_Air_Temperature_Sensor',
          'Zone_Air_Temperature_Sensor', 'Temperature_Sensor', 'Pressure_Sensor',
          'Differential_Pressure_Sensor', 'Fan_Status', 'Damper_Position_Sensor',
          'Air_Flow_Sensor', 'Water_Flow_Sensor', 'Thermal_Power_Sensor', 'Electric_Power_Sensor', 'Electric_Energy_Sensor'}
CLASS_ALIASES = {'Supply_Fan_Status': 'Fan_Status',
                 'Electrical_Power_Sensor': 'Electric_Power_Sensor',
                 'Electrical_Energy_Sensor': 'Electric_Energy_Sensor'}


def canonical_point_class(value):
    """Owner-authorized legacy spelling migration; never alters source records."""
    local = value.removeprefix('brick:')
    return CLASS_ALIASES.get(local, local)
PREDICATES = {'type', 'feeds', 'hasPoint', 'hasPart'}
EQUIPMENT_NAMES = {'air handling unit': 'AHU', 'air handler': 'AHU', 'vav terminal': 'VAV',
                   'variable air volume terminal': 'VAV', 'heat exchanger': 'Heat_Exchanger'}
POINT_NAMES = {'supply air temperature': 'Supply_Air_Temperature_Sensor',
               'outside air temperature': 'Outside_Air_Temperature_Sensor',
               'zone temperature': 'Zone_Air_Temperature_Sensor',
               'zone temp': 'Zone_Air_Temperature_Sensor',
               'supply fan status': 'Fan_Status',
               'airflow damper position': 'Damper_Position_Sensor',
               'pressure reading': 'Pressure_Sensor',
               'differential pressure': 'Differential_Pressure_Sensor'}

def equipment_class(value):
    value = value.removeprefix('brick:')
    return value if value in SCHEDULE_CLASSES else EQUIPMENT_NAMES.get(value.lower(), 'Equipment')


# ---- Owner-authorized extension (TASK_006 Phase 3D, 2026-11-03) ----------------------------------------------------
# Released, non-deprecated Brick 1.4.4 classes (verified against the pinned resource by tests). Deprecated names are replaced
# by the release's own `brick:isReplacedBy` target (e.g. Target_Zone_Air_Temperature_Setpoint). New classes are allowed as
# CANDIDATES; the evidence policy, not vocabulary membership, decides acceptance.
VOCABULARY_VERSION = 'brick-1.4.4-subset-v2'
POINT_ROLES = {
    'Air_Flow_Alarm': 'alarm', 'Air_Flow_Sensor': 'sensor', 'Air_Flow_Setpoint': 'setpoint', 'Air_Temperature_Sensor': 'sensor',
    'Air_Temperature_Setpoint': 'setpoint', 'Alarm': 'alarm', 'Building_Air_Static_Pressure_Sensor': 'sensor', 'CO2_Level_Sensor': 'sensor',
    'CO2_Sensor': 'sensor', 'CO2_Setpoint': 'setpoint', 'Chilled_Water_Differential_Pressure_Sensor': 'sensor', 'Communication_Loss_Alarm': 'alarm',
    'Cooling_Temperature_Setpoint': 'setpoint', 'Damper_Command': 'command', 'Damper_Position_Command': 'command', 'Damper_Position_Sensor': 'sensor',
    'Damper_Position_Status': 'status', 'Deadband_Setpoint': 'setpoint', 'Differential_Pressure_Sensor': 'sensor', 'Differential_Pressure_Setpoint': 'setpoint',
    'Disable_Command': 'command', 'Discharge_Air_Temperature_Sensor': 'sensor', 'Discharge_Air_Temperature_Setpoint': 'setpoint',
    'Effective_Air_Temperature_Setpoint': 'setpoint', 'Electric_Energy_Sensor': 'sensor', 'Electric_Power_Sensor': 'sensor', 'Enable_Command': 'command',
    'Enable_Status': 'status', 'Entering_Hot_Water_Temperature_Sensor': 'sensor', 'Exhaust_Air_Flow_Sensor': 'sensor', 'Failure_Alarm': 'alarm',
    'Fan_Command': 'command', 'Fan_Speed_Command': 'command', 'Fan_Status': 'status', 'Freeze_Status': 'status', 'Frequency_Command': 'command',
    'Heating_Temperature_Setpoint': 'setpoint', 'High_Temperature_Alarm': 'alarm', 'Hot_Water_Differential_Pressure_Sensor': 'sensor',
    'Hot_Water_Differential_Pressure_Setpoint': 'setpoint', 'Hot_Water_Flow_Sensor': 'sensor', 'Humidity_Sensor': 'sensor', 'Humidity_Setpoint': 'setpoint',
    'Lead_Lag_Command': 'command', 'Leaving_Hot_Water_Temperature_Sensor': 'sensor', 'Low_Air_Flow_Alarm': 'alarm', 'Low_Temperature_Alarm': 'alarm',
    'Maintenance_Required_Alarm': 'alarm', 'Max_Air_Flow_Setpoint_Limit': 'parameter', 'Max_Cooling_Supply_Air_Flow_Setpoint_Limit': 'parameter',
    'Max_Heating_Supply_Air_Flow_Setpoint_Limit': 'parameter', 'Min_Air_Flow_Setpoint_Limit': 'parameter', 'Min_Cooling_Supply_Air_Flow_Setpoint_Limit': 'parameter',
    'Min_Heating_Supply_Air_Flow_Setpoint_Limit': 'parameter', 'Mixed_Air_Temperature_Sensor': 'sensor', 'Mode_Command': 'command', 'Mode_Status': 'status',
    'Occupancy_Command': 'command', 'Occupancy_Sensor': 'sensor', 'Occupancy_Status': 'status', 'Occupied_Air_Temperature_Setpoint': 'setpoint',
    'Occupied_Cooling_Temperature_Setpoint': 'setpoint', 'Occupied_Heating_Temperature_Setpoint': 'setpoint', 'On_Off_Command': 'command',
    'On_Off_Status': 'status', 'Outside_Air_Humidity_Sensor': 'sensor', 'Outside_Air_Temperature_Sensor': 'sensor', 'Override_Command': 'command',
    'Point': 'point', 'Position_Sensor': 'sensor', 'Pressure_Sensor': 'sensor', 'Pump_Command': 'command', 'Pump_Status': 'status',
    'Relative_Humidity_Sensor': 'sensor', 'Reset_Command': 'command', 'Return_Air_Humidity_Sensor': 'sensor', 'Return_Air_Temperature_Sensor': 'sensor',
    'Run_Enable_Command': 'command', 'Run_Status': 'status', 'Sensor_Failure_Alarm': 'alarm', 'Smoke_Detection_Alarm': 'alarm', 'Speed_Command': 'command',
    'Speed_Mode_Status': 'status', 'Start_Stop_Command': 'command', 'Start_Stop_Status': 'status', 'Static_Pressure_Sensor': 'sensor',
    'Static_Pressure_Setpoint': 'setpoint', 'Supply_Air_Flow_Sensor': 'sensor', 'Supply_Air_Flow_Setpoint': 'setpoint',
    'Supply_Air_Static_Pressure_Sensor': 'sensor', 'Supply_Air_Static_Pressure_Setpoint': 'setpoint', 'Supply_Air_Temperature_Sensor': 'sensor',
    'Supply_Air_Temperature_Setpoint': 'setpoint', 'Target_Zone_Air_Temperature_Setpoint': 'setpoint', 'Temperature_Deadband_Setpoint': 'setpoint',
    'Temperature_Sensor': 'sensor', 'Temperature_Setpoint': 'setpoint', 'Thermal_Power_Sensor': 'sensor', 'Unoccupied_Air_Temperature_Setpoint': 'setpoint',
    'Unoccupied_Cooling_Temperature_Setpoint': 'setpoint', 'Unoccupied_Heating_Temperature_Setpoint': 'setpoint', 'Valve_Command': 'command',
    'Valve_Position_Command': 'command', 'Valve_Position_Sensor': 'sensor', 'Valve_Status': 'status', 'Velocity_Pressure_Sensor': 'sensor',
    'Water_Flow_Sensor': 'sensor', 'Water_Temperature_Sensor': 'sensor', 'Zone_Air_Humidity_Sensor': 'sensor', 'Zone_Air_Temperature_Sensor': 'sensor'}
MODE_CLASSES = {'Mode_Command', 'Mode_Status', 'Speed_Mode_Status', 'Occupancy_Command', 'Occupancy_Status'}
EQUIPMENT_EXTENSION = {'Terminal_Unit', 'Variable_Air_Volume_Box_With_Reheat', 'Fan_Coil_Unit', 'Exhaust_Fan', 'Supply_Fan', 'Return_Fan',
                       'Hot_Water_Pump', 'Chilled_Water_Pump', 'Radiator', 'Fume_Hood', 'Damper', 'Valve', 'Hot_Water_Valve', 'Chilled_Water_Valve',
                       'Coil', 'Cooling_Coil', 'Heating_Coil', 'Chilled_Water_Coil', 'Hot_Water_Coil'}
SYSTEMS = {'Hot_Water_System', 'Chilled_Water_System', 'Air_System', 'Water_System', 'Heating_Ventilation_Air_Conditioning_System',
           'Ventilation_Air_System', 'Steam_System'}
POINTS |= set(POINT_ROLES)
EQUIPMENT |= EQUIPMENT_EXTENSION
TYPE_CLASSES = POINTS | EQUIPMENT | SYSTEMS


def point_role(cls):
    """sensor / setpoint / parameter / command / status / alarm / point, plus 'mode' for mode and occupancy classes."""
    cls = canonical_point_class(cls)
    return 'mode' if cls in MODE_CLASSES else POINT_ROLES.get(cls)
