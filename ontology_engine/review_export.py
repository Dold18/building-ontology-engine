"""Reviewed packages use the existing deterministic gates, exporter and RunStore.

One immutable parent snapshot + frozen workspace history is the replay basis. No
provider is imported/invoked. This module orchestrates existing engine functions.
"""
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path
from .review import (ReviewService, ParentIndex, package, fail, files_snapshot, checked_root,
                     private_directory, write_new, sync_directory)
from .review_models import canonical, hashed
from .review_b_models import ExportBasis, contract_fingerprint
from .identity import uid
from .models import Assertion, Classification, Issue, ReviewItem, SourceRecord
from .candidates import evaluate_proposals, project, VERSION as VERSION_CANDIDATE, POLICY_VERSION
from .engineering import evaluate as engineering_evaluate, RuleResult
from .limits import Limits
from .pipeline import build_graph, validate_graph, write_json, write_csv, relationship_csv_row
from .state import RunStore, VERSION as STATE_VERSION, now
from .standards import validate_external, contract
from .change_history import diff_contents
from .review_replay import replay_inputs

VERSION='reviewed-package-v1'
EXTRA=['review_basis.json','replay_inputs.json','review_changes.json','root_cause_groups.json',
       'before_shacl.json','after_shacl.json','shareable/summary.json','shareable/events.jsonl']


def derived_counts(parent_counts, values, graph):
    counts=dict(parent_counts)
    counts.update(assertions=len(values['assertions.json']),accepted_triples=len(graph),
        classifications=len(values['classifications.json']),issues=len(values['issues.json']),review_items=len(values['review_questions.json']),
        candidate_claims=len(values['candidate_claims.json']['records']),candidate_observations=len(values['candidate_observations.json']['records']),
        claim_conflicts=len(values['claim_conflicts.json']['records']),engineering_results=len(values['engineering_results.json']['records']))
    return counts


def shareable_summary(manifest, parent):
    return {'run_id':manifest['run_id'],'parent_run_id':parent['run_id'],
        'data_origin':manifest['data_origin'],'execution_origin':'deterministic_review',
        'counts':manifest['counts'],'validation':manifest['validation'],
        'sponsor_accuracy':'NOT TESTED','real_inference':'NOT REPLAYED','new_external_requests':0}


def checkpoint_hook(stage):
    """Hard-exit seam, not configurable by source or reviewer text."""


def code_fingerprint():
    return hashed({'version':VERSION,'schema':contract_fingerprint(),'semantic':contract(),
        'code':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(__file__).parent.glob('*.py'))}})


