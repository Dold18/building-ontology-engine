"""Phase A pure disposition tests; all records/assertions are synthetic development data."""
import pytest
from ontology_engine.models import SourceRecord, Assertion
from ontology_engine.identity import uid
from ontology_engine.candidates import evaluate, project, CandidateBundle
from ontology_engine.limits import Limits


def record(key,kind,**fields):
    return SourceRecord(source_id=key,source_record_uid=uid('src','synthetic',key),source_file='SYNTHETIC.csv',
        source_type='csv',source_location=key,source_hash='0'*64,table_kind=kind,raw_value=[],raw_record={},
        normalized_fields=fields,parse_status='ok')


def observation(n,s,p,o,refs,status='supported_inference',method='direct_document',explanation='Supplied metadata',medium=None):
    return Assertion(assertion_id=f'a{n}',observation_uid=f'o{n}',claim_uid=uid('claim','site',s,p,o),
        subject_id=s,predicate=p,object_id=o,supporting_source_ids=refs,original_source_ids=refs,
        inference_method=method,explanation=explanation,status=status,validation_results=[],run_id='run',physical_medium=medium)


def equipment():return [record('e1','equipment',equipment_id='E',equipment_type='AHU'),record('e2','equipment',equipment_id='F',equipment_type='Pump')]


def test_same_claim_two_distinct_sources_keeps_observations_and_site_isolation():
    sources=equipment()+[record(k,'connections',from_equipment='E',to_equipment='F') for k in ('s1','s2')]
    rows=[observation(n,'equipment/E','feeds','equipment/F',[s]) for n,s in enumerate(('s1','s2'))]
    a=evaluate(rows,sources,'site-A','run');b=evaluate(rows,sources,'site-B','run')
    assert len(a.claims)==1 and len(a.observations)==2 and a.claims[0].status=='supported_inference'
    assert a.claims[0].claim_uid!=b.claims[0].claim_uid
    assert a.observations[0].resolved_source_uids!=a.observations[1].resolved_source_uids
    assert CandidateBundle.model_validate_json(a.model_dump_json())==a
    assert rows[0].status=='supported_inference'


@pytest.mark.parametrize('bad',['missing','irrelevant','blank','unresolved'])
def test_unsupported_observation_cannot_defeat_supported_claim(bad):
    sources=equipment()+[record('s','connections',from_equipment='E',to_equipment='F')]
    good=observation(1,'equipment/E','feeds','equipment/F',['s'])
    refs={'missing':[],'irrelevant':['e1'],'blank':['s'],'unresolved':['s','ghost']}[bad]
    other=observation(2,'equipment/E','feeds','equipment/F',refs,explanation='' if bad=='blank' else 'Not evidence')
    value=evaluate([good,other],sources,'site','run')
    assert value.claims[0].status=='supported_inference' and not value.conflicts
    assert not value.observations[1].acceptance_eligible
    assert value.observations[1].original_source_ids==refs
    if bad=='unresolved':assert value.observations[1].unresolved_source_ids==['ghost']


def test_supported_class_alternatives_withheld_without_owl_disjointness_or_winner():
    sources=equipment()+[record('p','points',point_id='P',point_class='Temperature_Sensor',description='Pressure Reading',equipment_id='E')]
    rows=[observation(i,'point/P','type',c,['p'],status='needs_review',method='llm') for i,c in enumerate(('Temperature_Sensor','Pressure_Sensor'))]
    value=evaluate(rows,sources,'site','run')
    assert all(o.documentary_supported for o in value.observations)
    assert len(value.conflicts)==1 and value.conflicts[0].kind=='classification'
    assert all(c.status=='needs_review' and len(c.conflicting_claim_uids)==1 for c in value.claims)
    assert all(a.status=='needs_review' for a in project(rows,value))


def test_subclass_and_generic_classes_are_compatible():
    sources=equipment()+[record('p','points',point_id='P',point_class='Temperature_Sensor',description='Supply Air Temperature')]
    rows=[observation(i,'point/P','type',c,['p'],status='needs_review') for i,c in enumerate(('Point','Temperature_Sensor','Supply_Air_Temperature_Sensor'))]
    assert not evaluate(rows,sources,'site','run').conflicts


@pytest.mark.parametrize('qualifier',['none','exclusive','other-scope','different-medium'])
def test_opposing_flow_requires_explicit_common_single_direction_scope(qualifier):
    fields={'connection_scope':'loop-A','direction_exclusive':'true'} if qualifier!='none' else {}
    sources=equipment()+[record('s1','connections',from_equipment='E',to_equipment='F',medium='water',**fields),record('s2','connections',from_equipment='F',to_equipment='E',medium='air' if qualifier=='different-medium' else 'water',**fields)]
    if qualifier=='other-scope':sources[-1].normalized_fields['connection_scope']='loop-B'
    rows=[observation(1,'equipment/E','feeds','equipment/F',['s1'],medium='water'),observation(2,'equipment/F','feeds','equipment/E',['s2'],medium='air' if qualifier=='different-medium' else 'water')]
    value=evaluate(rows,sources,'site','run')
    assert bool(value.conflicts)==(qualifier=='exclusive')
    if value.conflicts:
        assert value.conflicts[0].kind=='flow_direction' and all(c.status=='contradicted' for c in value.claims)
        assert all(a.status=='contradicted' for a in project(rows,value))


@pytest.mark.parametrize('exclusive',[False,True])
def test_shared_point_is_not_automatically_exclusive(exclusive):
    sources=equipment()+[record(k,'points',point_id='P',equipment_id=e,ownership_scope='physical-channel',ownership_exclusive=str(exclusive).lower()) for k,e in [('p1','E'),('p2','F')]]
    rows=[observation(1,'equipment/E','hasPoint','point/P',['p1']),observation(2,'equipment/F','hasPoint','point/P',['p2'])]
    result=evaluate(rows,sources,'site','run')
    assert bool(result.conflicts)==exclusive
    if exclusive:assert result.conflicts[0].kind=='exclusive_ownership'


