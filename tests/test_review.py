"""FR-04/05/07/11: SYNTHETIC Phase A review; no claimed human verification."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
from ontology_engine.pipeline import run
from ontology_engine.publication import verify_package
from ontology_engine.review import ReviewService, create_workspace, package
from ontology_engine.review_models import ReviewAction, canonical
from test_candidate_integration import flow_data


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def review(tmp_path):
    data = flow_data(tmp_path/'input')
    parent = tmp_path/'parent'; run(data, parent, building_id='SYNTHETIC:review-A')
    token = tmp_path/'credential'
    workspace = create_workspace(parent, tmp_path/'review', 'synthetic-reviewer', token)
    service = ReviewService(tmp_path/'review')
    claim = next(c for c in json.loads((parent/'candidate_claims.json').read_text())['records'] if c['predicate']=='feeds')
    item = service.inspect('claim', claim['claim_uid'])
    action = dict(workspace_id=workspace.workspace_id, site_scope=workspace.site_scope,
                  parent_fingerprint=workspace.parent_fingerprint, action_id='action-1', correlation_id='investigation-1',
                  actor_id=workspace.reviewer.actor_id, expected_revision=0, target_kind='claim', target_id=claim['claim_uid'],
                  action='defer', requested_status='needs_review', reason='SYNTHETIC conflicting documents need field review.',
                  evidence_uids=[item['sources'][0]['source_record_uid']])
    return service, token, action, parent


def test_pending_decision_preserves_competition_raw_evidence_and_original_bytes(review):
    service, token, action, parent = review
    before = hashes(parent); state = hashes(parent.with_name('.parent.run'))
    item = service.inspect('claim', action['target_id'])
    assert item['claims'][0]['status']=='contradicted' and len(item['alternatives'])==1
    assert item['sources'][0]['raw_value'] and item['conflicts'][0]['reason_code']=='EXPLICIT_OPPOSING_FLOW'
    event = service.decide(action, token)
    assert event.status=='needs_review' and event.outcome=='pending' and event.graph_consequence=='not_applied'
    assert event.evidence[0].source_hash and event.claim_uids==[action['target_id']]
    reopened = ReviewService(service.root)
    assert reopened.history()['events']==[event.model_dump()]
    assert reopened.verify()['revision']==1
    assert hashes(parent)==before and hashes(parent.with_name('.parent.run'))==state
    verify_package(parent)


def test_preview_does_not_write_and_exact_retry_is_idempotent(review):
    service, token, action, _ = review
    before=hashes(service.root)
    preview=service.decide(action, token, preview=True)
    assert preview.revision==1 and hashes(service.root)==before
    event=service.decide(action, token)
    assert service.decide(action, token)==event
    assert service.history()['total']==1
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        service.decide(dict(action,reason='Different'),token)


def test_explicit_supersession_preserves_all_history(review):
    service,token,action,_=review
    first=service.decide(action,token)
    replacement=dict(action,action_id='action-2',expected_revision=1,action='reject',requested_status='rejected')
    with pytest.raises(ValueError,match='REVIEW_CONFLICT'):service.decide(replacement,token)
    second=service.decide(dict(replacement,supersedes_event_id=first.event_id),token)
    assert second.request.supersedes_event_id==first.event_id and second.status=='needs_review'
    assert service.history()['total']==2 and service.verify()['revision']==2


@pytest.mark.parametrize('field,value,code',[
    ('expected_revision',1,'STALE_REVISION'),('site_scope','other','REVIEW_SCOPE'),
    ('workspace_id','other','REVIEW_SCOPE'),('target_id','claim:foreign','REVIEW_SCOPE'),
    ('actor_id','unregistered','UNAUTHORIZED'),('parent_fingerprint','0'*64,'STALE_EVIDENCE'),
    ('evidence_uids',['src:foreign'],'REVIEW_EVIDENCE'),('supersedes_event_id','foreign','REVIEW_CONFLICT')])
def test_invalid_requests_fail_closed_without_history(review,field,value,code):
    service,token,action,_=review
    with pytest.raises(ValueError,match=code):service.decide(dict(action,**{field:value}),token)
    assert service.history()['total']==0


def test_valid_bearer_cannot_verify_and_fake_credential_cannot_decide(review,tmp_path):
    service,token,action,_=review
    with pytest.raises(ValueError,match='VERIFICATION_UNAVAILABLE'):
        service.decide(dict(action,action='accept',requested_status='verified'),token)
    fake=tmp_path/'fake';fake.write_text('not-valid');fake.chmod(0o600)
    with pytest.raises(ValueError,match='UNAUTHORIZED'):service.decide(action,fake)
    assert service.history()['total']==0
    assert token.read_text().strip() not in (service.root/'review.sqlite').read_bytes().decode('utf-8',errors='ignore')


def test_role_cannot_be_self_asserted(review,tmp_path):
    _,_,_,parent=review
    token=tmp_path/'observer-token'
    w=create_workspace(parent,tmp_path/'observer','viewer',token,role='observer')
    s=ReviewService(tmp_path/'observer')
    original=review[2]
    a=dict(original,workspace_id=w.workspace_id,actor_id='viewer')
    with pytest.raises(ValueError,match='UNAUTHORIZED'):s.decide(a,token)
    with pytest.raises(ValueError):ReviewAction.model_validate(dict(a,role='admin'))


def test_source_changes_and_missing_prerequisites_fail_closed(review):
    service,token,action,parent=review
    p=parent/'sources/connections.csv';original=p.read_bytes();p.write_bytes(original+b'\n')
    with pytest.raises(ValueError,match='PARENT_INVALID'):service.decide(action,token)
    p.write_bytes(original)
    (parent/'COMPLETED.json').unlink()
    with pytest.raises((ValueError,OSError)):service.decide(action,token)


def test_original_republished_changed_fingerprint_is_stale(review,monkeypatch):
    service,token,action,_=review
    import ontology_engine.review as module
    original=module.package
    def altered(path):
        root,manifest,files,fp=original(path)
        return root,manifest,files,'f'*64
    monkeypatch.setattr(module,'package',altered)
    with pytest.raises(ValueError,match='STALE_EVIDENCE'):service.decide(action,token)


def test_concurrent_actions_have_one_revision_and_exact_retry(review):
    service,token,action,_=review
    def submit(a):
        try:return ReviewService(service.root).decide(a,token).event_id
        except ValueError as e:return str(e)
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers=list(pool.map(submit,[action,dict(action,action_id='other')]))
    assert sum('STALE_REVISION' in a for a in answers)==1
    assert service.history()['total']==1
    with ThreadPoolExecutor(max_workers=2) as pool:
        stored=service.history()['events'][0]['request']
        retry=list(pool.map(submit,[stored,stored]))
    assert retry[0]==retry[1] and service.history()['total']==1


def test_failed_transaction_rolls_back(review):
    service,token,action,_=review
    def crash(stage):
        if stage=='review_before_commit':raise OSError('SYNTHETIC interruption')
    with patch('ontology_engine.review.checkpoint_hook',side_effect=crash):
        with pytest.raises(OSError):service.decide(action,token)
    assert service.history()['total']==0
    assert service.decide(action,token).revision==1


def test_append_only_and_semantic_integrity(review):
    service,token,action,_=review;service.decide(action,token)
    db=sqlite3.connect(service.db_path)
    with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM events')
    db.rollback()
    db.execute('DROP TRIGGER events_no_update')
    row=db.execute('select payload from events').fetchone()[0]
    payload=json.loads(row);payload['status']='verified'
    db.execute('update events set payload=?',(canonical(payload),));db.commit();db.close()
    with pytest.raises(ValueError):service.verify()


@pytest.mark.parametrize('change',[
    {'unknown_field':True},{'reason':' '},{'action':'reject','requested_status':'needs_review'},
    {'action':'accept','requested_status':'supported_inference','evidence_uids':[]},
    {'action':'correct'},{'reason':'x'*4097},{'evidence_uids':['dup','dup']},
    {'expected_revision':True},{'schema_version':'review-v99'}])
def test_strict_schemas(review,change):
    service,token,action,_=review
    with pytest.raises(ValueError):service.decide(dict(action,**change),token)
    assert service.history()['total']==0


def test_private_permissions_symlinks_and_original_output_guard(review,tmp_path):
    service,token,action,parent=review
    with pytest.raises(ValueError,match='IMMUTABLE_PARENT'):
        create_workspace(parent,parent/'review','x',tmp_path/'new-token')
    with pytest.raises(ValueError,match='IMMUTABLE_PARENT'):
        create_workspace(parent,tmp_path/'another','x',parent.with_name('.parent.run')/'secret')
    link=tmp_path/'link';link.symlink_to(token)
    with pytest.raises(OSError):service.decide(action,link)
    token.chmod(0o644)
    with pytest.raises(ValueError,match='PERMISSION'):service.decide(action,token)
    token.chmod(0o600)
    (parent/'unexpected').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError,match='PATH_UNSAFE'):service.decide(action,token)


def test_queue_pagination_bounds_and_no_silent_resolution(review):
    service,token,action,_=review
    initial=service.queue();service.decide(action,token)
    after=service.queue()
    assert initial['total']==after['total'] and any(r['decision_state']=='pending' for r in after['items'])
    assert len(service.queue(limit=1)['items'])==1
    for kwargs in ({'limit':201},{'offset':-1},{'limit':True}):
        with pytest.raises(ValueError,match='REVIEW_LIMIT'):service.queue(**kwargs)


def test_question_group_cannot_accept_members(review):
    service,token,action,parent=review
    questions=json.loads((parent/'review_questions.json').read_text())
    q=next(q for q in questions if len(q['affected_assertion_ids'])==2)
    a=dict(action,target_kind='question',target_id=q['review_id'],action='accept',requested_status='supported_inference')
    with pytest.raises(ValueError,match='GROUP_ACTION'):service.decide(a,token)
    assert service.decide(dict(a,action='defer',requested_status='needs_review'),token).status=='needs_review'


def test_pending_correction_validates_canonical_vocabulary_and_endpoints(review):
    service,token,action,_=review
    claim=service.inspect('claim',action['target_id'])['claims'][0]
    correction=dict(kind='relationship',subject_id=claim['subject_id'],predicate='feeds',object_id=claim['object_id'],clarification='SYNTHETIC proposed clarification')
    a=dict(action,action='correct',requested_status='needs_review',correction=correction)
    assert service.decide(a,token,preview=True).request.correction.kind=='relationship'
    with pytest.raises(ValueError,match='CORRECTION'):
        service.decide(dict(a,correction=dict(correction,object_id='equipment/foreign')),token)
