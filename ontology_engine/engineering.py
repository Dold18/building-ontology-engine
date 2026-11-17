"""FR-03/04/06: deterministic advisory engineering screening, not fault diagnosis."""
import json
import math
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Dict,List,Literal
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError
from .models import Model
from .identity import uid,unique_source_map
from .dimensional import convert,QuantityError
from .vocabulary import canonical_point_class,equipment_class
from .limits import Limits

VERSION='engineering-v1'
POLICY='engineering-screen-v1'
REGISTRY_PATH=Path(__file__).with_name('resources')/'engineering-rules-v1.json'

class RuleDefinition(Model):
    rule_id:str
    version:str
    applicability:str
    required_inputs:List[str]
    unit_dimensions:Dict[str,str]
    time_prerequisites:List[str]
    operating_prerequisites:List[str]
    outcomes:List[Literal['pass','fail','not_evaluable','blocked','error']]
    reason_codes:List[str]
    severity:str
    evidence_references:List[str]

class RuleResult(Model):
    result_uid:str
    rule_id:str
    rule_version:str
    site_scope:str
    target_id:str
    outcome:Literal['pass','fail','not_evaluable','blocked','error']
    reason_codes:List[str]
    severity:str
    source_ids:List[str]
    source_uids:List[str]
    claim_uids:List[str]
    observation_uids:List[str]
    conflict_uids:List[str]
    details:Dict[str,Any]
    action:str='Review cited source data/assumptions; screening does not establish an equipment fault.'

class EngineeringLedger(Model):
    schema_version:Literal['engineering-v1']=VERSION
    policy_version:Literal['engineering-screen-v1']=POLICY
    site_scope:str
    registry:List[RuleDefinition]
    records:List[RuleResult]
    summary:Dict[str,Any]


def rule_registry():return [RuleDefinition.model_validate(r) for r in json.loads(REGISTRY_PATH.read_text())['rules']]


def instant(text,tz=''):
    if not text:raise QuantityError('MISSING_TIMESTAMP')
    if 'T' not in text and ' ' not in text:raise QuantityError('INVALID_TIMESTAMP')
    try:t=datetime.fromisoformat(text.replace('Z','+00:00'))
    except (ValueError,TypeError):raise QuantityError('INVALID_TIMESTAMP') from None
    if not t.tzinfo:
        if not tz:raise QuantityError('MISSING_TIMEZONE')
        try:z=ZoneInfo(tz)
        except (ZoneInfoNotFoundError,ValueError):raise QuantityError('INVALID_TIMEZONE') from None
        # Ambiguous or nonexistent local civil times are unsafe without an explicit offset.
        a=t.replace(tzinfo=z,fold=0);b=t.replace(tzinfo=z,fold=1)
        if a.utcoffset()!=b.utcoffset():raise QuantityError('AMBIGUOUS_LOCAL_TIME')
        t=a
    elif tz:
        try:z=ZoneInfo(tz)
        except (ZoneInfoNotFoundError,ValueError):raise QuantityError('INVALID_TIMEZONE') from None
        if t.astimezone(z).utcoffset()!=t.utcoffset():raise QuantityError('TIMEZONE_OFFSET_MISMATCH')
    return t.astimezone(timezone.utc)


def class_quantity(cls):
    cls=canonical_point_class(cls)
    if 'Temperature' in cls:return 'temperature'
    if 'Pressure' in cls:return 'pressure'
    if cls in {'Air_Flow_Sensor','Water_Flow_Sensor'}:return 'volume_flow'
    if 'Power' in cls:return 'power'
    if 'Energy' in cls:return 'energy'
    if cls=='Fan_Status':return 'status'
    return None


