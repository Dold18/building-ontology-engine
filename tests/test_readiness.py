"""FR-10/D2/D3/D8 development contract. SYNTHETIC / FOR TESTING ONLY."""
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest
from rdflib import Graph, RDF, URIRef
from ontology_engine.pipeline import run, entity_iri
from ontology_engine.review import files_snapshot, package, create_workspace, ReviewService
from ontology_engine.readiness.models import Report
from ontology_engine.readiness.report import generate
from ontology_engine.readiness.publication import create_report, verify_report
from ontology_engine.readiness.render import json_bytes, render
from ontology_engine.vocabulary import BRICK

ROOT=Path(__file__).resolve().parents[1]
INPUTS=ROOT/'fixtures/readiness_c_panels'


def read(p):return json.loads(Path(p).read_bytes())
def hashes(p):return {n:hashlib.sha256(v).hexdigest() for n,v in files_snapshot(p).items()}
def thermal(r):return next(a for a in r.assessments if a.target_id=='equipment/HX-A')
def query(r,kind,target):return next(q for q in r.queries if q.kind==kind and q.target_id=='equipment/'+target)
def projection(p):
    _,_,files,fp=package(p)
    return generate(files,fp,{'full_brick_shacl':'passed','released_vocabulary':'passed'})


@pytest.fixture(scope='module')
def cases(tmp_path_factory):
    root=tmp_path_factory.mktemp('readiness')
    for n,h in read(ROOT/'fixtures/readiness_c_panels_reference/freeze.json')['sha256'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h
    for name in ('complete','missing','conflicted'):
        parent=root/(name+'-parent')
        with patch('ontology_engine.providers.openai',side_effect=AssertionError('External call forbidden')):
            run(INPUTS/name,parent,building_id='SYNTHETIC:readiness-'+name)
            before=hashes(parent);state=hashes(root/('.'+name+'-parent.run'))
            create_report(parent,root/name)
            assert hashes(parent)==before and hashes(root/('.'+name+'-parent.run'))==state
    return root


@pytest.mark.parametrize('case',['complete','missing','conflicted'])
def test_frozen_query_answers_and_source_graph_lineage(cases,case):
    r=Report.model_validate(read(cases/case/'readiness_report.json'))
    zone=query(r,'serves_zone','Zone-A');names={a['entity_id'] for a in zone.answers}
    assert names==({'equipment/AHU-A'} if case=='conflicted' else {'equipment/HX-A','equipment/AHU-A'})
    uncertain=query(r,'serves_zone','Zone-Unknown');assert uncertain.state=='unknown' and not uncertain.answers and uncertain.withheld_claim_uids
    points=query(r,'asset_points','HX-A');assert {a['entity_id'] for a in points.answers}=={'point/Tin','point/Tout','point/Flow','point/Heat'}
    assert query(r,'equipment_meters','HX-A').state=='missing'
    meters=query(r,'equipment_meters','Panel-A');assert [a['entity_id'] for a in meters.answers]==['equipment/Meter-A']
    blocked=query(r,'equipment_meters','AHU-A');assert blocked.state=='contradicted' and not blocked.answers and blocked.withheld_claim_uids
    assert query(r,'equipment_meters','Unknown-A').state=='missing'
    g=Graph().parse(cases/case/'parent_package/ontology.ttl',format='turtle')
    claims={c['claim_uid']:c for c in r.claims};obs={o['observation_uid']:o for o in r.observations};sources={s.source_id:s for s in r.evidence}
    for q in r.queries:
        for a in q.answers:
            assert a['types'] and a['claim_uids']
            assert all(claims[c]['status']=='supported_inference' for c in a['claim_uids'])
            for edge in a['path']:
                ns=read(cases/case/'parent_package/run_manifest.json')['settings']['namespace']
                assert (entity_iri(ns,edge['subject_id']),BRICK[edge['predicate']],entity_iri(ns,edge['object_id'])) in g
                for c in a['claim_uids']:
                    for o in claims[c]['observation_uids']:
                        for sid in obs[o]['resolved_source_ids']:
                            s=sources[sid];assert s.source_hash==r.input_hashes[s.source_file]
                            assert s.source_record_uid and s.source_location and s.raw_record
    zone_entity=next(e for e in r.entities if e['entity_id']=='equipment/Zone-A')
    assert zone_entity['types']==['HVAC_Zone']
    assert (URIRef(zone_entity['iri']),RDF.type,BRICK.Equipment) not in g
    assert read(cases/case/'brick_shacl.json')['full_brick_shacl']=='passed'


def test_bounded_thermal_calculation_and_abstention(cases):
    r=Report.model_validate(read(cases/'complete/readiness_report.json'));a=thermal(r)
    assert a.state=='ready' and a.calculation['calculated_power_W']==pytest.approx([41800]*3)
    assert a.calculation['measured_power_W']==pytest.approx([41800]*3)
    assert a.calculation['result_units']=='W' and a.calculation['samples']==3 and a.calculation['source_uids']
    assert a.calculation['window_start']=='2026-01-01T00:00:00+00:00'
    assert all(p['state']=='ready' for p in a.prerequisites)
    assert next(x for x in r.assessments if x.target_id=='equipment/AHU-A').state=='inapplicable'
    assert next(x for x in r.assessments if x.target_id=='equipment/Unknown-A').state=='unknown'
    a=thermal(Report.model_validate(read(cases/'missing/readiness_report.json')))
    assert a.state=='not_evaluable' and a.calculation is None and 'INSUFFICIENT_FLOW_SAMPLES' in a.reason_codes
    flow=next(p for p in a.prerequisites if p.get('point_id')=='Flow')
    assert flow['state']=='missing' and flow['supplied_sample_count']==0 and not flow['measurement_source_ids']
    # An unrelated disputed downstream service path cannot erase local thermal evidence.
    assert thermal(Report.model_validate(read(cases/'conflicted/readiness_report.json'))).state=='ready'


@pytest.mark.parametrize('field,value,reason,expected',[
    ('units','Pa','INVALID_MEASUREMENT','blocked'),
    ('timestamp','2026-01-01','INVALID_TIMESTAMP','not_evaluable'),
    ('quality','bad','INVALID_MEASUREMENT','blocked'),
    ('operating_state','off','EQUIPMENT_NOT_RUNNING_OR_UNKNOWN','not_evaluable'),
    ('value','NaN','INVALID_MEASUREMENT','blocked'),
    ('equipment_id','WRONG','MEASUREMENT_EQUIPMENT_MISMATCH','blocked'),
])
def test_invalid_real_source_prerequisites_abstain(tmp_path,field,value,reason,expected):
    source=tmp_path/'source';shutil.copytree(INPUTS/'complete',source)
    p=source/'measurements.csv'
    with p.open(newline='') as f:reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
    rows[0][field]=value
    with p.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:bad-prerequisite')
    a=thermal(projection(parent));assert a.state==expected and a.calculation is None and reason in a.reason_codes


def test_no_telemetry_or_configuration_never_fabricates_readings(tmp_path):
    source=tmp_path/'source';shutil.copytree(INPUTS/'complete',source)
    (source/'measurements.csv').unlink();(source/'thermal_checks.csv').unlink()
    with (source/'points.csv').open('a') as f:f.write('broken,row\n')
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:missing')
    r=projection(parent);a=thermal(r)
    assert a.state=='not_evaluable' and a.calculation is None
    assert all(a.calculation is None for a in r.assessments)
    assert any(s.parse_status=='error' and s.raw_value==['broken','row'] for s in r.evidence)


@pytest.mark.parametrize('damage',['json','html','source','graph','extra','manifest','semantic'])
def test_archive_hash_and_semantic_tampering_refused(cases,tmp_path,damage):
    dest=tmp_path/'copy';shutil.copytree(cases/'complete',dest)
    paths={'json':'readiness_report.json','html':'readiness_report.html','source':'parent_package/sources/points.csv',
           'graph':'parent_package/ontology.ttl','extra':'extra.txt','manifest':'report_manifest.json'}
    if damage=='semantic':
        p=dest/'readiness_report.json';r=read(p);r['assessments'][0]['state']='ready';r['assessments'][0]['reason_codes']=['FORGED'];p.write_bytes(json_bytes(r))
        m=read(dest/'report_manifest.json');m['output_hashes'][p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        (dest/'report_manifest.json').write_bytes(json_bytes(m))
        (dest/'COMPLETED.json').write_bytes(json_bytes({'report_id':m['report_id'],'manifest_sha256':hashlib.sha256((dest/'report_manifest.json').read_bytes()).hexdigest()}))
    else:
        p=dest/paths[damage];p.write_bytes((p.read_bytes() if p.exists() else b'')+b'altered')
    with pytest.raises(ValueError):verify_report(dest)


def test_repeat_portability_wrong_parent_and_version(cases,tmp_path,monkeypatch):
    parent=cases/'complete-parent';before=hashes(parent);out=tmp_path/'repeat'
    create_report(parent,out);create_report(parent,out,resume=True)
    assert hashes(out)==hashes(cases/'complete') and hashes(parent)==before
    portable=tmp_path/'portable';shutil.copytree(out,portable);verify_report(portable)
    with pytest.raises(ValueError,match='PARENT_MISMATCH'):verify_report(out,cases/'missing-parent')
    from ontology_engine.readiness import publication
    monkeypatch.setattr(publication,'implementation_fingerprint',lambda:'changed')
    with pytest.raises(ValueError,match='VERSION'):verify_report(out)


@pytest.mark.parametrize('stage',['before_checkpoint','before_publication','after_publication'])
def test_crash_and_resume_keeps_parent_immutable(cases,tmp_path,stage):
    parent=cases/'complete-parent';before=hashes(parent);dest=tmp_path/'report'
    code='''import os,sys
import ontology_engine.readiness.publication as p
def hook(stage):
    if stage==sys.argv[3]:os._exit(73)
p.checkpoint_hook=hook
p.create_report(sys.argv[1],sys.argv[2])
'''
    result=subprocess.run([sys.executable,'-c',code,str(parent),str(dest),stage],capture_output=True,text=True)
    assert result.returncode==73,result.stderr
    create_report(parent,dest,resume=True);verify_report(dest,parent)
    assert hashes(dest)==hashes(cases/'complete') and hashes(parent)==before


def test_cli_create_verify_safe_errors(cases,tmp_path):
    parent=cases/'complete-parent';dest=tmp_path/'report'
    for args in (['create','--parent',str(parent),'--output',str(dest)],['verify','--report',str(dest),'--parent',str(parent)]):
        result=subprocess.run([sys.executable,'-m','ontology_engine','readiness',*args],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['schema_version']=='readiness-report-v1'
    result=subprocess.run([sys.executable,'-m','ontology_engine','readiness','verify','--report',str(tmp_path/'<script>')],capture_output=True,text=True)
    assert result.returncode==1 and '<script>' not in result.stderr and 'Traceback' not in result.stderr


@pytest.mark.parametrize('target',['parent','parent/nested','.parent.run/report'])
def test_overlapping_output_rejected(cases,tmp_path,target):
    parent=tmp_path/'parent';shutil.copytree(cases/'complete-parent',parent)
    dest=tmp_path/target
    if target.endswith('/report'):dest.parent.mkdir()
    with pytest.raises(ValueError,match='IMMUTABLE_PARENT'):create_report(parent,dest)


def test_symlink_and_work_limits_refuse_publication(cases,tmp_path,monkeypatch):
    linked=tmp_path/'linked';linked.symlink_to(cases/'complete-parent',target_is_directory=True)
    with pytest.raises(ValueError,match='PATH_UNSAFE'):create_report(linked,tmp_path/'report')
    from ontology_engine.readiness import report
    monkeypatch.setattr(report,'MAX_WORK',0)
    with pytest.raises(ValueError,match='WORK_LIMIT'):create_report(cases/'complete-parent',tmp_path/'report')
    assert not (tmp_path/'report').exists()


def test_site_scope_and_malicious_text_are_preserved_and_escaped(cases,tmp_path):
    source=tmp_path/'source';shutil.copytree(INPUTS/'complete',source)
    p=source/'connections.csv';p.write_text(p.read_text().replace('SYNTHETIC duct schedule supplies Zone-A','<script>alert(1)</script> =COMMAND()'))
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:second-site');r=projection(parent)
    old=Report.model_validate(read(cases/'complete/readiness_report.json'))
    assert not {c['claim_uid'] for c in r.claims}&{c['claim_uid'] for c in old.claims}
    assert not {s.source_record_uid for s in r.evidence}&{s.source_record_uid for s in old.evidence}
    rendered=render(r)['readiness_report.html'].decode()
    assert '<script>' not in rendered and '&lt;script&gt;' in rendered
    assert '<script>' in str(r.model_dump())


def test_reviewed_parent_and_existing_commissioning_are_compatible(tmp_path):
    from ontology_engine.review_export import export_review
    from scripts.task004_final_support import alias_request
    from ontology_engine.commissioning.publication import create_report as commissioning_create, verify_report as commissioning_verify
    parent=tmp_path/'parent';run(ROOT/'fixtures/review_shared_cause',parent,building_id='SYNTHETIC:readiness-reviewed')
    token=tmp_path/'credential';create_workspace(parent,tmp_path/'workspace','synthetic-reviewer',token,schema_version='review-v2')
    service=ReviewService(tmp_path/'workspace');service.decide(alias_request(service),token)
    reviewed=tmp_path/'reviewed';export_review(service,reviewed,token,1)
    before=hashes(reviewed);create_report(reviewed,tmp_path/'ready');verify_report(tmp_path/'ready',reviewed)
    commissioning_create(reviewed,tmp_path/'commissioning');commissioning_verify(tmp_path/'commissioning',reviewed)
    assert hashes(reviewed)==before
    assert (tmp_path/'ready/parent_package/review_basis.json').read_bytes()==(reviewed/'review_basis.json').read_bytes()


def test_historical_review_fingerprint_is_not_bypassed(tmp_path,monkeypatch):
    # A fresh reviewed package becomes historical when its original code fingerprint differs.
    from ontology_engine.review_export import export_review
    import ontology_engine.review_export as replay
    parent=tmp_path/'parent';run(INPUTS/'complete',parent,building_id='SYNTHETIC:historical')
    token=tmp_path/'credential';create_workspace(parent,tmp_path/'workspace','synthetic-reviewer',token,schema_version='review-v2')
    export_review(ReviewService(tmp_path/'workspace'),tmp_path/'reviewed',token,0)
    monkeypatch.setattr(replay,'code_fingerprint',lambda:'new-code')
    with pytest.raises(ValueError):create_report(tmp_path/'reviewed',tmp_path/'report')
    assert not (tmp_path/'report').exists()


def test_new_terms_use_existing_evidence_gate_and_standard_types(cases):
    from ontology_engine.evidence import eligibility
    from ontology_engine.pipeline import validate_graph
    _,_,files,_=package(cases/'complete-parent')
    sources={r.source_id:r for r in [__import__('ontology_engine.models',fromlist=['SourceRecord']).SourceRecord.model_validate_json(line) for line in files['source_records.jsonl'].splitlines()]}
    meter=next(s.source_id for s in sources.values() if s.normalized_fields.get('connection_id')=='meter')
    assert eligibility('equipment/Panel-A','hasPart','equipment/Meter-A',[meter],sources,'direct_document','documented')['eligible']
    assert not eligibility('equipment/Meter-A','meters','equipment/HX-A',[meter],sources,'direct_document','unsupported predicate')['eligible']
    assert not eligibility('equipment/Meter-A','meters','equipment/HX-A',[meter],sources,'llm','model says')['eligible']
    assert not eligibility('equipment/AHU-A','meters','equipment/HX-A',[meter],sources,'direct_document','unsupported meter')['eligible']
    assert not eligibility('equipment/Zone-A','hasPart','equipment/HX-A',[meter],sources,'direct_document','zone is not equipment')['eligible']
    g=Graph();a=URIRef('urn:a');b=URIRef('urn:b')
    g.add((a,RDF.type,BRICK.AHU));g.add((b,RDF.type,BRICK.Heat_Exchanger));g.add((a,BRICK.meters,b))
    assert validate_graph(g)['supported_subset']=='failed'


def test_contradicted_ownership_blocks_calculation_and_absent_zone_stays_unknown(tmp_path):
    source=tmp_path/'source';shutil.copytree(INPUTS/'complete',source)
    with (source/'equipment.csv').open('a') as f:f.write('Zone-Absent,HVAC_Zone\n')
    p=source/'points.csv';s=p.read_text().replace('measurement_role','measurement_role,ownership_scope,ownership_exclusive')
    s='\n'.join(line+',single-flow,true' if line.startswith('Flow,') else line+',,' if i else line for i,line in enumerate(s.splitlines()))+'\n'
    s+='Flow,AHU-A,Water_Flow_Sensor,volume_flow,L/s,flow,single-flow,true\n';p.write_text(s)
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:ownership-conflict');r=projection(parent);a=thermal(r)
    assert a.state=='blocked' and a.calculation is None and 'MISSING_OR_AMBIGUOUS_FLOW_POINT' in a.reason_codes
    assert not query(r,'serves_zone','Zone-Absent').answers and query(r,'serves_zone','Zone-Absent').state=='unknown'
    assert 'point/Flow' not in {x['entity_id'] for x in query(r,'asset_points','HX-A').answers}


def test_ready_is_not_a_passing_thermal_screen(tmp_path):
    source=tmp_path/'source';shutil.copytree(INPUTS/'complete',source)
    p=source/'measurements.csv';p.write_text(p.read_text().replace('41.8,kW','12,kW'))
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:screening-failure');a=thermal(projection(parent))
    assert a.state=='ready' and a.calculation['screening_outcome']=='fail'
    assert a.calculation['calculated_power_W']==pytest.approx([41800]*3)
    assert 'THERMAL_POWER_OUTSIDE_TOLERANCE' in a.reason_codes


def test_output_budget_and_bad_graph_abstain(cases,monkeypatch):
    from ontology_engine.readiness import report
    _,_,files,fp=package(cases/'complete-parent')
    with pytest.raises(ValueError,match='GRAPH_INVALID'):generate(files,fp,{'full_brick_shacl':'failed','released_vocabulary':'passed'})
    monkeypatch.setattr(report,'MAX_OUTPUT_BYTES',1)
    with pytest.raises(ValueError,match='OUTPUT_LIMIT'):projection(cases/'complete-parent')


def test_dotdot_alias_cannot_bypass_parent_containment(cases,tmp_path):
    from ontology_engine.readiness.publication import safe_path
    parent=tmp_path/'parent';shutil.copytree(cases/'complete-parent',parent)
    alias=tmp_path/'sibling';alias.mkdir()
    destination=alias/'..'/'parent'/'nested'
    assert safe_path(destination)==parent/'nested'
    before=hashes(parent)
    with pytest.raises(ValueError,match='IMMUTABLE_PARENT'):create_report(parent,destination)
    assert hashes(parent)==before and not (parent/'nested').exists()


def test_unimplemented_meter_load_predicate_is_never_promoted(tmp_path):
    # Original diagnostic fixture has a meters load claim unsupported by the accepted engine.
    parent=tmp_path/'parent';run(ROOT/'fixtures/readiness_c/complete',parent,building_id='SYNTHETIC:unsupported-meter')
    r=projection(parent)
    assert query(r,'equipment_meters','HX-A').answers==[]
    assert all(c['status'] not in {'verified','supported_inference'} for c in r.claims if c['predicate']=='meters')
    assert all(a['predicate']!='meters' for q in r.queries for a in q.answers for a in a['path'])
    for n,h in read(ROOT/'fixtures/readiness_c_reference/freeze.json')['sha256'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h


def test_invalid_hvac_meter_composition_fails_released_shapes(tmp_path):
    parent=tmp_path/'parent';run(ROOT/'fixtures/readiness_c_components/complete',parent,building_id='SYNTHETIC:invalid-composition')
    with pytest.raises(ValueError,match='GRAPH_INVALID'):create_report(parent,tmp_path/'report')
    assert not (tmp_path/'report').exists()
    for n,h in read(ROOT/'fixtures/readiness_c_components_reference/freeze.json')['sha256'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h
