"""Bounded contextual calls on the existing Execution/SQLite stage controller."""
import json
import hashlib
import shutil
from datetime import datetime,timezone
from . import providers
from .models import Proposals
from .retrieval import build_plan,RetrievalPlan,batch_content,encoded
from .contextual import ContextResponse,scope_audit,abstention_audit,PROMPT_VERSION
from .identity import uid
from .errors import EngineError,classify


def reserve(plan,records,model,limits):
    limits.require(len(plan.batches)<=limits.max_requests,'planned provider requests')
    sources={r.source_id:r for r in records};total=0
    for batch in plan.batches:
        payload=providers.request_payload([sources[r.source_id] for r in batch.records],model,limits,batch_content(batch,plan.site_scope))
        size=len(json.dumps(payload).encode())
        limits.request(json.dumps(payload));total+=size
    limits.require(total<=limits.max_total_input_tokens,'cumulative conservative input tokens')
    limits.require(len(plan.batches)*limits.max_output_tokens<=limits.max_total_output_tokens,'cumulative output token reservation')
    if limits.max_cost_usd is not None and plan.batches:
        estimate=(total*limits.input_usd_per_million+len(plan.batches)*limits.max_output_tokens*limits.output_usd_per_million)/1_000_000
        limits.require(estimate<=limits.max_cost_usd,'cumulative operator-priced cost reservation')
    return {'planned_requests':len(plan.batches),'input_token_upper_bound':total,
        'output_token_upper_bound':len(plan.batches)*limits.max_output_tokens,
        'cost_status':'no_provider_requests' if not plan.batches else ('not_evaluable' if limits.max_cost_usd is None else 'operator_priced_upper_bound')}


def run_context(context,records,output):
    # Import deferred to avoid any additional state/write implementation.
    from .runtime import atomic_json,checkpoint_hook
    context.active_stage='retrieval';context.budget()
    root=context.work/'context';root.mkdir(mode=0o700,exist_ok=True)
    plan_path=root/'retrieval_plan.json';origin_path=root/'execution_origin.json'
    if context.store.committed(context.run_id,'retrieval'):
        plan=RetrievalPlan.model_validate_json(plan_path.read_text())
    else:
        plan=build_plan(records,context.scope,context.limits);atomic_json(plan_path,plan.model_dump())
        atomic_json(origin_path,{'execution_origin':context.arguments['transport_origin']})
        context.stage('retrieval',[plan_path,origin_path])
    authorized_origin=json.loads(origin_path.read_text())['execution_origin']
    budget=reserve(plan,records,context.arguments['model'],context.limits)
    sources={r.source_id:r for r in records};results=[]
    for number,batch in enumerate(plan.batches):
        context.active_stage='context_batch';context.budget()
        name=f'batch_{number:04d}';folder=root/name;folder.mkdir(mode=0o700,exist_ok=True)
        response_path=folder/'provider_response.json';meta_path=folder/'transport_meta.json';result_path=folder/'result.json'
        response_stage=name+'_response';proposal_stage=name+'_proposals'
        attempt=uid('attempt',context.run_id,batch.batch_id)
        if context.store.committed(context.run_id,proposal_stage):
            result=json.loads(result_path.read_text());ContextResponse.model_validate(result['response']);results.append(result);continue
        if context.store.committed(context.run_id,response_stage):
            response,returned,raw=providers.parse_response(response_path.read_text(),ContextResponse)
            origin=json.loads(meta_path.read_text())['execution_origin']
            if any(a==attempt for a,_ in context.store.attempts(context.run_id)):context.store.finish_attempt(context.run_id,attempt,'COMPLETED')
        elif any(a==attempt for a,_ in context.store.attempts(context.run_id)):
            raise EngineError('EXTERNAL_OUTCOME_UNKNOWN','Context batch has an attempted request without a committed response; no replay.')
        else:
            if providers.execution_origin('openai')!=authorized_origin:
                raise EngineError('PERMISSION_DENIED','Transport origin changed since checkpoint; restore original transport or authorize a separate new run.')
            context.limits.require(len(context.store.attempts(context.run_id))<context.limits.max_requests,'cumulative request attempts')
            origin=providers.execution_origin('openai');atomic_json(meta_path,{'execution_origin':origin,'batch_id':batch.batch_id,
                'request_sha256':hashlib.sha256(json.dumps(providers.request_payload([sources[r.source_id] for r in batch.records],
                    context.arguments['model'],context.limits,batch_content(batch,context.scope))).encode()).hexdigest()})
            def observe(event):
                if event=='request_started':
                    context.store.begin_attempt(context.run_id,attempt,name);checkpoint_hook(name+'_request_started')
                elif event=='response_persisted':context.stage(response_stage,[response_path,meta_path])
            remaining=context.limits.max_run_seconds-(datetime.now(timezone.utc)-datetime.fromisoformat(context.started)).total_seconds()
            context.limits.require(remaining>=1,'remaining run deadline')
            try:
                response,returned,_=providers.openai([sources[r.source_id] for r in batch.records],context.arguments['model'],
                    context.approval or False,response_path,context.limits.model_copy(update={'max_run_seconds':max(1,int(remaining))}),observe,
                    context=batch_content(batch,context.scope),
                    **({'api_key': context.api_key} if context.api_key is not None else {}))
                raw=json.loads(response_path.read_text());context.store.finish_attempt(context.run_id,attempt,'COMPLETED')
            except Exception as exc:
                if any(a==attempt for a,_ in context.store.attempts(context.run_id)):
                    context.store.finish_attempt(context.run_id,attempt,'BLOCKED',classify(exc).as_dict())
                raise
        context.limits.require(len(response.candidates)<=context.limits.max_batch_candidates,'candidates per model batch')
        context.limits.require(len(response.abstentions)<=context.limits.max_batch_targets,'abstentions per model batch')
        result={'batch_id':batch.batch_id,'response':response.model_dump(),'returned_model':returned or context.arguments['model'],
            'execution_origin':origin,'prompt_version':PROMPT_VERSION,'usage':raw.get('usage'),
            'request_sha256':json.loads(meta_path.read_text())['request_sha256'],'candidate_audit':scope_audit(response,batch,records),'abstention_audit':abstention_audit(response,batch)}
        atomic_json(result_path,result);context.stage(proposal_stage,[result_path]);results.append(result)
    # Persist one aggregate completion stage; completed individual batches never get charged/replayed twice.
    aggregate=root/'context_results.json'
    value={'schema_version':'context-results-v1','transport_origin':authorized_origin,'budget':budget,'batches':results}
    if not context.store.committed(context.run_id,'context_complete'):
        atomic_json(aggregate,value);context.stage('context_complete',[aggregate])
    elif json.loads(aggregate.read_text())!=value:raise EngineError('CHECKPOINT_INVALID','Context aggregate differs from committed batches.')
    origins={r['execution_origin'] for r in results}
    context.execution_origin=next(iter(origins)) if len(origins)==1 else ('not_invoked' if not origins else 'mixed')
    context.context_results=value;context.retrieval_plan=plan
    shutil.copyfile(plan_path,output/'retrieval_plan.json');shutil.copyfile(aggregate,output/'context_batches.json')
    audit=[entry for result in results for entry in result['candidate_audit']]
    atomic_json(output/'context_candidates.json',{'schema_version':'context-results-v1','records':audit})
    versions={r['returned_model'] for r in results}
    return providers.documentary(records),next(iter(versions)) if len(versions)==1 else ('multiple_returned_models' if versions else None),None
