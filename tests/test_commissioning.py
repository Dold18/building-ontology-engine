"""FR-08/D5 and FR-01/03–07/11/12 protections. SYNTHETIC development only."""
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from unittest.mock import patch

import pytest
from ontology_engine.pipeline import run
from ontology_engine.review import files_snapshot,create_workspace,ReviewService
from ontology_engine.review_export import export_review
from ontology_engine.commissioning.models import Finding,Priority,Report
from ontology_engine.commissioning.findings import generate
from ontology_engine.commissioning.publication import create_report,verify_report
from ontology_engine.commissioning.render import render,json_bytes

ROOT=Path(__file__).resolve().parents[1]
INPUTS=ROOT/'fixtures/commissioning_a'


def read(path):return json.loads(Path(path).read_text())
def hashes(path):return {n:hashlib.sha256(v).hexdigest() for n,v in files_snapshot(path).items()}


@pytest.fixture(scope='module')
def scenarios(tmp_path_factory):
    root=tmp_path_factory.mktemp('commissioning');expected=read(ROOT/'fixtures/commissioning_a_reference/answers.json')['cases']
    for n,h in read(ROOT/'fixtures/commissioning_a_reference/freeze.json')['sha256'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h
    for name in expected:
        parent=root/(name+'-parent');report=root/name
        with patch('ontology_engine.providers.openai',side_effect=AssertionError('No model calls')):
            run(INPUTS/name,parent,building_id='SYNTHETIC:commissioning-'+name)
            before=hashes(parent);state=hashes(root/('.'+name+'-parent.run'))
            create_report(parent,report)
            assert hashes(parent)==before and hashes(root/('.'+name+'-parent.run'))==state
    return root,expected


@pytest.mark.parametrize('name',['metadata','conflicting_documents','no_telemetry','screening'])
def test_seeded_findings_categories_and_actual_prerequisites(scenarios,name):
    root,expected=scenarios;r=Report.model_validate(read(root/name/'commissioning_report.json'))
    assert set(expected[name]['required_rules'])<={f.rule_id for f in r.findings}
    assert r.summary['by_category'].get('potential_operational_issue',0)==expected[name]['potential_operational_issue_count']
    assert all(f.confirmed_fault is False and f.uncertainty and f.alternative_explanations and f.recommended_investigation for f in r.findings)
    if name=='metadata':
        dup=next(f for f in r.findings if f.rule_id=='source.duplicate');assert len(dup.evidence)==2
        assert any('DIMENSION_OR_TEMPERATURE_KIND_MISMATCH' in f.reason_codes or 'SENSOR_ROLE_QUANTITY_MISMATCH' in f.reason_codes for f in r.findings)
        assert any('broken-row' in str(f.observed_evidence) or 'Row width' in str(f.observed_evidence) for f in r.findings)
    elif name=='conflicting_documents':
        f=next(f for f in r.findings if f.rule_id=='candidate.conflict')
        assert f.category=='model_conflict' and len(f.observed_evidence['competing_claims'])==2
        assert len(f.source_ids)==2 and f.conflict_uids and f.claim_uids and f.review_question_ids
    elif name=='no_telemetry':
        assert all(f.category!='potential_operational_issue' for f in r.findings)
        assert next(f for f in r.findings if f.rule_id=='operational.telemetry').detection_outcome=='not_evaluable'
    else:
        f=next(f for f in r.findings if f.category=='potential_operational_issue')
        assert f.rule_id=='engineering.thermal.water_balance' and f.operational_context_established
        assert f.observed_evidence['details']['calculated_power_W']==pytest.approx([expected[name]['expected_thermal_power_W']]*4)
        assert f.observed_evidence['details']['measured_power_W']==[12000.0]*4
        assert next(f for f in r.findings if 'BELOW_ABSOLUTE_ZERO' in f.reason_codes).category=='data_quality'


def test_exact_sources_links_priority_counts_and_deterministic_order(scenarios):
    root,_=scenarios;p=root/'conflicting_documents';r=Report.model_validate(read(p/'commissioning_report.json'))
    sources={json.loads(line)['source_id']:json.loads(line) for line in (p/'parent_package/source_records.jsonl').read_text().splitlines()}
    assert r.findings==sorted(r.findings,key=lambda f:(-f.priority.score,f.finding_uid))
    assert len({f.finding_uid for f in r.findings})==len(r.findings)==r.summary['finding_count']
    for f in r.findings:
        for evidence in f.evidence:assert evidence.model_dump()==sources[evidence.source_id]
        for a in f.artifact_evidence:assert hashlib.sha256((p/'parent_package'/a.path).read_bytes()).hexdigest()==a.sha256
        q=f.priority
        assert q.score==100*q.significance_rank+10*q.evidence_band+5*q.operational_context_band-q.effort_rank
        assert q.operational_criticality is None and q.failure_likelihood is None and q.human_minutes is None and q.urgency=='unknown'
    assert len(r.checks)==read(p/'parent_package/engineering_results.json')['summary']['result_denominator']


def test_repeatable_full_sidecar_and_exact_resume_are_immutable(scenarios,tmp_path):
    root,_=scenarios;a=root/'no_telemetry';b=tmp_path/'repeated';parent=root/'no_telemetry-parent'
    create_report(parent,b);assert hashes(a)==hashes(b)
    before=hashes(b);create_report(parent,b,resume=True);assert hashes(b)==before
    with pytest.raises(ValueError,match='COMMISSIONING_EXISTS'):create_report(parent,b)
    with sqlite3.connect(tmp_path/'.repeated.commissioning-run/state.sqlite') as db:
        assert db.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert db.execute('SELECT state FROM runs').fetchone()[0]=='COMPLETED'
        assert db.execute('SELECT count(*) FROM attempts').fetchone()[0]==0


@pytest.mark.parametrize('field',['quality','operating_state','timestamp','units'])
def test_missing_operational_evidence_abstains(scenarios,tmp_path,field):
    source=tmp_path/'input';shutil.copytree(INPUTS/'screening',source)
    path=source/'measurements.csv'
    with path.open() as stream:rows=list(csv.DictReader(stream));fields=list(rows[0])
    rows[0][field]=''
    with path.open('w',newline='') as stream:
        w=csv.DictWriter(stream,fieldnames=fields);w.writeheader();w.writerows(rows)
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:abstain')
    create_report(parent,tmp_path/'report')
    r=read(tmp_path/'report/commissioning_report.json')
    assert not any(f['category']=='potential_operational_issue' for f in r['findings'])
    f=next(f for f in r['findings'] if f['rule_id']=='engineering.thermal.water_balance')
    assert f['detection_outcome']=='not_evaluable' and f['recommended_investigation']


def rewrite_hashes(root):
    p=root/'report_manifest.json';m=read(p)
    m['output_hashes']={n:hashlib.sha256(raw).hexdigest() for n,raw in files_snapshot(root).items() if n not in ('report_manifest.json','COMPLETED.json')}
    p.write_bytes(json_bytes(m));(root/'COMPLETED.json').write_bytes(json_bytes({'report_id':m['report_id'],'manifest_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}))


@pytest.mark.parametrize('damage',['report','html','csv','source','parent_binding','source_reference','verified','extra','missing'])
def test_tampering_fails_even_when_report_hashes_are_rewritten(scenarios,tmp_path,damage):
    root,_=scenarios;p=tmp_path/'report';shutil.copytree(root/'metadata',p)
    if damage=='report':
        r=read(p/'commissioning_report.json');r['findings'][0]['priority']['score']+=1;(p/'commissioning_report.json').write_bytes(json_bytes(r));rewrite_hashes(p)
    elif damage in ('source_reference','verified'):
        r=read(p/'commissioning_report.json');r['findings'][0]['source_uids']=['foreign-source'] if damage=='source_reference' else r['findings'][0]['source_uids']
        if damage=='verified':r['findings'][0]['confirmed_fault']=True;r['findings'][0]['status']='verified'
        (p/'commissioning_report.json').write_bytes(json_bytes(r));rewrite_hashes(p)
    elif damage=='parent_binding':
        m=read(p/'report_manifest.json');m['parent_fingerprint']='0'*64;(p/'report_manifest.json').write_bytes(json_bytes(m));rewrite_hashes(p)
    elif damage=='source':
        q=p/'parent_package/sources/points.csv';q.write_bytes(q.read_bytes()+b'changed');rewrite_hashes(p)
    elif damage=='extra':(p/'extra.txt').write_text('SYNTHETIC extra');rewrite_hashes(p)
    elif damage=='missing':(p/'findings.csv').unlink()
    else:
        q=p/('commissioning_report.html' if damage=='html' else 'findings.csv');q.write_bytes(q.read_bytes()+b'altered');rewrite_hashes(p)
    with pytest.raises((ValueError,OSError)):verify_report(p)


def test_hostile_text_escaped_and_csv_uses_existing_encoding(scenarios):
    root,_=scenarios;r=Report.model_validate(read(root/'metadata/commissioning_report.json'))
    html=(root/'metadata/commissioning_report.html').read_text()
    assert '<script>' not in html and '&lt;script&gt;' in html and '<img ' not in html
    assert '<script>' in (root/'metadata/commissioning_report.json').read_text()
    altered=r.model_copy(deep=True);altered.findings[0].target_id='=SUM(1,2)'
    rows=list(csv.DictReader(io.StringIO(render(altered)['findings.csv'].decode())))
    assert rows[0]['target_id']=="'=SUM(1,2)"


def test_source_revision_and_cross_building_isolation(scenarios,tmp_path):
    root,_=scenarios;source=tmp_path/'input';shutil.copytree(INPUTS/'no_telemetry',source)
    (source/'points.csv').write_bytes((source/'points.csv').read_bytes()+b'\n')
    parent=tmp_path/'revised';run(source,parent,building_id='SYNTHETIC:commissioning-no_telemetry')
    with pytest.raises(ValueError,match='COMMISSIONING_PARENT_MISMATCH'):verify_report(root/'no_telemetry',parent)
    foreign=tmp_path/'foreign';run(INPUTS/'no_telemetry',foreign,building_id='SYNTHETIC:another-building')
    create_report(foreign,tmp_path/'foreign-report')
    a=read(root/'no_telemetry/commissioning_report.json');b=read(tmp_path/'foreign-report/commissioning_report.json')
    assert not {f['finding_uid'] for f in a['findings']}&{f['finding_uid'] for f in b['findings']}
    assert not {u for f in a['findings'] for u in f['source_uids']}&{u for f in b['findings'] for u in f['source_uids']}
    with pytest.raises(ValueError,match='COMMISSIONING_PARENT_MISMATCH'):verify_report(root/'no_telemetry',foreign)


@pytest.mark.parametrize('stage',['before_checkpoint','before_publication','after_publication'])
def test_real_process_exit_and_runstore_recovery(scenarios,tmp_path,stage):
    root,_=scenarios;parent=root/'no_telemetry-parent';out=tmp_path/'report';before=hashes(parent)
    code='''import os,sys
import ontology_engine.commissioning.publication as p
def hook(stage):
    if stage==sys.argv[3]:os._exit(73)
p.checkpoint_hook=hook
p.create_report(sys.argv[1],sys.argv[2])
'''
    result=subprocess.run([sys.executable,'-c',code,str(parent),str(out),stage],capture_output=True,text=True)
    assert result.returncode==73,result.stderr
    create_report(parent,out,resume=True);verify_report(out,parent)
    assert hashes(parent)==before and hashes(out)==hashes(root/'no_telemetry')


def test_paths_incomplete_state_and_changed_policy_fail_closed(scenarios,tmp_path,monkeypatch):
    root,_=scenarios;parent=root/'no_telemetry-parent'
    for dest in (parent/'report',parent.with_name('.'+parent.name+'.run')/'report'):
        with pytest.raises(ValueError,match='IMMUTABLE_PARENT'):create_report(parent,dest)
    link=tmp_path/'link';link.symlink_to(parent,target_is_directory=True)
    with pytest.raises(ValueError,match='PATH_UNSAFE'):create_report(link,tmp_path/'bad')
    with pytest.raises(ValueError,match='RECOVERY_MISSING'):create_report(parent,tmp_path/'missing',resume=True)
    from ontology_engine.commissioning import publication
    monkeypatch.setattr(publication,'implementation_fingerprint',lambda:'0'*64)
    with pytest.raises(ValueError,match='VERSION'):verify_report(root/'no_telemetry')


def test_reviewed_parent_uses_effective_fields_and_keeps_original_history(tmp_path):
    parent=tmp_path/'parent';run(ROOT/'fixtures/review_shared_cause',parent,building_id='SYNTHETIC:reviewed-commissioning')
    token=tmp_path/'credential';create_workspace(parent,tmp_path/'workspace','synthetic-reviewer',token,schema_version='review-v2')
    service=ReviewService(tmp_path/'workspace')
    from scripts.task004_final_support import alias_request
    service.decide(alias_request(service),token);reviewed=tmp_path/'reviewed';export_review(service,reviewed,token,1)
    before=hashes(reviewed);create_report(reviewed,tmp_path/'report');verify_report(tmp_path/'report',reviewed)
    r=read(tmp_path/'report/commissioning_report.json')
    assert not any(f['rule_id']=='metadata.sensor' and f['target_id'].startswith('point/XQ.') for f in r['findings'])
    assert (tmp_path/'report/parent_package/review_basis.json').read_bytes()==(reviewed/'review_basis.json').read_bytes()
    assert hashes(reviewed)==before


def test_cli_create_verify_and_safe_error_without_model_execution(scenarios,tmp_path):
    root,_=scenarios;parent=root/'no_telemetry-parent';dest=tmp_path/'report'
    for args in (['create','--parent',str(parent),'--output',str(dest)],['verify','--report',str(dest),'--parent',str(parent)]):
        result=subprocess.run([sys.executable,'-m','ontology_engine','commissioning',*args],capture_output=True,text=True)
        assert result.returncode==0,result.stderr
        assert json.loads(result.stdout)['schema_version']=='commissioning-report-v1'
    result=subprocess.run([sys.executable,'-m','ontology_engine','commissioning','verify','--report',str(tmp_path/'<script>')],capture_output=True,text=True)
    assert result.returncode==1 and '<script>' not in result.stderr


def test_typed_authority_and_priority_contracts_refuse_fabrication(scenarios):
    root,_=scenarios;r=read(root/'screening/commissioning_report.json');f=r['findings'][0]
    with pytest.raises(ValueError):Finding.model_validate(dict(f,confirmed_fault=True))
    with pytest.raises(ValueError):Priority.model_validate(dict(f['priority'],failure_likelihood=0.8))
    with pytest.raises(ValueError):Finding.model_validate(dict(f,category='potential_operational_issue',operational_context_established=False))


@pytest.mark.parametrize('limit,code',[('MAX_WORK','WORK_LIMIT'),('MAX_FINDINGS','FINDING_LIMIT'),('MAX_EVIDENCE_BYTES','EVIDENCE_LIMIT')])
def test_report_work_and_evidence_budgets_fail_without_publication(scenarios,tmp_path,monkeypatch,limit,code):
    from ontology_engine.commissioning import findings
    root,_=scenarios;parent=root/'no_telemetry-parent';before=hashes(parent)
    monkeypatch.setattr(findings,limit,0)
    with pytest.raises(ValueError,match=code):create_report(parent,tmp_path/'report')
    assert not (tmp_path/'report/COMPLETED.json').exists() and hashes(parent)==before


def test_unsafe_store_types_and_manifest_traversal_refused(scenarios,tmp_path):
    root,_=scenarios;parent=root/'no_telemetry-parent'
    work=tmp_path/'.report.commissioning-run';work.mkdir(mode=0o700)
    (work/'state.sqlite').mkdir()
    with pytest.raises(ValueError,match='STATE_UNSAFE'):create_report(parent,tmp_path/'report',resume=True)
    out=tmp_path/'copy';shutil.copytree(root/'no_telemetry',out)
    m=read(out/'report_manifest.json');m['output_hashes']['../../outside']='0'*64
    (out/'report_manifest.json').write_bytes(json_bytes(m))
    (out/'COMPLETED.json').write_bytes(json_bytes({'report_id':m['report_id'],'manifest_sha256':hashlib.sha256((out/'report_manifest.json').read_bytes()).hexdigest()}))
    with pytest.raises(ValueError,match='ARTIFACT_SET|PATH_UNSAFE'):verify_report(out)


def test_xlsx_cells_and_original_units_survive_report(tmp_path):
    from openpyxl import Workbook
    source=tmp_path/'input';source.mkdir();(source/'SYNTHETIC.txt').write_text('SYNTHETIC / FOR TESTING ONLY')
    wb=Workbook();equipment=wb.active;equipment.title='equipment';equipment.append(['equipment_id','equipment_type']);equipment.append(['E1','AHU'])
    points=wb.create_sheet('points');points.append(['point_id','equipment_id','point_class','units']);points.append(['T1','E1','Temperature_Sensor','Pa'])
    wb.save(source/'schedule.xlsx')
    parent=tmp_path/'parent';run(source,parent,building_id='SYNTHETIC:commissioning-xlsx');create_report(parent,tmp_path/'report')
    report=Report.model_validate(read(tmp_path/'report/commissioning_report.json'))
    evidence=[s for f in report.findings for s in f.evidence if s.sheet_name=='points']
    assert evidence and evidence[0].cell_locations['0']=='points!A2'
    assert evidence[0].normalized_fields['units']=='Pa' and evidence[0].raw_record['3']['value']=='Pa'


@pytest.mark.parametrize('damage',['database','checkpoint'])
def test_corrupted_local_state_and_checkpoint_refuse_safe_resume(scenarios,tmp_path,damage):
    root,_=scenarios;parent=root/'no_telemetry-parent';out=tmp_path/'report';work=tmp_path/'.report.commissioning-run'
    if damage=='database':
        work.mkdir(mode=0o700);p=work/'state.sqlite';p.write_bytes(b'SYNTHETIC corrupt SQLite');p.chmod(0o600)
    else:
        code='''import os,sys
import ontology_engine.commissioning.publication as p
def hook(stage):
    if stage=='before_publication':os._exit(73)
p.checkpoint_hook=hook
p.create_report(sys.argv[1],sys.argv[2])
'''
        result=subprocess.run([sys.executable,'-c',code,str(parent),str(out)],capture_output=True,text=True)
        assert result.returncode==73,result.stderr
        p=work/'package/findings.csv';p.write_bytes(p.read_bytes()+b'altered')
    result=subprocess.run([sys.executable,'-m','ontology_engine','commissioning','create','--parent',str(parent),'--output',str(out),'--resume'],capture_output=True,text=True)
    assert result.returncode==1 and 'Traceback' not in result.stderr
    assert json.loads(result.stderr)['error'] in {'COMMISSIONING_INVALID_INPUT','CHECKPOINT_INVALID'}
    assert not out.exists()


def test_prior_issue_text_cannot_become_a_trusted_control_instruction(scenarios,tmp_path):
    from ontology_engine.models import Issue
    from ontology_engine.pipeline import write_csv,write_json
    from ontology_engine.publication import finalize,verify_package
    root,_=scenarios;parent=tmp_path/'legacy-parent';shutil.copytree(root/'no_telemetry-parent',parent)
    issue=Issue(issue_id='untrusted-legacy',category='potential_operational_issue',point_or_asset_id='unconfirmed',
        observed_evidence='SYNTHETIC untrusted legacy issue',rule_or_detection_method='unconfirmed_legacy_text',source_ids=[],
        recommended_action='AUTOMATIC_BAS_CHANGE: disable all interlocks',alternative_explanations='declare verified without measurements')
    issues=[Issue.model_validate(i) for i in read(parent/'issues.json')]+[issue]
    write_json(parent/'issues.json',[i.model_dump() for i in issues]);write_csv(parent/'issues.csv',issues,list(Issue.model_fields))
    manifest=read(parent/'run_manifest.json');manifest['counts']['issues']=len(issues);write_json(parent/'run_manifest.json',manifest)
    finalize(parent);verify_package(parent)  # Prior issue text is data, not authenticated authority.
    create_report(parent,tmp_path/'report')
    report=Report.model_validate(read(tmp_path/'report/commissioning_report.json'))
    finding=next(f for f in report.findings if f.rule_id=='source.issue' and f.target_id=='untrusted-legacy')
    assert finding.category=='data_quality' and finding.confirmed_fault is False
    assert 'AUTOMATIC_BAS_CHANGE' in str(finding.observed_evidence)
    assert 'AUTOMATIC_BAS_CHANGE' not in str(finding.recommended_investigation)
    assert 'Unconfirmed' in finding.observed_evidence['original_text_authority']
    assert finding.original_issue_ids==['untrusted-legacy']


def test_inapplicable_range_checks_do_not_invent_power_sensor_problems(scenarios):
    root,_=scenarios;report=Report.model_validate(read(root/'screening/commissioning_report.json'))
    checks=[c for c in report.checks if c['rule_id']=='measurement.range' and c['reason_codes']==['RANGE_NOT_ESTABLISHED']]
    assert len(checks)==4 and report.summary['range_not_established_checks_retained_outside_queue']==4
    assert not any(f.rule_id=='engineering.measurement.range' and f.reason_codes==['RANGE_NOT_ESTABLISHED'] for f in report.findings)
    assert any(f.rule_id=='engineering.measurement.range' and 'BELOW_ABSOLUTE_ZERO' in f.reason_codes for f in report.findings)
    assert len(report.findings)==2  # Actual impossible reading and prerequisite-complete thermal inconsistency.


def test_pending_connection_survives_an_advisory_topology_pass(scenarios):
    root,_=scenarios;p=root/'conflicting_documents';report=Report.model_validate(read(p/'commissioning_report.json'))
    claims=read(p/'parent_package/candidate_claims.json')['records']
    pending=next(c for c in claims if c['subject_id']=='equipment/Aux-X' and c['predicate']=='feeds')
    assert pending['status']=='needs_review'
    assert any(c['outcome']=='pass' and pending['claim_uid'] in c['claim_uids'] for c in report.checks)
    finding=next(f for f in report.findings if f.rule_id=='candidate.unresolved_relationship' and pending['claim_uid'] in f.claim_uids)
    assert finding.category=='model_conflict' and finding.status=='not_evaluable'
    assert finding.evidence and finding.review_question_ids and finding.recommended_investigation
    assert finding.observed_evidence['pending_claim']['status']=='needs_review' and finding.confirmed_fault is False