def compute(files, workspace, events):
    """Full deterministic recomputation, shared by export and integrity verification."""
    inputs=replay_inputs(files,workspace,events)
    manifest=json.loads(files['run_manifest.json']);limits=Limits.model_validate(manifest['settings']['limits'])
    records=[SourceRecord.model_validate(r) for r in inputs['records']]
    base=[Assertion.model_validate(a) for a in inputs['assertions']]
    bundle=evaluate_proposals(base,records,workspace.site_scope,workspace.parent_run_id,inputs['proposals'],limits)
    assertions=project(base,bundle)
    engineering=engineering_evaluate(records,assertions,bundle,workspace.site_scope,limits)
    # A supported normalization alone does not override resulting conflicts/gates.
    for outcome in inputs['outcomes']:
        if outcome['outcome']=='applied_supported_inference':
            event=next(e for e in events if e.event_id==outcome['event_id'])
            edited={uid('claim',workspace.site_scope,e.subject_id,e.predicate,e.object_id) for e in event.request.correction.edits}
            if edited and any(c.claim_uid in edited and c.status not in {'supported_inference','verified'} for c in bundle.claims):
                fail('REVIEW_RECOMPUTE_CONFLICT','Correction creates a withheld claim; evidence requires further review.')
    classes=[Classification.model_validate(c) for c in json.loads(files['classifications.json'])]
    selected=set(inputs['dependency_analysis']['selected_claim_uids'])
    index=ParentIndex(files,workspace)
    changed_entities={index.claims[u]['subject_id'] for u in selected}
    changed_points=changed_entities|{index.claims[u]['object_id'] for u in selected if index.claims[u]['predicate']=='hasPoint'}
    for c in classes:
        entity='point/'+c.source_point_id
        if entity not in changed_points:continue
        types=[a for a in assertions if a.subject_id==entity and a.predicate=='type' and a.object_id!='Point']
        owners=[a for a in assertions if a.predicate=='hasPoint' and a.object_id==entity and a.status in {'supported_inference','verified'}]
        if len({a.object_id for a in types})==1 and types:
            row=types[-1]
            c.point_class=row.object_id;c.status=row.status;c.explanation=row.explanation
            c.supporting_source_ids=row.supporting_source_ids;c.inference_method=row.inference_method
            c.model_version=row.model_version;c.prompt_version=row.prompt_version
        c.equipment_id=owners[0].subject_id[10:] if len({a.subject_id for a in owners})==1 else ''
    old_engineering=json.loads(files['engineering_results.json'])['records']
    old_rule_reviews={uid('engineering-review',workspace.parent_run_id,r['result_uid']) for r in old_engineering}
    old_conflicts=json.loads(files['claim_conflicts.json'])['records']
    old_conflict_reviews={uid('conflict-review',workspace.site_scope,workspace.parent_run_id,c['conflict_uid']) for c in old_conflicts}
    reviews=[]
    for raw in json.loads(files['review_questions.json']):
        if raw['review_id'] in old_rule_reviews|old_conflict_reviews:continue
        q=ReviewItem.model_validate(raw)
        original=index.target('question',q.review_id)['claims']
        ids={c['claim_uid'] for c in original}
        if ids and ids<=selected:
            q.review_status='resolved_by_derived_interpretation';q.reviewer_decision='See review_basis.json and replay_inputs.json; no verified authority.'
        reviews.append(q)
    old_rule_issues={uid('engineering-issue',workspace.parent_run_id,r['result_uid']) for r in old_engineering}
    old_conflict_issues={uid('conflict-issue',workspace.site_scope,workspace.parent_run_id,c['conflict_uid']) for c in old_conflicts}
    issues=[Issue.model_validate(i) for i in json.loads(files['issues.json']) if i['issue_id'] not in old_rule_issues|old_conflict_issues]
    from .vocabulary import equipment_class
    resolved_equipment_sources={r.source_id for r in records if r.table_kind=='equipment' and
        'equipment/'+r.normalized_fields.get('equipment_id','') in changed_entities and
        equipment_class(r.normalized_fields.get('equipment_type',''))!='Equipment'}
    issues=[i for i in issues if not (i.rule_or_detection_method=='unknown_equipment_type' and
                                     set(i.source_ids)<=resolved_equipment_sources)]
    for conflict in bundle.conflicts:
        affected=[o.assertion_id for o in bundle.observations if o.observation_uid in conflict.observation_uids and o.assertion_id]
        reviews.append(ReviewItem(review_id=uid('conflict-review',workspace.site_scope,workspace.parent_run_id,conflict.conflict_uid),
            affected_assertion_ids=affected,question_or_test=conflict.explanation,supporting_evidence_ids=conflict.source_ids,
            reason_for_uncertainty=conflict.reason_code))
        issues.append(Issue(issue_id=uid('conflict-issue',workspace.site_scope,workspace.parent_run_id,conflict.conflict_uid),category='model_conflict',
            point_or_asset_id=conflict.claim_uids[0],observed_evidence=conflict.explanation,rule_or_detection_method=conflict.reason_code,
            source_ids=conflict.source_ids,recommended_action='Review documentary conflict; no automatic winner.'))
    for r in engineering.records:
        if r.outcome=='pass' or not r.source_ids:continue
        affected=[a.assertion_id for a in assertions if a.claim_uid in r.claim_uids]
        if r.rule_id=='topology.evidence' and any(set(q.affected_assertion_ids)&set(affected) for q in reviews if q.review_status=='open'):continue
        reviews.append(ReviewItem(review_id=uid('engineering-review',workspace.parent_run_id,r.result_uid),affected_assertion_ids=affected,
            question_or_test=r.action,supporting_evidence_ids=r.source_ids,reason_for_uncertainty=r.rule_id+': '+', '.join(r.reason_codes)))
        if r.outcome in {'fail','blocked','error'}:
            issues.append(Issue(issue_id=uid('engineering-issue',workspace.parent_run_id,r.result_uid),
                category='model_conflict' if r.rule_id=='topology.evidence' else 'data_quality',point_or_asset_id=r.target_id,
                observed_evidence=json.dumps(r.details),rule_or_detection_method=r.rule_id+'@'+r.rule_version,
                source_ids=r.source_ids,recommended_action=r.action,uncertainty='Screening finding; not a proven equipment fault.'))
    from .coverage import coverage
    original_records=[SourceRecord.model_validate(json.loads(line)) for line in files['source_records.jsonl'].splitlines()]
    # Retrieval/request evidence remains the original frozen context, not replayed.
    plan,context=verify_context_artifacts_from_files(files,original_records,manifest,limits)
    graph=build_graph(assertions,manifest['settings']['namespace'])
    validation=validate_graph(graph);validation.update(engineering=engineering.summary,physics=engineering.summary['physics'])
    if validation['errors']:fail('REVIEW_GRAPH_INVALID','Reviewed graph failed existing structural validation.')
    values={'replay_inputs.json':inputs,'assertions.json':[a.model_dump() for a in assertions],
        'classifications.json':[c.model_dump() for c in classes],'issues.json':[i.model_dump() for i in issues],
        'review_questions.json':[q.model_dump() for q in reviews],
        'candidate_assertions.json':[a.model_dump() for a in assertions if a.status not in {'verified','supported_inference'}],
        'engineering_results.json':engineering.model_dump(),'engineering_registry.json':[r.model_dump() for r in engineering.registry],
        'validation.json':validation,'inference_coverage.json':coverage(records,assertions,bundle,plan,context)}
    for name,rows in [('candidate_claims.json',bundle.claims),('candidate_observations.json',bundle.observations),('claim_conflicts.json',bundle.conflicts)]:
        values[name]={'schema_version':VERSION_CANDIDATE,'policy_version':POLICY_VERSION,'records':[r.model_dump() for r in rows]}
    return values,graph


