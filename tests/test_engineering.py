"""FR-03/04/05/06/08/11 development checks; synthetic, never sponsor evidence."""
import csv
from pathlib import Path
from types import SimpleNamespace
import pytest
from ontology_engine.dimensional import convert,QuantityError
from ontology_engine.engineering import evaluate,instant,rule_registry,EngineeringLedger
from ontology_engine.ingest import ingest
from ontology_engine.identity import uid
from ontology_engine.models import Assertion

@pytest.mark.parametrize('value,unit,kind,target,expected',[
 (32,'degF','temperature','degC',0),(100,'degC','temperature','degF',212),
 (18,'delta_degF','temperature_difference','delta_degC',10),(10,'delta_degC','temperature_difference','delta_K',10),
 (1,'kPa','pressure','Pa',1000),(1,'L/s','volume_flow','m3/s',.001),
 (2,'kW','power','W',2000),(1,'kWh','energy','J',3600000)])
def test_conversions(value,unit,kind,target,expected):assert convert(value,unit,kind,target)==pytest.approx(expected)

@pytest.mark.parametrize('value,unit,kind',[(1,'Pa','volume_flow'),(10,'degC','temperature_difference'),(10,'delta_degC','temperature'),('nan','K','temperature'),('inf','W','power'),('hello','Pa','pressure'),(1,'__import__(os)','power')])
def test_incompatible_or_corrupt_quantities(value,unit,kind):
 with pytest.raises(QuantityError):convert(value,unit,kind)

def write_csv(path,rows):
 with path.open('w',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for row in rows for k in row)));w.writeheader();w.writerows(rows)

def make_input(root):
 root.mkdir(exist_ok=True);(root/'SYNTHETIC.txt').write_text('SYNTHETIC / FOR TESTING ONLY')
 write_csv(root/'equipment.csv',[{'equipment_id':'HX-A','equipment_type':'Heat_Exchanger'},{'equipment_id':'PUMP-B','equipment_type':'Pump'},{'equipment_id':'LOAD-C','equipment_type':'Heat_Exchanger'}])
 write_csv(root/'connections.csv',[{'connection_id':'up','from_equipment':'PUMP-B','to_equipment':'HX-A','predicate':'feeds','medium':'water','source_description':'SYNTHETIC mechanical schedule'},{'connection_id':'down','from_equipment':'HX-A','to_equipment':'LOAD-C','predicate':'feeds','medium':'water','source_description':'SYNTHETIC mechanical schedule'}])
 points=[];measurements=[]
 for role,cls,kind,unit,value in [('inlet','Temperature_Sensor','temperature','degC',20),('outlet','Temperature_Sensor','temperature','degC',30),('flow','Water_Flow_Sensor','volume_flow','L/s',1),('power','Thermal_Power_Sensor','power','kW',41.8)]:
  points.append({'point_id':role+'-A','equipment_id':'HX-A','point_class':cls,'quantity_kind':kind,'units':unit,'measurement_role':role})
  for minute in range(3):
   measurements.append({'measurement_id':role+str(minute),'point_id':role+'-A','equipment_id':'HX-A','quantity_kind':kind,'value':str(value),'units':unit,'timestamp':f'2026-01-01T12:0{minute}:00+00:00','quality':'good','operating_state':'running','unsigned_flow':'true'})
 write_csv(root/'points.csv',points);write_csv(root/'measurements.csv',measurements)
 check={'check_id':'balance-A','equipment_id':'HX-A','inlet_point_id':'inlet-A','outlet_point_id':'outlet-A','flow_point_id':'flow-A','power_point_id':'power-A','medium':'water','mode':'heating','density':'1000','density_units':'kg/m3','specific_heat':'4180','specific_heat_units':'J/(kg*K)','assumptions':'steady_state_sensible_water','window_start':'2026-01-01T12:00:00+00:00','window_end':'2026-01-01T12:02:00+00:00','sample_period_seconds':'60','max_gap_seconds':'90','min_samples':'3','tolerance_fraction':'0.01'}
 write_csv(root/'thermal_checks.csv',[check]);return root

