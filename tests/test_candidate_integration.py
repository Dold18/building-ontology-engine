"""Phase A vertical integration, original evidence, atomic exports and recovery (SYNTHETIC)."""
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch
import pytest
from rdflib import Graph,RDF
from ontology_engine.pipeline import run,resume
from ontology_engine.publication import verify_package
from ontology_engine.vocabulary import BRICK
from ontology_engine.standards import validate_external
from test_closeout_a import dataset
from test_closeout_b import mocked
from test_closeout_c import crash


def ledger(out,name):return json.loads((out/name).read_text())['records']


def flow_data(path,reverse=True,scope='single',exclusive='true',media=('water','water')):
    data=dataset(path)
    (data/'equipment.csv').write_text('equipment_id,equipment_type\nE,AHU\nF,Pump\n')
    (data/'SYNTHETIC.txt').write_text('SYNTHETIC / FOR TESTING ONLY\n')
    (data/'connections.csv').write_text('from_equipment,to_equipment,medium,connection_scope,direction_exclusive,medium_exclusive\n'
        +f'E,F,{media[0]},{scope},{exclusive},{exclusive}\n'
        +f'{"F,E" if reverse else "E,F"},{media[1]},{scope},{exclusive},{exclusive}\n')
    return data


def test_documented_opposing_directions_withheld_but_partial_graph_retained(tmp_path):
    data=flow_data(tmp_path/'input');out=tmp_path/'out';run(data,out)
    m=verify_package(out);g=Graph().parse(out/'ontology.ttl')
    assert not list(g.triples((None,BRICK.feeds,None)))
    conflicts=ledger(out,'claim_conflicts.json');assert len(conflicts)==1 and conflicts[0]['kind']=='flow_direction'
    claims=[c for c in ledger(out,'candidate_claims.json') if c['predicate']=='feeds']
    assert all(c['status']=='contradicted' and c['conflicting_claim_uids'] for c in claims)
    assert list(g.subjects(RDF.type,BRICK.AHU)) and list(g.subjects(RDF.type,BRICK.Pressure_Sensor))
    assert (out/'sources/connections.csv').read_bytes()==(data/'connections.csv').read_bytes()
    assert 'candidate_claims.json' in m['output_hashes']
    assert validate_external(g)['full_brick_shacl']=='passed'


def test_duplicate_observations_one_claim_and_one_rdf_edge(tmp_path):
    data=flow_data(tmp_path/'input',reverse=False);out=tmp_path/'out';run(data,out)
    verify_package(out)
    c=next(c for c in ledger(out,'candidate_claims.json') if c['predicate']=='feeds')
    assert len(c['observation_uids'])==2 and c['status']=='supported_inference'
    o=[o for o in ledger(out,'candidate_observations.json') if o['claim_uid']==c['claim_uid']]
    assert len({tuple(r['resolved_source_uids']) for r in o})==2
    assert len(list(Graph().parse(out/'ontology.ttl').triples((None,BRICK.feeds,None))))==1


def test_two_source_supported_classes_and_legacy_alias_remain_inspectable(tmp_path,monkeypatch):
    data=dataset(tmp_path/'input')
    (data/'points.csv').write_text('point_id,description,point_class,equipment_id,units\np,Pressure Reading,Temperature_Sensor,E,\nfan,Supply Fan Status,Supply_Fan_Status,E,boolean\n')
    points=[{'source_id':'points.csv:2','point_class':c,'equipment_id':'E','explanation':text}
            for c,text in [('Temperature_Sensor','Explicit source class'),('Pressure_Sensor','Source description states pressure')]]
    points.append({'source_id':'points.csv:3','point_class':'Supply_Fan_Status','equipment_id':'E','explanation':'Legacy explicit class'})
    monkeypatch.setenv('OPENAI_API_KEY','fake')
    with patch('ontology_engine.providers.urlopen',return_value=mocked(points)):run(data,tmp_path/'out','openai','MOCKED',True)
    out=tmp_path/'out';verify_package(out);g=Graph().parse(out/'ontology.ttl')
    conflicts=ledger(out,'claim_conflicts.json');assert any(c['kind']=='classification' for c in conflicts)
    claims=[c for c in ledger(out,'candidate_claims.json') if c['subject_id']=='point/p' and c['object_id']!='Point']
    assert all(c['status']=='needs_review' for c in claims)
    assert not list(g.subjects(RDF.type,BRICK.Temperature_Sensor)) and not list(g.subjects(RDF.type,BRICK.Pressure_Sensor))
    assert list(g.subjects(RDF.type,BRICK.Fan_Status)) and not list(g.subjects(RDF.type,BRICK.Supply_Fan_Status))
    obs=ledger(out,'candidate_observations.json')
    assert any(o['original_proposals'] and o['original_proposals'][0]['point_class']=='Supply_Fan_Status' for o in obs)


