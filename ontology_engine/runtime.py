"""Single-run durable controller. No agents, automatic model replay, or BAS tools."""
import hashlib
import fcntl
import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from . import providers
from .config import load_config, InputConfig
from .errors import EngineError, classify, retry_local
from .identity import uid, building_scope
from .limits import Limits, load_limits
from .models import SourceRecord, Proposals, RunManifest
from .security import Approval
from .sources import snapshot_input, SourceSnapshot
from .state import RunStore, VERSION as STATE_VERSION


def checkpoint_hook(stage):
    """No-op instrumentation seam for subprocess crash tests; never driven by source text."""


def inference_fingerprint(source):
    """Inference subpackage sources keyed by RELATIVE path (no basename collisions) plus governing versions."""
    package = source / 'inference'
    files = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob('*.py'))} if package.is_dir() else {}
    from .inference.versions import governing_versions
    return {'files': files, 'versions': governing_versions()}


def fingerprint(arguments):
    source = Path(__file__).parent
    versions = {'state': STATE_VERSION, 'canonical_schema': SourceRecord.model_json_schema(),
                'assertion_schema': __import__('ontology_engine.models', fromlist=['Assertion']).Assertion.model_json_schema(),
                'prompt': providers.PROMPT_VERSION, 'config': arguments['config'], 'limits': arguments['limits'],
                'provider': arguments['provider'], 'model': arguments['model'], 'scope': arguments['scope'],
                'transport_origin':arguments.get('transport_origin'),
                'code': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.glob('*.py'))},
                'inference': inference_fingerprint(source)}
    from .standards import contract
    versions['semantic_contract'] = contract()
    return uid('execution', versions)


def atomic_json(path, value):
    # This retry wraps an idempotent local temp-file replacement only.
    def operation():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + '.tmp')
        temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
        temporary.chmod(0o600); os.replace(temporary, path)
    retry_local(operation)


def secure_tree(directory):
    for path in Path(directory).rglob('*'):
        if path.is_symlink(): raise EngineError('PERMISSION_DENIED', 'Unexpected symlink in run artifacts.')
        path.chmod(0o700 if path.is_dir() else 0o600)
    Path(directory).chmod(0o700)