def calculate(root,mutate=None):
 records,_,_=ingest(root,scope='test');assertions=[]
 for r in records:
  if r.table_kind=='points':
   p=r.normalized_fields['point_id'];claim=uid('claim','test','equipment/HX-A','hasPoint','point/'+p)
   assertions.append(Assertion(assertion_id=p,subject_id='equipment/HX-A',predicate='hasPoint',object_id='point/'+p,supporting_source_ids=[r.source_id],inference_method='rule',explanation='Explicit source ownership',status='supported_inference',validation_results=[],run_id='test',claim_uid=claim))
 if mutate:mutate(records)
 return evaluate(records,assertions,SimpleNamespace(claims=[],observations=[],conflicts=[]),'test')

def thermal(ledger):return next(r for r in ledger.records if r.rule_id=='thermal.water_balance')

def test_independently_calculable_water_example(tmp_path):
 ledger=calculate(make_input(tmp_path/'input'));r=thermal(ledger)
 assert r.outcome=='pass';assert r.details['calculated_power_W']==pytest.approx([1000*4180*.001*(30-20)]*3)
 assert r.details['measured_power_W']==[41800]*3
 assert EngineeringLedger.model_validate_json(ledger.model_dump_json())==ledger
 assert all(r.source_ids and len(r.source_ids)==len(r.source_uids) for r in ledger.records)
 assert len({r.result_uid for r in ledger.records})==len(ledger.records)
 assert all(r.version and r.required_inputs and r.reason_codes for r in rule_registry())

@pytest.mark.parametrize('field,value,reason',[
 ('timestamp','','MISSING_TIMESTAMP'),('timestamp','2026-01-01T12:00:00','MISSING_TIMEZONE'),
 ('timestamp','2026-01-01T12:00:30+00:00','UNALIGNED_TIMESTAMPS'),
 ('operating_state','off','EQUIPMENT_NOT_RUNNING_OR_UNKNOWN'),('quality','bad','INVALID_MEASUREMENT'),
 ('value','nan','INVALID_MEASUREMENT'),('units','Pa','INVALID_MEASUREMENT')])
def test_measurement_prerequisite_abstention(tmp_path,field,value,reason):
 def change(records):next(r for r in records if r.table_kind=='measurements').normalized_fields[field]=value
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert reason in r.reason_codes

@pytest.mark.parametrize('role',['inlet','outlet','flow','power'])
def test_missing_sensor(tmp_path,role):
 def change(records):next(r for r in records if r.table_kind=='thermal_checks').normalized_fields[role+'_point_id']=''
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert 'MISSING_OR_AMBIGUOUS_'+role.upper()+'_POINT' in r.reason_codes

def test_gap_and_insufficient_sampling(tmp_path):
 def change(records):records[:]=[r for r in records if not (r.table_kind=='measurements' and r.normalized_fields['measurement_id']=='flow1')]
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert {'MEASUREMENT_GAP','INSUFFICIENT_FLOW_SAMPLES','UNALIGNED_TIMESTAMPS'}<=set(r.reason_codes)

def test_offsets_are_aligned_not_naively_compared(tmp_path):
 def change(records):
  for r in records:
   if r.table_kind=='measurements' and r.normalized_fields['quantity_kind']=='temperature':r.normalized_fields['timestamp']=r.normalized_fields['timestamp'].replace('12:','05:').replace('+00:00','-07:00')
 assert thermal(calculate(make_input(tmp_path/'input'),change)).outcome=='pass'

def test_timezone_disagreement_and_dst():
 with pytest.raises(QuantityError,match='TIMEZONE_OFFSET_MISMATCH'):instant('2026-01-01T12:00:00+00:00','America/Denver')
 with pytest.raises(QuantityError,match='AMBIGUOUS_LOCAL_TIME'):instant('2026-11-01T01:30:00','America/Denver')

def test_impossible_temperature_with_unknown_unsigned_flow_not_false_fault(tmp_path):
 def change(records):
  for r in records:
   if r.table_kind=='measurements' and r.normalized_fields['measurement_id']=='inlet0':r.normalized_fields['value']='-300'
 ledger=calculate(make_input(tmp_path/'input'),change)
 assert any(r.outcome=='fail' and 'BELOW_ABSOLUTE_ZERO' in r.reason_codes for r in ledger.records)
 assert thermal(ledger).outcome=='not_evaluable'