def test_skipped_invalid_proposals_have_typed_rejection_and_original_citations(tmp_path,monkeypatch):
    data=dataset(tmp_path/'input');monkeypatch.setenv('OPENAI_API_KEY','fake')
    p={'source_id':'ghost','point_class':'Pressure_Sensor','equipment_id':'E','explanation':'Missing record'}
    relation={'subject_id':'equipment/E','predicate':'feeds','object_id':'equipment/F','supporting_source_ids':['equipment.csv:2','ghost'],'explanation':'Unsubstantiated relationship'}
    with patch('ontology_engine.providers.urlopen',return_value=mocked([p],relationships=[relation])):run(data,tmp_path/'out','openai','MOCKED',True)
    out=tmp_path/'out';verify_package(out)
    rows=ledger(out,'candidate_observations.json')
    orphan=next(o for o in rows if o['subject_id']=='unresolved-source/ghost')
    assert orphan['status']=='rejected' and orphan['initial_status']=='rejected' and orphan['assertion_id'] is None
    assert orphan['original_source_ids']==orphan['unresolved_source_ids']==['ghost']
    assert orphan['original_proposals']==[p] and orphan['inference_method']=='llm'
    rel=next(o for o in rows if o['predicate']=='feeds')
    assert rel['original_source_ids']==relation['supporting_source_ids'] and rel['unresolved_source_ids']==['ghost']
    assert not list(Graph().parse(out/'ontology.ttl').triples((None,BRICK.feeds,None)))


@pytest.mark.parametrize('stage',['before_candidates','candidates'])
def test_interrupted_candidate_stage_resumes_once_without_provider_replay(tmp_path,stage):
    data=flow_data(tmp_path/'input');out=tmp_path/'out';crash(data,out,stage)
    assert not out.exists()
    with patch('ontology_engine.providers.urlopen',side_effect=AssertionError('No replay')):resume(out);resume(out)
    verify_package(out)
    db=sqlite3.connect(tmp_path/'.out.run/state.sqlite')
    assert db.execute('select count(*) from stages where name="candidates"').fetchone()[0]==1
    assert db.execute('select state from runs').fetchone()[0]=='COMPLETED';db.close()
    obs=ledger(out,'candidate_observations.json')
    assert len(obs)==len({o['observation_uid'] for o in obs})
    assert len(ledger(out,'claim_conflicts.json'))==1


def test_candidate_checkpoint_tampering_blocks_resume(tmp_path):
    data=dataset(tmp_path/'input');out=tmp_path/'out';crash(data,out,'candidates')
    (tmp_path/'.out.run/candidate_bundle.json').write_text('{}')
    with pytest.raises(ValueError,match='CHECKPOINT_INVALID'):resume(out)
    assert not out.exists()


@pytest.mark.parametrize('filename',['candidate_claims.json','candidate_observations.json','claim_conflicts.json'])
def test_new_ledger_write_failure_never_publishes_false_complete(tmp_path,filename):
    data=dataset(tmp_path/'input');out=tmp_path/'out';original=Path.write_text
    def fail(path,*args,**kwargs):
        if path.name==filename:raise OSError('SYNTHETIC candidate ledger fault')
        return original(path,*args,**kwargs)
    with patch.object(Path,'write_text',fail):
        with pytest.raises(OSError):run(data,out)
    assert not out.exists() and list(tmp_path.glob('.out.staging-*/failure.json'))
    resume(out);verify_package(out)


def test_candidate_ledger_semantic_tampering_is_recomputed_not_just_hashed(tmp_path):
    data=dataset(tmp_path/'input');out=tmp_path/'out';run(data,out)
    value=json.loads((out/'candidate_claims.json').read_text());value['records'][0]['status']='verified'
    (out/'candidate_claims.json').write_text(json.dumps(value))
    with pytest.raises(ValueError,match='CANDIDATE_LEDGER_MISMATCH'):verify_package(out)


def test_explicit_exclusive_ownership_on_duplicate_tags_requests_identity_review(tmp_path):
    data=dataset(tmp_path/'input')
    (data/'equipment.csv').write_text('equipment_id,equipment_type\nE,AHU\nF,Pump\n')
    (data/'points.csv').write_text('point_id,equipment_id,ownership_scope,ownership_exclusive\np,E,physical-channel,true\np,F,physical-channel,true\n')
    run(data,tmp_path/'out');out=tmp_path/'out';verify_package(out)
    conflicts=ledger(out,'claim_conflicts.json')
    assert len(conflicts)==1 and conflicts[0]['kind']=='exclusive_ownership' and conflicts[0]['status']=='needs_review'
    assert not list(Graph().parse(out/'ontology.ttl').triples((None,BRICK.hasPoint,None)))
    records=[json.loads(line) for line in (out/'source_records.jsonl').read_text().splitlines()]
    assert all(r['parse_status']=='error' for r in records if r['table_kind']=='points')


def test_cross_building_candidate_and_conflict_isolation(tmp_path):
    data=flow_data(tmp_path/'input')
    for name in ('A','B'):run(data,tmp_path/name,building_id=name)
    for ledger_name,key in [('candidate_claims.json','claim_uid'),('claim_conflicts.json','conflict_uid')]:
        a={c[key] for c in ledger(tmp_path/'A',ledger_name)};b={c[key] for c in ledger(tmp_path/'B',ledger_name)}
        assert not a & b


def test_mocked_provider_not_replayed_after_candidate_commit(tmp_path):
    data=flow_data(tmp_path/'input');out=tmp_path/'out';crash(data,out,'candidates','openai')
    with patch('ontology_engine.providers.urlopen',side_effect=AssertionError('Do not replay')) as transport:
        result=resume(out);transport.assert_not_called()
    assert result.execution_origin=='mocked'
    verify_package(out)
    original=json.loads((tmp_path/'.out.run/candidate_bundle.json').read_text())
    assert ledger(out,'candidate_observations.json')==original['observations']