class Execution:
    def __init__(self, destination, arguments=None, resume=False, api_key=None):
        self.api_key = api_key  # Never persisted in state, manifests or execution fingerprints.
        self.destination = Path(destination).absolute()
        self.work = self.destination.with_name('.' + self.destination.name + '.run')
        self.new = not resume
        if self.work.is_symlink() or (self.work / 'state.sqlite').is_symlink():
            raise EngineError('PERMISSION_DENIED', 'Redirected durable state refused.')
        if resume:
            if not (self.work / 'state.sqlite').is_file(): raise EngineError('CHECKPOINT_MISSING', 'No durable run to resume.')
        else:
            if self.work.exists(): raise EngineError('RECOVERY_REQUIRED', 'Durable run already exists; use explicit resume or a new output.')
            self.work.mkdir(mode=0o700, parents=True)
        self.lock = (self.work / 'run.lock').open('a')
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise EngineError('RUN_BUSY', 'Another process owns this run; concurrent execution refused.') from None
        self.store = RunStore(self.work / 'state.sqlite')
        if resume:
            row = self.store.row()
            self.arguments = json.loads(row['arguments']); self.run_id = row['run_id']; self.started = row['started_at']
            if row['version'] != STATE_VERSION or row['fingerprint'] != fingerprint(self.arguments):
                self.store.close(); self.lock.close()
                raise EngineError('CHECKPOINT_STALE', 'Code/config/schema versions changed; restore matching source or create a new run.')
        else:
            self.arguments = arguments; self.run_id = str(uuid.uuid4())
            self.started = datetime.now(timezone.utc).isoformat()
            self.store.create(self.run_id, fingerprint(arguments), arguments)
        self.config = InputConfig.model_validate(self.arguments['config'])
        self.limits = Limits.model_validate(self.arguments['limits'])
        self.scope = self.arguments['scope']
        self.execution_origin = providers.execution_origin(self.arguments['provider'])
        self.snapshot = None
        self.approval = None
        self.active_stage = 'created'
        self.stage_started = time.monotonic()

    def budget(self):
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(self.started)).total_seconds()
        self.limits.require(elapsed <= self.limits.max_run_seconds, 'run wall-clock deadline')

    def stage(self, name, paths):
        self.store.commit_stage(self.run_id, name, paths, duration=time.monotonic() - self.stage_started)
        self.stage_started = time.monotonic()
        checkpoint_hook(name)
        self.budget()

    def prepare(self):
        self.budget(); self.active_stage = 'snapshot'
        state = self.store.row()['state']
        if state != 'RUNNING': self.store.transition(self.run_id, 'RUNNING')
        if self.store.committed(self.run_id, 'snapshot'):
            metadata = json.loads((self.work / 'snapshot.json').read_text())
            current = snapshot_input(self.arguments['input'], self.limits)
            if current.hashes != metadata['hashes']:
                raise EngineError('CHECKPOINT_STALE', 'Authorized input changed since checkpoint; explicit new run required.')
            files = {name: (self.work / 'inputs' / name).read_bytes() for name in metadata['hashes']}
            self.snapshot = SourceSnapshot(Path(self.arguments['input']), MappingProxyType(files))
        else:
            checkpoint_hook('before_snapshot')
            self.snapshot = snapshot_input(self.arguments['input'], self.limits)
            folder = self.work / 'inputs'; folder.mkdir(mode=0o700, exist_ok=True)
            for name, content in self.snapshot.files.items():
                p = folder / name; p.write_bytes(content); p.chmod(0o600)
            atomic_json(self.work / 'snapshot.json', {'hashes': self.snapshot.hashes})
            self.stage('snapshot', [self.work / 'snapshot.json', *folder.iterdir()])
        if self.arguments['allow_external']:
            self.approval = Approval(uid('snapshot', self.snapshot.hashes), self.arguments['provider'],
                                     self.arguments['model'], tuple(self.snapshot.hashes.values()))
        self.data_origin = 'synthetic' if 'SYNTHETIC.txt' in self.snapshot.files else 'public-derived' if 'DATA_PROVENANCE.json' in self.snapshot.files else 'unknown'

    def normalized(self):
        from .ingest import ingest
        self.active_stage = 'normalized'; self.stage_started = time.monotonic(); self.budget()
        path = self.work / 'records.json'
        if self.store.committed(self.run_id, 'normalized'):
            records = [SourceRecord.model_validate(r) for r in json.loads(path.read_text())]
        else:
            checkpoint_hook('before_ingest')
            records, hashes, _ = ingest(self.snapshot.root, self.config, self.snapshot, self.scope, self.limits)
            if not hashes: raise EngineError('INVALID_INPUT', 'No supported CSV/XLSX files found')
            atomic_json(path, [r.model_dump() for r in records]); self.stage('normalized', [path])
        return records, self.snapshot.hashes, dict(self.snapshot.files)

    def _copy_provider_audit(self, output):
        folder = self.work / 'provider'
        if folder.exists():
            for path in folder.glob('*.json'):
                shutil.copyfile(path, output / path.name)

    def propose(self, records, output):
        self.active_stage = 'provider'; self.stage_started = time.monotonic(); self.budget()
        provider = self.arguments['provider']; model = self.arguments['model']
        if self.config.inference_mode=='contextual':
            if provider!='openai':raise EngineError('INVALID_INPUT','Contextual inference requires the configured model provider, not fixture rules.')
            from .batching import run_context
            try:
                return run_context(self,records,output)
            except Exception as exc:
                root=self.work/'context';plan_path=root/'retrieval_plan.json'
                plan=json.loads(plan_path.read_text()) if plan_path.exists() else None
                completed=list(root.glob('batch_*/result.json')) if root.exists() else []
                failure={'status':'failed','error_code':classify(exc).code,'execution_origin':providers.execution_origin('openai'),
                    'planned_batches':len(plan['batches']) if plan else None,'completed_batches':len(completed),
                    'attempted_requests':len(self.store.attempts(self.run_id)),'live_semantic_performance':'NOT_TESTED'}
                atomic_json(output/'provider_audit.json',failure)
                atomic_json(output/'context_failure.json',failure)
                if plan_path.exists():shutil.copyfile(plan_path,output/'retrieval_plan.json')
                raise
        path = self.work / 'proposals.json'
        folder = self.work / 'provider'; folder.mkdir(mode=0o700, exist_ok=True)
        response_path = folder / 'provider_response.json'
        if self.store.committed(self.run_id, 'proposals'):
            saved = json.loads(path.read_text()); proposals = Proposals.model_validate(saved['proposals'])
            self.execution_origin = saved['execution_origin']
            self._copy_provider_audit(output)
            return proposals, saved['model_version'], None
        if self.store.committed(self.run_id, 'response'):
            proposals, returned, result = providers.parse_response(response_path.read_text())
            saved_audit = json.loads((folder / 'provider_audit.json').read_text())
            self.execution_origin = saved_audit['execution_origin']
            saved_audit.update(status='structured_response_validated', returned_model=returned or model,
                               usage=result.get('usage'), response_id=result.get('id'))
            providers.save(folder / 'provider_audit.json', saved_audit)
            for attempt_id, status in self.store.attempts(self.run_id):
                if status != 'COMPLETED': self.store.finish_attempt(self.run_id, attempt_id, 'COMPLETED')
        elif self.store.attempts(self.run_id):
            raise EngineError('EXTERNAL_OUTCOME_UNKNOWN', 'A previous model attempt has no committed response; automatic replay refused.')
        elif provider == 'fixture':
            proposals, returned = providers.fixture(records), None
        else:
            if providers.execution_origin(provider)!=self.arguments.get('transport_origin',providers.execution_origin(provider)):
                raise EngineError('PERMISSION_DENIED','Transport origin changed; a separate authorized run is required.')
            self.limits.require(self.limits.max_requests >= 1, 'provider request count')
            attempt = uid('attempt', self.run_id, 1)
            def observe(event):
                if event == 'request_started':
                    self.store.begin_attempt(self.run_id, attempt, 'provider')
                    checkpoint_hook('request_started')
                elif event == 'response_persisted':
                    self.stage('response', [response_path])
            try:
                kwargs = {'limits': self.limits, 'observer': observe}
                if self.api_key is not None:
                    kwargs['api_key'] = self.api_key
                proposals, returned, _ = providers.openai(records, model, self.approval or False, response_path, **kwargs)
                self.store.finish_attempt(self.run_id, attempt, 'COMPLETED')
            except Exception as exc:
                error = classify(exc)
                if self.store.attempts(self.run_id): self.store.finish_attempt(self.run_id, attempt, 'BLOCKED', error.as_dict())
                self._copy_provider_audit(output)
                raise
        atomic_json(path, {'proposals': proposals.model_dump(), 'model_version': returned,
                           'execution_origin': self.execution_origin})
        self.stage('proposals', [path])
        self._copy_provider_audit(output)
        return proposals, returned, None

    def candidates(self, assertions, records, proposal_audit):
        from .candidates import CandidateBundle, evaluate_proposals
        self.active_stage = 'candidates'; self.stage_started = time.monotonic(); self.budget()
        inputs = {'assertions': [a.model_dump() for a in assertions], 'proposal_audit': proposal_audit}
        input_path = self.work / 'candidate_inputs.json'
        bundle_path = self.work / 'candidate_bundle.json'
        if self.store.committed(self.run_id, 'candidates'):
            if json.loads(input_path.read_text()) != inputs:
                raise EngineError('CHECKPOINT_INVALID', 'Candidate input differs from committed audit; resume refused.')
            return CandidateBundle.model_validate_json(bundle_path.read_text())
        checkpoint_hook('before_candidates')
        result = evaluate_proposals(assertions, records, self.scope, self.run_id, proposal_audit, self.limits)
        atomic_json(input_path, inputs); atomic_json(bundle_path, result.model_dump())
        self.stage('candidates', [input_path, bundle_path])
        return result

    def engineering(self, records, assertions, bundle):
        from .engineering import evaluate, EngineeringLedger
        self.active_stage = 'engineering'; self.stage_started = time.monotonic(); self.budget()
        inputs = {'assertions': [a.model_dump() for a in assertions], 'bundle': bundle.model_dump(),
                  'source_uids': [r.source_record_uid for r in records]}
        input_path = self.work / 'engineering_inputs.json'
        ledger_path = self.work / 'engineering_results.json'
        if self.store.committed(self.run_id, 'engineering'):
            if json.loads(input_path.read_text()) != inputs:
                raise EngineError('CHECKPOINT_INVALID', 'Engineering inputs differ from committed audit.')
            return EngineeringLedger.model_validate_json(ledger_path.read_text())
        checkpoint_hook('before_engineering')
        result = evaluate(records, assertions, bundle, self.scope, self.limits)
        atomic_json(input_path, inputs); atomic_json(ledger_path, result.model_dump())
        self.stage('engineering', [input_path, ledger_path])
        return result

    def decorate(self, staging):
        self.active_stage = 'export'; self.budget()
        manifest_path = staging / 'run_manifest.json'
        manifest = json.loads(manifest_path.read_text())
        from .standards import contract
        manifest['settings']['semantic_contract'] = contract()
        manifest['data_origin'] = self.data_origin
        manifest['execution_origin'] = self.execution_origin
        if self.config.inference_mode=='contextual':
            manifest['provider_audit']={'provider':'openai','status':'context_batches_completed','usage':None,
                'batch_count':len(self.context_results['batches']),'budget':self.context_results['budget'],
                'execution_origin':self.execution_origin,'live_semantic_performance':'NOT_TESTED'}
        manifest['settings']['limits'] = self.limits.model_dump()
        manifest['settings']['execution_fingerprint'] = fingerprint(self.arguments)
        manifest['settings']['state_schema_version'] = STATE_VERSION
        manifest['settings']['durable_state'] = str(self.work / 'state.sqlite')
        manifest['settings']['external_processing_approval'] = self.approval.as_dict() if self.approval else None
        manifest['settings']['cost_budget_status'] = 'not_evaluable' if self.limits.max_cost_usd is None else 'operator_priced_upper_bound'
        provenance = self.snapshot.files.get('DATA_PROVENANCE.json')
        if provenance:
            try:
                lineage = json.loads(provenance)
                registry = json.loads((Path(__file__).resolve().parents[1] / 'datasets/public_sources.json').read_text())['sources']
                match = next((s for s in registry if s['url'] == lineage.get('source_url')
                    and s.get('extracted_sha256', s['sha256']) == lineage.get('source_sha256')), None)
                manifest['settings']['source_lineage'] = {
                    'source_url': lineage.get('source_url'), 'source_sha256': lineage.get('source_sha256'),
                    'registry_match': bool(match), 'derived_table_truth_verified': False,
                    'redistribution_allowed': bool(match and match['license'].startswith('CC BY 4.0')),
                    'license_note': match['license'] if match else 'Unresolved; redistribution blocked'}
            except (ValueError, TypeError, KeyError):
                manifest['settings']['source_lineage'] = {'registry_match': False, 'redistribution_allowed': False, 'reason': 'Invalid provenance metadata'}
        manifest['settings']['trace_snapshot_end'] = 'pre-publication; final publication event is in SQLite and COMPLETED marker'
        manifest['privacy_policy'] = {'default_package': 'restricted; not distributable without review',
            'restricted_artifacts': ['sources/', 'source_records.jsonl', '*provider*.json', '*assertions*.json', 'candidate_*.json', 'claim_conflicts.json', 'assertion_observations.json', 'retrieval_plan.json', 'context_*.json', 'inference_coverage.json', 'engineering_*.json', 'engineering_*.csv'],
            'shareable_artifacts': ['shareable/summary.json', 'shareable/events.jsonl'],
            'retention': 'Operator managed; no automatic deletion; restricted originals in durable run directory.'}
        atomic_json(manifest_path, manifest)
        if self.execution_origin == 'mocked':
            atomic_json(staging / 'MOCK_TRANSPORT.json', {'transport': 'MOCKED', 'real_hosted_model_invoked': False})
        shareable = staging / 'shareable'; shareable.mkdir()
        atomic_json(shareable / 'summary.json', {'run_id': self.run_id, 'data_origin': self.data_origin,
            'execution_origin': self.execution_origin, 'counts': manifest['counts'], 'validation': manifest['validation'],
            'sponsor_accuracy': 'NOT TESTED', 'real_inference': 'NOT TESTED' if self.execution_origin != 'live' else 'requires independent evaluation'})
        events = self.store.events(self.run_id)
        (shareable / 'events.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        secure_tree(staging)

    def failed(self, exc):
        error = classify(exc)
        error.attempt_count = max(error.attempt_count, len(self.store.attempts(self.run_id)))
        code = error.code
        if 'SYMLINK' in str(exc): code = 'PERMISSION_DENIED'
        state = 'BLOCKED' if code in {'PERMISSION_DENIED', 'EXTERNAL_OUTCOME_UNKNOWN', 'BUDGET_EXCEEDED', 'COST_NOT_EVALUABLE', 'CHECKPOINT_STALE'} else 'FAILED'
        current = self.store.row()['state']
        if state in __import__('ontology_engine.state', fromlist=['TRANSITIONS']).TRANSITIONS.get(current, set()):
            self.store.transition(self.run_id, state)
        self.store.event(self.run_id, self.active_stage, 'stop', state, error_code=code)
        atomic_json(self.work / 'failure.json', {**error.as_dict(), 'error_code': code, 'run_id': self.run_id,
                                               'stage': self.active_stage, 'state': state})


def execute(input_dir, output_dir, provider='fixture', model=None, allow_external=False, building_id=None,
            config_path=None, limits=None, resume=False, api_key=None):
    from .pipeline import _run_core
    from .publication import finalize, publish, verify_package
    destination = Path(output_dir).absolute()
    if destination.is_symlink(): raise EngineError('PERMISSION_DENIED', 'Output symlink refused.')
    if not resume:
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Output directory must be empty; use a new directory to preserve audit history')
        original = Path(input_dir).resolve()
        if destination.resolve() == original or original in destination.resolve().parents or destination.resolve() in original.parents:
            raise ValueError('Input and output directories must be separate, non-nested directories')
        if provider not in {'fixture', 'openai'}: raise ValueError('Unsupported LLM provider')
        if provider == 'openai' and not allow_external:
            raise ValueError('External processing requires --allow-external-processing owner authorization')
        if Path(input_dir).is_symlink(): raise ValueError('SYMLINK_REJECTED: input root')
        arguments = {'input': str(original), 'output': str(destination), 'provider': provider, 'model': model,
            'allow_external': allow_external, 'scope': building_scope(original, building_id),
            'transport_origin':providers.execution_origin(provider), 'config': load_config(config_path).model_dump(), 'limits': (limits or Limits()).model_dump()}
    else:
        arguments = None
    context = Execution(destination, arguments, resume, api_key=api_key)
    staging = destination.with_name('.' + destination.name + '.staging-' + uuid.uuid4().hex)
    try:
        if resume and destination.exists() and (destination / 'COMPLETED.json').exists():
            manifest = verify_package(destination)
            if manifest['run_id'] != context.run_id: raise EngineError('STATE_INVALID', 'Published run ID differs from durable state.')
            if context.store.row()['state'] != 'COMPLETED':
                context.store.record_publication(context.run_id, destination)
                context.store.transition(context.run_id, 'COMPLETED')
            return RunManifest.model_validate(manifest)
        context.prepare()
        args = context.arguments
        result = _run_core(args['input'], staging, args['provider'], args['model'], args['allow_external'],
                           args['scope'], None, _context=context)
        context.decorate(staging)
        finalize(staging)
        context.stage('package_ready_' + staging.name, [staging / 'run_manifest.json', staging / 'COMPLETED.json'])
        checkpoint_hook('before_publication')
        publish(staging, destination)
        checkpoint_hook('after_publication')
        context.store.record_publication(context.run_id, destination)
        context.store.transition(context.run_id, 'COMPLETED')
        return RunManifest.model_validate(json.loads((destination / 'run_manifest.json').read_text()))
    except Exception as exc:
        context.failed(exc)
        if staging.exists():
            if (staging / 'failure.json').exists() and not (staging / 'ontology.ttl').exists():
                atomic_json(staging / 'run_manifest.json', {'completion_status': 'FAILED', 'error_type': type(exc).__name__})
                if not destination.exists(): staging.rename(destination)
            else:
                atomic_json(staging / 'failure.json', {'completion_status': 'FAILED', 'error_type': type(exc).__name__})
        raise
    finally:
        context.store.close(); context.lock.close()