def test_thermal_mismatch_is_screening_not_fault(tmp_path):
 def change(records):
  for r in records:
   if r.table_kind=='measurements' and r.normalized_fields['quantity_kind']=='power':r.normalized_fields['value']='1'
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='fail';assert 'THERMAL_POWER_OUTSIDE_TOLERANCE' in r.reason_codes;assert 'fault' in r.action

def test_fahrenheit_equivalent_balance(tmp_path):
 def change(records):
  for r in records:
   if r.table_kind=='measurements' and r.normalized_fields['quantity_kind']=='temperature':
    f=r.normalized_fields;f['value']=str(float(f['value'])*1.8+32);f['units']='degF'
 assert thermal(calculate(make_input(tmp_path/'input'),change)).details['calculated_power_W']==pytest.approx([41800]*3)

@pytest.mark.parametrize('field,value,reason',[
 ('density','','MISSING_OR_INVALID_DENSITY'),('specific_heat','-4180','INVALID_FLUID_PROPERTIES'),
 ('medium','air','THERMAL_APPLICABILITY_NOT_ESTABLISHED'),('assumptions','','THERMAL_APPLICABILITY_NOT_ESTABLISHED'),
 ('window_start','','INVALID_OR_MISSING_WINDOW_CONFIGURATION'),('min_samples','1','INVALID_OR_MISSING_WINDOW_CONFIGURATION'),
 ('tolerance_fraction','NaN','INVALID_OR_MISSING_WINDOW_CONFIGURATION')])
def test_explicit_calculation_contract_required(tmp_path,field,value,reason):
 def change(records):next(r for r in records if r.table_kind=='thermal_checks').normalized_fields[field]=value
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert reason in r.reason_codes

def test_cooling_signed_thermal_power_and_electric_input_are_distinct(tmp_path):
 def change(records):
  for r in records:
   f=r.normalized_fields
   if r.table_kind=='thermal_checks':f['mode']='cooling'
   if r.table_kind=='measurements' and f['point_id']=='outlet-A':f['value']='10'
   if r.table_kind=='measurements' and f['point_id']=='power-A':f['value']='-41.8'
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='pass';assert r.details['calculated_power_W']==pytest.approx([-41800]*3)

def test_sensor_role_conflict_blocks_calculation_even_when_quantity_field_plausible(tmp_path):
 def change(records):next(r for r in records if r.table_kind=='points' and r.normalized_fields['point_id']=='flow-A').normalized_fields['point_class']='Pressure_Sensor'
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert 'INVALID_MEASUREMENT' in r.reason_codes

def test_electric_power_cannot_substitute_for_thermal_power(tmp_path):
 def change(records):next(r for r in records if r.table_kind=='points' and r.normalized_fields['point_id']=='power-A').normalized_fields['point_class']='Electric_Power_Sensor'
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert 'THERMAL_POWER_SENSOR_REQUIRED' in r.reason_codes

def test_duplicate_timestamp_and_absent_operating_state(tmp_path):
 def change(records):
  row=next(r for r in records if r.table_kind=='measurements');dup=row.model_copy(deep=True);dup.source_id='measurements.csv:extra';dup.source_record_uid='unique-extra';dup.normalized_fields['operating_state']='';records.append(dup)
 r=thermal(calculate(make_input(tmp_path/'input'),change));assert r.outcome=='not_evaluable';assert {'DUPLICATE_TIMESTAMP','EQUIPMENT_NOT_RUNNING_OR_UNKNOWN'}<=set(r.reason_codes)

def test_signed_flow_is_not_automatically_impossible_without_constraint(tmp_path):
 def change(records):
  row=next(r for r in records if r.table_kind=='measurements' and r.normalized_fields['quantity_kind']=='volume_flow');row.normalized_fields.update(value='-1',unsigned_flow='')
 ledger=calculate(make_input(tmp_path/'input'),change)
 assert not any('NEGATIVE_UNSIGNED_FLOW' in r.reason_codes for r in ledger.records)
 assert thermal(ledger).outcome=='not_evaluable' # declared inlet->outlet balance still requires positive directional flow

def test_date_only_is_not_a_measurement_timestamp():
 with pytest.raises(QuantityError,match='INVALID_TIMESTAMP'):instant('2026-01-01','UTC')