def evaluate(records,assertions,bundle,scope,limits=None):
    """Pure, bounded recomputation from immutable source evidence and Phase A projection."""
    limits=limits or Limits();sources=unique_source_map(records);definitions=rule_registry()
    rules={r.rule_id:r for r in definitions};results=[];ops=0
    measurements=[r for r in records if r.table_kind=='measurements']
    checks=[r for r in records if r.table_kind=='thermal_checks']
    points=defaultdict(list);equipment=defaultdict(list);series=defaultdict(list)
    for r in records:
        if r.table_kind=='points':points[r.normalized_fields.get('point_id','')].append(r)
        if r.table_kind=='equipment':equipment[r.normalized_fields.get('equipment_id','')].append(r)
        if r.table_kind=='measurements':series[r.normalized_fields.get('point_id','')].append(r)
    entity_claims=defaultdict(list);conflicts_by_claim=defaultdict(list)
    for claim in bundle.claims:
        entity_claims[claim.subject_id].append(claim.claim_uid);entity_claims[claim.object_id].append(claim.claim_uid)
    for conflict in bundle.conflicts:
        for key in conflict.claim_uids:conflicts_by_claim[key].append(conflict)
    accepted={a.claim_uid for a in assertions if a.status in {'verified','supported_inference'}}
    by_claim={c.claim_uid:c for c in bundle.claims}
    point_quantities={}
    for a in assertions:
        if a.predicate=='type' and a.subject_id.startswith('point/') and a.status in {'verified','supported_inference'} and class_quantity(a.object_id):
            point_quantities[a.subject_id[6:]]=class_quantity(a.object_id)
    def charge(n=1):
        nonlocal ops
        ops+=n;limits.require(ops<=limits.max_engineering_operations,'engineering operations')
    def emit(rule,target,outcome,codes,refs=(),claims=(),details=None,conflicts=()):
        charge();limits.require(len(results)<limits.max_engineering_results,'engineering results')
        refs=sorted(set(refs));claims=sorted(set(claims));r=rules[rule]
        charge(len(refs)+len(claims)+sum(len(by_claim[c].observation_uids) for c in claims if c in by_claim))
        if not set(refs)<=sources.keys():raise ValueError('ENGINEERING_UNRESOLVED_CITATION')
        if not set(codes)<=set(r.reason_codes):raise ValueError('UNREGISTERED_ENGINEERING_REASON')
        results.append(RuleResult(result_uid=uid('engineering-result',scope,VERSION,rule,target,[sources[s].source_record_uid for s in refs]),rule_id=rule,
            rule_version=r.version,site_scope=scope,target_id=target,outcome=outcome,reason_codes=sorted(set(codes)),
            severity=r.severity,source_ids=refs,source_uids=[sources[s].source_record_uid for s in refs],claim_uids=claims,
            observation_uids=sorted({o for c in claims if c in by_claim for o in by_claim[c].observation_uids}),
            conflict_uids=sorted(set(conflicts)),details=details or {}))
    def claims_for(entity):return entity_claims.get(entity,[])
    point_dimension_errors={}
    # Validate explicit point quantity/class dimensions even without numeric telemetry.
    for rows in points.values():
        for r in rows:
            charge();f=r.normalized_fields;pid=f.get('point_id','');kind=f.get('quantity_kind') or class_quantity(f.get('point_class','')) or point_quantities.get(pid)
            codes=[];outcome='pass'
            if r.parse_status!='ok':outcome='blocked';codes=['INVALID_SOURCE_RECORD']
            elif not kind or not f.get('units'):outcome='not_evaluable';codes=['MISSING_QUANTITY_OR_UNIT']
            else:
                try:
                    convert('0',f['units'],kind)
                    q=class_quantity(f.get('point_class',''))
                    if q and q!=kind:raise QuantityError('SENSOR_ROLE_QUANTITY_MISMATCH')
                except QuantityError as exc:outcome='fail';codes=[exc.code]
            point_dimension_errors[r.source_id]=list(codes)
            emit('measurement.dimension',r.source_id,outcome,codes or ['DIMENSION_COMPATIBLE'],[r.source_id],claims_for('point/'+pid),{'quantity_kind':kind,'original_units':f.get('units','')})
    # Per-reading units/quality/time are tracked independently; no malformed row silently disappears.
    readings={};time_results={}
    for r in measurements:
        charge();f=r.normalized_fields;pid=f.get('point_id','');codes=[];value=None;kind=f.get('quantity_kind','');owner=points.get(pid,[])
        if r.parse_status!='ok':codes.append('INVALID_SOURCE_RECORD')
        if len(owner)!=1 or owner[0].parse_status!='ok':codes.append('POINT_IDENTITY_UNRESOLVED')
        else:
            codes.extend(point_dimension_errors.get(owner[0].source_id,[]))
            pf=owner[0].normalized_fields;expected=pf.get('quantity_kind') or class_quantity(pf.get('point_class','')) or point_quantities.get(pid)
            if not expected:codes.append('MISSING_QUANTITY_OR_UNIT')
            elif expected!=kind:codes.append('SENSOR_ROLE_QUANTITY_MISMATCH')
            try:
                convert('0',pf.get('units',''),expected)
            except QuantityError as exc:codes.append(exc.code)
        if f.get('quality','').lower()!='good':codes.append('MEASUREMENT_QUALITY_NOT_GOOD')
        try:value=convert(f.get('value',''),f.get('units',''),kind)
        except QuantityError as exc:codes.append(exc.code)
        refs=[r.source_id]+[p.source_id for p in owner]
        emit('measurement.dimension',r.source_id,'blocked' if codes else 'pass',codes or ['DIMENSION_COMPATIBLE'],refs,claims_for('point/'+pid),{'original_value':f.get('value',''),'original_units':f.get('units',''),'quantity_kind':kind,'normalized_value':value})
        readings[r.source_id]=(value,kind,codes)
        range_codes=[];range_out='not_evaluable'
        if not codes and kind=='temperature':range_out='fail' if value<0 else 'pass';range_codes=['BELOW_ABSOLUTE_ZERO'] if value<0 else ['ABSOLUTE_TEMPERATURE_NONNEGATIVE']
        elif not codes and kind in {'volume_flow','mass_flow'} and f.get('unsigned_flow','').lower()=='true':range_out='fail' if value<0 else 'pass';range_codes=['NEGATIVE_UNSIGNED_FLOW'] if value<0 else ['UNSIGNED_FLOW_NONNEGATIVE']
        else:range_codes=['RANGE_NOT_ESTABLISHED']
        emit('measurement.range',r.source_id,range_out,range_codes,refs,claims_for('point/'+pid),{'normalized_value':value,'quantity_kind':kind})
        try:
            t=instant(f.get('timestamp',''),f.get('timezone',''));time_results[r.source_id]=(t,None)
            emit('measurement.clock',r.source_id,'pass',['TIMESTAMP_RESOLVED'],[r.source_id],details={'utc':t.isoformat()})
        except QuantityError as exc:
            time_results[r.source_id]=(None,exc.code);emit('measurement.clock',r.source_id,'not_evaluable',[exc.code],[r.source_id])
    # Topology truth is separate from schema validity; use documented eligibility and Phase A constraints.
    obs={o.assertion_id:o for o in bundle.observations if o.assertion_id}
    for a in assertions:
        charge()
        if a.predicate not in {'feeds','hasPart','hasPoint'}:continue
        o=obs.get(a.assertion_id);conflicts=conflicts_by_claim.get(a.claim_uid,[])
        supported=[c for c in conflicts if c.kind in {'flow_direction','physical_media','exclusive_ownership'}]
        refs=set(a.supporting_source_ids);refs.update(s for c in supported for s in c.source_ids)
        if supported:out='fail';codes=['SUPPORTED_TOPOLOGY_CONFLICT']
        elif o and o.documentary_supported:
            out='pass';codes=['DOCUMENTARY_RELATION_SUPPORTED']
        else:out='not_evaluable';codes=['CONNECTION_OR_OWNERSHIP_NOT_ESTABLISHED']
        emit('topology.evidence',a.assertion_id,out,codes,refs,[a.claim_uid],{'predicate':a.predicate,'subject_id':a.subject_id,'object_id':a.object_id,'assertion_status':a.status,'evidence_truth':'not_evaluable'},[c.conflict_uid for c in supported])
    for check in checks:
        charge();f=check.normalized_fields;refs={check.source_id};codes=[];eid=f.get('equipment_id','');details={'equation':'Qdot = rho * cp * Vdot * (Tout - Tin)','result_units':'W','mode':f.get('mode',''),'assumptions':f.get('assumptions',''),'equipment_id':eid}
        def miss(code):
            if code not in codes:codes.append(code)
        if check.parse_status!='ok':miss('INVALID_SOURCE_RECORD')
        eq=equipment.get(eid,[])
        if len(eq)!=1 or eq[0].parse_status!='ok':miss('EQUIPMENT_IDENTITY_UNRESOLVED')
        else:
            refs.add(eq[0].source_id)
            if equipment_class(eq[0].normalized_fields.get('equipment_type',''))!='Heat_Exchanger':miss('THERMAL_APPLICABILITY_NOT_ESTABLISHED')
        if f.get('medium','').lower()!='water' or f.get('assumptions')!='steady_state_sensible_water':miss('THERMAL_APPLICABILITY_NOT_ESTABLISHED')
        if f.get('mode') not in {'heating','cooling'}:miss('MODE_NOT_ESTABLISHED')
        numbers={}
        for name,kind,unit in [('density','density','kg/m3'),('specific_heat','specific_heat','J/(kg*K)')]:
            try:
                numbers[name]=convert(f.get(name,''),f.get(name+'_units',''),kind)
                if numbers[name]<=0:miss('INVALID_FLUID_PROPERTIES')
            except QuantityError:miss('MISSING_OR_INVALID_'+name.upper())
        try:
            start=instant(f.get('window_start',''),f.get('timezone',''));end=instant(f.get('window_end',''),f.get('timezone',''))
            if end<=start:raise ValueError()
            sample=float(f.get('sample_period_seconds',''));gap=float(f.get('max_gap_seconds',''));tol=float(f.get('tolerance_fraction',''));minimum=int(f.get('min_samples',''))
            if not all(math.isfinite(x) for x in (sample,gap,tol)) or sample<=0 or gap<sample or not 0<=tol<=0.5 or not 2<=minimum<=limits.max_rows:raise ValueError()
        except (ValueError,QuantityError,OverflowError):
            miss('INVALID_OR_MISSING_WINDOW_CONFIGURATION');start=end=None;sample=gap=tol=0;minimum=2
        aligned={}
        for role,kind in [('inlet','temperature'),('outlet','temperature'),('flow','volume_flow'),('power','power')]:
            pid=f.get(role+'_point_id','');pr=points.get(pid,[])
            if len(pr)!=1 or pr[0].parse_status!='ok':miss('MISSING_OR_AMBIGUOUS_'+role.upper()+'_POINT');continue
            pf=pr[0].normalized_fields;refs.add(pr[0].source_id)
            if role=='power' and canonical_point_class(pf.get('point_class',''))!='Thermal_Power_Sensor':miss('THERMAL_POWER_SENSOR_REQUIRED')
            if pf.get('equipment_id')!=eid or pf.get('measurement_role')!=role or (pf.get('quantity_kind') or class_quantity(pf.get('point_class','')))!=kind:
                miss('DOCUMENTED_ROLE_OR_OWNERSHIP_MISMATCH')
            ownership=uid('claim',scope,'equipment/'+eid,'hasPoint','point/'+pid)
            if ownership not in accepted:miss('ACCEPTED_OWNERSHIP_REQUIRED')
            values={}
            for row in series.get(pid,[]):
                charge();t,err=time_results[row.source_id]
                if err:refs.add(row.source_id);miss(err);continue
                if start is None or not start<=t<=end:continue
                refs.add(row.source_id)
                value,q,bad=readings[row.source_id]
                if bad or q!=kind:miss('INVALID_MEASUREMENT');continue
                if q=='temperature' and value<0 or q=='volume_flow' and value<0:miss('PHYSICALLY_INVALID_MEASUREMENT');continue
                if row.normalized_fields.get('equipment_id')!=eid:miss('MEASUREMENT_EQUIPMENT_MISMATCH')
                if row.normalized_fields.get('operating_state','').lower()!='running':miss('EQUIPMENT_NOT_RUNNING_OR_UNKNOWN')
                if t in values:miss('DUPLICATE_TIMESTAMP')
                values[t]=value
            aligned[role]=values
            if len(values)<minimum:miss('INSUFFICIENT_'+role.upper()+'_SAMPLES')
            times=sorted(values)
            if times and start and (times[0]!=start or times[-1]!=end):miss('INCOMPLETE_WINDOW_COVERAGE')
            if any((b-a).total_seconds()>gap for a,b in zip(times,times[1:])):miss('MEASUREMENT_GAP')
            if any(abs((b-a).total_seconds()-sample)>1e-6 for a,b in zip(times,times[1:])):miss('IRREGULAR_SAMPLE_CADENCE')
        if len(aligned)==4 and len({tuple(sorted(v)) for v in aligned.values()})!=1:miss('UNALIGNED_TIMESTAMPS')
        if codes:
            emit('thermal.water_balance',check.source_id,'not_evaluable',codes,refs,claims_for('equipment/'+eid),details);continue
        calculated=[];measured=[];residual=[]
        for t in sorted(aligned['inlet']):
            charge();delta=aligned['outlet'][t]-aligned['inlet'][t];power=numbers['density']*numbers['specific_heat']*aligned['flow'][t]*delta
            # Signed heat into fluid: heating positive, cooling negative; comparator is documented thermal power, not electric input.
            if not math.isfinite(power):miss('NONFINITE_CALCULATION');break
            calculated.append(power);measured.append(aligned['power'][t]);residual.append(abs(power-aligned['power'][t])/max(abs(power),abs(aligned['power'][t]),1.0))
        if codes:
            emit('thermal.water_balance',check.source_id,'blocked',codes,refs,claims_for('equipment/'+eid),details);continue
        sign=1 if f['mode']=='heating' else -1
        reasons=[]
        if any(sign*v<0 for v in calculated+measured):reasons.append('HEATING_COOLING_SIGN_MISMATCH')
        if any(v>tol for v in residual):reasons.append('THERMAL_POWER_OUTSIDE_TOLERANCE')
        details.update(density_kg_m3=numbers['density'],specific_heat_J_kgK=numbers['specific_heat'],calculated_power_W=calculated,measured_power_W=measured,
            relative_residuals=residual,tolerance_fraction=tol,samples=len(calculated),window_start=start.isoformat(),window_end=end.isoformat(),
            timestamp_alignment='exact UTC',equipment_state='running',measurement_quality='good')
        emit('thermal.water_balance',check.source_id,'fail' if reasons else 'pass',reasons or ['THERMAL_BALANCE_WITHIN_TOLERANCE'],refs,claims_for('equipment/'+eid),details)
    if not checks:emit('thermal.water_balance','run','not_evaluable',['NO_THERMAL_CHECK_CONFIGURATION'])
    counts=dict(Counter(r.outcome for r in results));physics=[r for r in results if r.rule_id in {'measurement.range','thermal.water_balance'}]
    summary={'result_denominator':len(results),'outcomes':counts,
        'rule_outcomes':{r.rule_id:dict(Counter(f.outcome for f in results if f.rule_id==r.rule_id)) for r in definitions},'selection_operations':ops,'physics':'screened' if any(r.outcome in {'pass','fail'} for r in physics) else 'not_evaluable',
        'evidence_truth':'not_evaluable','fault_diagnosis':'not_performed','disposition_effect':'none_advisory_only','thermal_check_denominator':len(checks)}
    return EngineeringLedger(site_scope=scope,registry=definitions,records=results,summary=summary)