def verify_context_artifacts_from_files(files,records,manifest,limits):
    # The normal publication verifier validates these original files against the
    # parent. Here obtain the already frozen typed plan/results without new calls.
    if manifest['settings']['input_config'].get('inference_mode','legacy')!='contextual':return None,None
    from .retrieval import RetrievalPlan
    return RetrievalPlan.model_validate_json(files['retrieval_plan.json']),json.loads(files['context_batches.json'])


def basis_events(basis, files):
    typed=ExportBasis.model_validate(basis)
    w=typed.workspace
    if w.contract_fingerprint!=contract_fingerprint():fail('REVIEW_VERSION','Changed review schema/policy.')
    service=object.__new__(ReviewService);service.workspace=w
    index=ParentIndex(files,w);events=[]
    if len(basis['events'])>1000:fail('REVIEW_LIMIT','Too many archived decisions.')
    for event in typed.events:
        if service.make_event(event.request,index,events,event.timestamp)!=event:fail('REVIEW_STORE_INVALID','Archived decision replay differs.')
        events.append(event)
    if basis['revision']!=len(events):fail('REVIEW_STALE_REVISION','Archived revision differs.')
    return w,events


def verify_derivation(directory,manifest):
    """Called by the existing publication verifier, including on every resume."""
    directory=Path(directory)
    for name in EXTRA:
        if not (directory/name).is_file():fail('REVIEW_ARTIFACT_MISSING','Derived artifact missing.')
    _,parent,files,fp=package(directory/'parent_package')
    if parent['settings'].get('review_derivation'):fail('REVIEW_VERSION','Nested reviewed-parent replay is not supported in Phase B.')
    basis=json.loads((directory/'review_basis.json').read_text());w,events=basis_events(basis,files)
    if fp!=w.parent_fingerprint or hashed(parent['input_hashes'])!=w.source_fingerprint or parent['run_id']!=w.parent_run_id:
        fail('REVIEW_STALE_EVIDENCE','Archived parent differs from review binding.')
    if (hashlib.sha256(files['ontology.ttl']).hexdigest()!=w.graph_sha256 or
        hashlib.sha256(files['run_manifest.json']).hexdigest()!=w.manifest_sha256):
        fail('REVIEW_STALE_EVIDENCE','Archived graph/manifest binding differs.')
    if parent['settings']['building_scope']!=w.site_scope or manifest['settings']['building_scope']!=w.site_scope:
        fail('REVIEW_SCOPE','Foreign building in derived package.')
    derived=manifest['settings']['review_derivation']
    expected={'version':VERSION,'parent_run_id':w.parent_run_id,'parent_fingerprint':w.parent_fingerprint,
        'workspace_id':w.workspace_id,'revision':len(events),'basis_sha256':hashed(basis),'code_fingerprint':code_fingerprint()}
    if derived!=expected:fail('REVIEW_VERSION','Derived contract or code fingerprint differs.')
    binding=hashed({'basis':basis,'code':code_fingerprint(),'destination':basis['publication_destination']})
    if manifest['run_id']!=uid('reviewed-run',w.workspace_id,binding):fail('REVIEW_VERSION','Derived run identity differs.')
    original_settings=dict(parent['settings'])
    original_settings.update(review_derivation=expected,external_processing=False,durable_state=manifest['settings']['durable_state'])
    if manifest['settings']!=original_settings:fail('REVIEW_VERSION','Original semantic/settings binding changed.')
    if manifest['input_hashes']!=parent['input_hashes']:fail('REVIEW_STALE_EVIDENCE','Derived input hashes differ.')
    for name in ('source_records.jsonl','assertion_observations.json','proposal_observations.json','input_config.json',
                 'retrieval_plan.json','context_batches.json','context_candidates.json'):
        if (directory/name).read_bytes()!=files[name]:fail('REVIEW_STALE_EVIDENCE','Original evidence was rewritten.')
    values,graph=compute(files,w,events)
    if manifest['counts']!=derived_counts(parent['counts'],values,graph):fail('REVIEW_REPLAY_MISMATCH','Derived manifest counts differ.')
    for name,value in values.items():
        if json.loads((directory/name).read_text())!=value:fail('REVIEW_REPLAY_MISMATCH','Derived deterministic artifact differs: '+name)
    if json.loads((directory/'shareable/summary.json').read_text())!=shareable_summary(manifest,parent):
        fail('REVIEW_REPLAY_MISMATCH','Shareable summary differs from reviewed ontology.')
    for line in (directory/'shareable/events.jsonl').read_text().splitlines():
        if json.loads(line)['run_id']!=manifest['run_id']:fail('REVIEW_REPLAY_MISMATCH','Shareable event belongs to another run.')
    after={name:(directory/name).read_bytes() for name in files if (directory/name).is_file()}
    if json.loads((directory/'review_changes.json').read_text())!=diff_contents(files,after):fail('REVIEW_DIFF_MISMATCH','Change history differs.')
    from .clarification import consolidate
    current=ParentIndex(after,w)
    if json.loads((directory/'root_cause_groups.json').read_text())!=consolidate(current,w.site_scope):fail('REVIEW_REPLAY_MISMATCH','Consolidated queue differs.')
    for name,g in [('before_shacl.json',__import__('rdflib').Graph().parse(data=files['ontology.ttl'],format='turtle')),('after_shacl.json',graph)]:
        actual=validate_external(g)
        if actual['full_brick_shacl']!='passed' or actual['released_vocabulary']!='passed' or json.loads((directory/name).read_text())!=actual:
            fail('REVIEW_SHACL_INVALID','Pinned Brick SHACL did not match/pass.')
    return values