@pytest.mark.parametrize('same_scope',[True,False])
def test_media_only_conflict_on_explicit_single_medium_connection(same_scope):
    sources=equipment()+[record(k,'connections',from_equipment='E',to_equipment='F',connection_scope=scope,medium_exclusive='true',medium=m) for k,scope,m in [('s1','loop-A','water'),('s2','loop-A' if same_scope else 'loop-B','air')]]
    rows=[observation(i,'equipment/E','feeds','equipment/F',[s],medium=m) for i,s,m in [(1,'s1','water'),(2,'s2','air')]]
    result=evaluate(rows,sources,'site','run')
    assert bool(result.conflicts)==same_scope
    if same_scope:assert result.conflicts[0].kind=='physical_media' and len(result.claims)==1 and result.claims[0].status=='contradicted'


def test_units_conflict_is_not_physical_fault_or_verification():
    sources=equipment()+[record('p','points',point_id='P',point_class='Electric_Power_Sensor',units='degC')]
    a=observation(1,'point/P','type','Electric_Power_Sensor',['p'])
    result=evaluate([a],sources,'site','run')
    assert result.conflicts[0].kind=='unit_type' and result.claims[0].status=='contradicted'
    a.status='verified'
    value=evaluate([a],sources,'site','run')
    assert not any(c.status=='verified' for c in value.claims)


def test_bounds_and_duplicate_observation_fail_closed():
    sources=equipment();rows=[observation(i,'equipment/E','type','AHU',['e1']) for i in range(3)]
    with pytest.raises(ValueError,match='candidate observations'):evaluate(rows,sources,'site','run',Limits(max_candidate_observations=2))
    with pytest.raises(ValueError,match='conflict comparisons'):evaluate(rows,sources,'site','run',Limits(max_conflict_comparisons=1))
    with pytest.raises(ValueError,match='OBSERVATION_ID_COLLISION'):evaluate([rows[0],rows[0]],sources,'site','run')


def test_unrelated_circuit_observation_not_falsely_labeled_contradicted():
    sources=equipment()+[record('s1','connections',from_equipment='E',to_equipment='F',medium='water',connection_scope='bad-loop',direction_exclusive='true'),
        record('s2','connections',from_equipment='F',to_equipment='E',medium='water',connection_scope='bad-loop',direction_exclusive='true'),
        record('s3','connections',from_equipment='E',to_equipment='F',medium='water',connection_scope='other-loop',direction_exclusive='true')]
    rows=[observation(1,'equipment/E','feeds','equipment/F',['s1'],medium='water'),
          observation(2,'equipment/F','feeds','equipment/E',['s2'],medium='water'),
          observation(3,'equipment/E','feeds','equipment/F',['s3'],medium='water')]
    bundle=evaluate(rows,sources,'site','run')
    assert bundle.conflicts[0].observation_uids==['o1','o2']
    assert bundle.observations[-1].status=='needs_review' and 'CLAIM_CONTEXT_DISPUTE' in bundle.observations[-1].reason_codes
    assert project(rows,bundle)[-1].status=='needs_review'


def test_missing_ownership_citation_is_rejected_without_boolean_coercion():
    value=evaluate([observation(1,'equipment/E','hasPoint','point/P',[])],equipment(),'site','run')
    assert value.observations[0].status=='rejected' and not value.observations[0].documentary_supported


@pytest.mark.parametrize('status',['verified','contradicted'])
def test_ineligible_authority_or_contradiction_cannot_override_documentary_claim(status):
    records=equipment()+[record('s','connections',from_equipment='E',to_equipment='F')]
    good=observation(1,'equipment/E','feeds','equipment/F',['s'])
    bad=observation(2,'equipment/E','feeds','equipment/F',['e1'],status=status)
    assert evaluate([good,bad],records,'site','run').claims[0].status=='supported_inference'


def test_wrong_class_domain_and_undocumented_medium_do_not_create_supported_conflict():
    records=equipment()+[record('p','points',point_id='P',point_class='AHU'),
        record('s1','connections',from_equipment='E',to_equipment='F',connection_scope='same',medium_exclusive='true'),
        record('s2','connections',from_equipment='E',to_equipment='F',connection_scope='same',medium_exclusive='true')]
    rows=[observation(1,'point/P','type','AHU',['p']),observation(2,'equipment/E','feeds','equipment/F',['s1'],medium='water'),
          observation(3,'equipment/E','feeds','equipment/F',['s2'],medium='air')]
    value=evaluate(rows,records,'site','run')
    assert value.observations[0].status=='rejected' and not value.observations[0].documentary_supported
    assert not value.conflicts


def test_irrelevant_record_cannot_supply_exclusivity_or_medium_constraints():
    records=equipment()+[record('s1','connections',from_equipment='E',to_equipment='F',medium='water'),
        record('s2','connections',from_equipment='F',to_equipment='E',medium='water')]
    records[0].normalized_fields.update(from_equipment='E',to_equipment='F',medium='water',connection_scope='fake',direction_exclusive='true')
    records[1].normalized_fields.update(from_equipment='F',to_equipment='E',medium='water',connection_scope='fake',direction_exclusive='true')
    rows=[observation(1,'equipment/E','feeds','equipment/F',['s1','e1'],medium='water'),
          observation(2,'equipment/F','feeds','equipment/E',['s2','e2'],medium='water')]
    value=evaluate(rows,records,'site','run')
    assert all(o.documentary_supported for o in value.observations)
    assert not value.conflicts  # equipment rows cannot declare connection constraints