def safe_destination(service,destination,resume=False):
    path=Path(destination).absolute()
    if path.is_symlink() or (path.exists() and not resume):fail('REVIEW_EXISTS','Export destination must be new; use explicit resume for an existing job.')
    if not path.parent.is_dir() or path.parent.is_symlink():fail('REVIEW_PATH_UNSAFE','Select an existing output parent.')
    path=path.parent.resolve()/path.name
    protected=[Path(service.workspace.parent_path),service.root]
    protected.append(protected[0].with_name('.'+protected[0].name+'.run'))
    work=path.with_name('.'+path.name+'.review-run')
    for p in (path,work):
        if any(p==q or p in q.parents or q in p.parents for q in protected):fail('REVIEW_IMMUTABLE_PARENT','Export must be separate from parent and workspace.')
    return path,work


def export_review(service,destination,credential_file,expected_revision,resume=False):
    """Freeze a revision while holding the existing review write lock through publish.

    RunStore stage hashes + a single publication lock recover interrupted local
    exports. A completed destination is never rewritten, even on an exact retry.
    """
    if service.workspace.schema_version!='review-v2':fail('REVIEW_VERSION','Create an explicit review-v2 workspace for Phase B replay.')
    if type(expected_revision) is not int or expected_revision<0:fail('REVIEW_STALE_REVISION','Explicit nonnegative revision required.')
    # Validate destination on both paths; existing output is allowed only for resume.
    dest,work=safe_destination(service,destination,resume)
    if resume:
        if not work.is_dir():fail('CHECKPOINT_MISSING','No reviewed export job to resume.')
    else:
        if work.exists() or work.is_symlink():fail('RECOVERY_REQUIRED','Use explicit resume for existing job.')
    with service.transaction(write=True) as (db,credential_hash):
        service.authenticate(credential_file,credential_hash,service.workspace.reviewer.actor_id)
        index=service.parent();events=service.checked_events(db,index)
        if len(events)!=expected_revision:fail('REVIEW_STALE_REVISION','Revision changed before export.')
        _,parent,files,fp=package(service.workspace.parent_path)
        if parent['settings'].get('review_derivation'):fail('REVIEW_VERSION','Use the original workspace to publish another revision; nested reviewed parents are unsupported.')
        basis=ExportBasis(workspace=service.workspace,revision=len(events),events=events,publication_destination=str(dest)).model_dump()
        binding=hashed({'basis':basis,'code':code_fingerprint(),'destination':str(dest)})
        if not resume:work.mkdir(mode=0o700)
        checked_root(work);private_directory(work)
        for p in (work/'export.lock',work/'state.sqlite',work/'state.sqlite-journal',work/'state.sqlite-wal',work/'state.sqlite-shm'):
            if p.is_symlink():fail('REVIEW_PATH_UNSAFE','Redirected export state refused.')
        lock=(work/'export.lock').open('a')
        try:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('RUN_BUSY','Another export process owns this job.')
            if resume and not (work/'state.sqlite').is_file():fail('CHECKPOINT_MISSING','Missing export state.')
            store=RunStore(work/'state.sqlite');(work/'state.sqlite').chmod(0o600)
            try:
                run_id=uid('reviewed-run',basis['workspace']['workspace_id'],binding)
                if resume:
                    row=store.row()
                    if row['version']!=STATE_VERSION or row['fingerprint']!=binding or row['run_id']!=run_id:
                        fail('CHECKPOINT_STALE','Export code/schema/evidence/revision changed.')
                    if store.db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:fail('STATE_INVALID','Export SQLite integrity failed.')
                else:store.create(run_id,binding,{'version':VERSION,'basis':hashed(basis),'destination':str(dest)})
                from .publication import verify_package,finalize,publish
                if dest.exists():
                    if not resume:fail('REVIEW_EXISTS','Destination appeared during export.')
                    verified=package(dest)[1]
                    if verified['run_id']!=run_id:fail('STATE_INVALID','Published run differs from export job.')
                    if store.row()['state']!='COMPLETED':
                        store.record_publication(run_id,dest);store.transition(run_id,'COMPLETED')
                    return verified
                if store.row()['state']=='COMPLETED':fail('CHECKPOINT_INVALID','Completed export package is missing.')
                if store.row()['state']!='RUNNING':store.transition(run_id,'RUNNING')
                staging=work/'package'
                if store.committed(run_id,'package_ready'):
                    package(staging)
                else:
                    if staging.exists():
                        # Retain interrupted staging as diagnostics, never execute it.
                        if staging.is_symlink():fail('REVIEW_PATH_UNSAFE','Redirected staging refused.')
                        os.rename(staging,work/('interrupted-'+os.urandom(8).hex()))
                    staging.mkdir(mode=0o700)
                    for name,data in files.items():
                        p=staging/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);write_new(p,data)
                        p=staging/'parent_package'/name;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);write_new(p,data)
                    values,graph=compute(files,service.workspace,events)
                    for name,value in values.items():write_json(staging/name,value)
                    write_json(staging/'review_basis.json',basis)
                    # Stable Turtle serialization, one full-IRI triple per line.
                    (staging/'ontology.ttl').write_text('\n'.join(sorted(' '.join(t.n3() for t in row)+' .' for row in graph))+'\n')
                    for csvname,jsonname,model,serializer in [('relationships.csv','assertions.json',Assertion,relationship_csv_row),
                            ('classifications.csv','classifications.json',Classification,None),('issues.csv','issues.json',Issue,None),
                            ('review_questions.csv','review_questions.json',ReviewItem,None)]:
                        write_csv(staging/csvname,[model.model_validate(r) for r in values[jsonname]],
                            list(model.model_fields)+(['subject_entity_id','object_entity_id'] if serializer else []),serializer)
                    write_csv(staging/'engineering_results.csv',[RuleResult.model_validate(r) for r in values['engineering_results.json']['records']],list(RuleResult.model_fields))
                    for name,g in [('before_shacl.json',__import__('rdflib').Graph().parse(data=files['ontology.ttl'],format='turtle')),('after_shacl.json',graph)]:
                        result=validate_external(g)
                        if result['full_brick_shacl']!='passed' or result['released_vocabulary']!='passed':fail('REVIEW_SHACL_INVALID','Pinned validation failed.')
                        write_json(staging/name,result)
                    from .clarification import consolidate
                    current=files_snapshot(staging)
                    write_json(staging/'review_changes.json',diff_contents(files,current))
                    write_json(staging/'root_cause_groups.json',consolidate(ParentIndex(current,service.workspace),service.workspace.site_scope))
                    manifest=json.loads(files['run_manifest.json'])
                    manifest.update(run_id=run_id,execution_origin='deterministic_review',validation=values['validation.json'],
                        completion_status='RUNNING',output_hashes={},required_artifacts=[],
                        started_at=store.row()['started_at'],finished_at=now(),
                        provider_audit={'status':'frozen_parent_inference_only','new_external_requests':0})
                    try:
                        manifest['code_revision']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=Path(__file__).parent,text=True,stderr=subprocess.DEVNULL).strip()
                        if subprocess.check_output(['git','status','--porcelain'],cwd=Path(__file__).parent,text=True):manifest['code_revision']+='+dirty'
                    except (OSError,subprocess.CalledProcessError):manifest['code_revision']='unknown'
                    manifest['settings']['review_derivation']={'version':VERSION,'parent_run_id':service.workspace.parent_run_id,
                        'parent_fingerprint':fp,'workspace_id':service.workspace.workspace_id,'revision':len(events),
                        'basis_sha256':hashed(basis),'code_fingerprint':code_fingerprint()}
                    manifest['settings']['external_processing']=False
                    manifest['settings']['durable_state']=str(work/'state.sqlite')
                    manifest['counts']=derived_counts(parent['counts'],values,graph)
                    write_json(staging/'run_manifest.json',manifest)
                    shareable=staging/'shareable';shareable.mkdir(exist_ok=True,mode=0o700)
                    write_json(shareable/'summary.json',shareable_summary(manifest,parent))
                    (shareable/'events.jsonl').write_text(''.join(canonical(e)+'\n' for e in store.events(run_id)))
                    (staging/'README.md').write_text(('# SYNTHETIC / FOR TESTING ONLY\n' if manifest['synthetic'] else '# PROVISIONAL REVIEWED ONTOLOGY\n')+
                        '\nDerived documentary interpretation; no verified outcomes or authenticated engineering authority.\n'
                        'Original complete package: parent_package/. Decisions: review_basis.json. Effects/pending reasons: replay_inputs.json.\n'
                        'Graph, claims, classifications and prior deleted evidence: review_changes.json. Source bytes and raw rows are preserved.\n'
                        'Actual pinned validation: before_shacl.json / after_shacl.json. No external inference was replayed.\n')
                    from .runtime import secure_tree
                    secure_tree(staging)
                    finalize(staging)
                    # Hash all prepared artifacts, not only the completion marker.
                    for p in staging.rglob('*'):
                        if p.is_file():
                            with p.open('rb') as stream:os.fsync(stream.fileno())
                    sync_directory(staging)
                    checkpoint_hook('before_package_checkpoint')
                    store.commit_stage(run_id,'package_ready',[p for p in staging.rglob('*') if p.is_file()])
                checkpoint_hook('before_publication')
                service.parent()
                publish(staging,dest);sync_directory(dest.parent)
                checkpoint_hook('after_publication')
                store.record_publication(run_id,dest);store.transition(run_id,'COMPLETED')
                return json.loads((dest/'run_manifest.json').read_text())
            finally:store.close()
        finally:lock.close()
