"""Immutable source-bound sidecars, using existing parent verification and RunStore."""
import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path

from rdflib import Graph
from ..identity import uid
from ..review import (package,files_snapshot,read_bytes,private_directory,write_new,sync_directory,sha_bytes)
from ..review_models import hashed
from ..runtime import secure_tree
from ..state import RunStore, VERSION as STATE_VERSION
from ..standards import validate_external
from .models import Report,ReportManifest,VERSION,POLICY
from .report import generate
from .render import json_bytes,render

PUBLIC_FILES={'readiness_report.json','readiness_report.html','report_schema.json','brick_shacl.json'}


def safe_path(path):
    p=Path(path).absolute()
    if any(x.is_symlink() for x in (p,*p.parents)): raise ValueError('READINESS_PATH_UNSAFE')
    return p.resolve()


def implementation_fingerprint():
    return hashed({'schema':Report.model_json_schema(),'manifest':ReportManifest.model_json_schema(),
        'version':VERSION,'policy':POLICY,
        'shared_files':{p.name:sha_bytes(p.read_bytes()) for p in sorted(Path(__file__).parent.parent.glob('*.py'))},
        'shared_render':sha_bytes((Path(__file__).parent.parent/'commissioning'/'render.py').read_bytes()),'files':{p.name:sha_bytes(p.read_bytes()) for p in sorted(Path(__file__).parent.glob('*.py'))}})


def actual_shacl(files):
    result=validate_external(Graph().parse(data=files['ontology.ttl'],format='turtle'))
    # Released shapes may use blank nodes; volatile diagnostic text is not a
    # reproducibility key. Store the actual stable outcome/contract projection.
    return {k:v for k,v in result.items() if k!='report'}


def outputs(files,fp):
    shacl=actual_shacl(files)
    report=generate(files,fp,shacl)
    return report,{**render(report),'report_schema.json':json_bytes(Report.model_json_schema()),'brick_shacl.json':json_bytes(shacl)}


def verify_report(directory,parent=None):
    root=safe_path(directory)
    if not root.is_dir(): raise ValueError('READINESS_REPORT_MISSING')
    before=files_snapshot(root)
    try:
        m=ReportManifest.model_validate_json(before['report_manifest.json'])
        marker=json.loads(before['COMPLETED.json'])
        if marker!={'report_id':m.report_id,'manifest_sha256':sha_bytes(before['report_manifest.json'])}: raise ValueError('READINESS_MARKER_INVALID')
        if m.implementation_fingerprint!=implementation_fingerprint(): raise ValueError('READINESS_VERSION')
        names=set(before)-{'report_manifest.json','COMPLETED.json'}
        if set(m.output_hashes)!=names: raise ValueError('READINESS_ARTIFACT_SET')
        for name,digest in m.output_hashes.items():
            p=Path(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name: raise ValueError('READINESS_PATH_UNSAFE')
            if sha_bytes(before[name])!=digest: raise ValueError('READINESS_HASH_MISMATCH')
        _,pm,files,fp=package(root/'parent_package')
        if set(before)!=(PUBLIC_FILES|{'report_manifest.json','COMPLETED.json'}|{'parent_package/'+n for n in files}): raise ValueError('READINESS_ARTIFACT_SET')
        if (m.parent_fingerprint!=fp or m.parent_run_id!=pm['run_id'] or m.site_scope!=pm['settings']['building_scope']
            or m.source_fingerprint!=hashed(pm['input_hashes']) or m.graph_sha256!=sha_bytes(files['ontology.ttl'])): raise ValueError('READINESS_PARENT_MISMATCH')
        if parent is not None and package(safe_path(parent))[3]!=fp: raise ValueError('READINESS_PARENT_MISMATCH')
        report,expected=outputs(files,fp)
        if m.report_id!=report.report_id: raise ValueError('READINESS_ID_MISMATCH')
        for n,raw in expected.items():
            if before[n]!=raw: raise ValueError('READINESS_REPORT_MISMATCH')
        if files_snapshot(root)!=before: raise ValueError('READINESS_CHANGED_DURING_VERIFY')
    except KeyError as exc:
        raise ValueError('READINESS_ARTIFACT_MISSING') from exc
    return m.model_dump()


def checkpoint_hook(stage):
    """Process-crash test seam; never driven by uploaded data or CLI parameters."""


def create_report(parent,destination,resume=False):
    parent=safe_path(parent);dest=safe_path(destination)
    if not dest.parent.is_dir(): raise ValueError('READINESS_OUTPUT_PARENT_MISSING')
    parent,pm,files,fp=package(parent)
    work=dest.with_name('.'+dest.name+'.readiness-run')
    protected=[parent,parent.with_name('.'+parent.name+'.run'),parent.with_name('.'+parent.name+'.review-run')]
    for p in (dest,work):
        safe_path(p)
        if any(p==q or p in q.parents or q in p.parents for q in protected): raise ValueError('READINESS_IMMUTABLE_PARENT')
    if not resume and (dest.exists() or work.exists()): raise ValueError('READINESS_EXISTS')
    if resume and not work.is_dir(): raise ValueError('READINESS_RECOVERY_MISSING')
    binding=hashed({'parent':fp,'implementation':implementation_fingerprint(),'destination':str(dest)})
    if not resume:work.mkdir(mode=0o700)
    private_directory(work)
    for n in ('publish.lock','state.sqlite','state.sqlite-journal','state.sqlite-wal','state.sqlite-shm'):
        p=safe_path(work/n)
        if p.exists():
            mode=p.stat()
            if not stat.S_ISREG(mode.st_mode) or mode.st_uid!=os.getuid() or mode.st_mode&0o077:raise ValueError('READINESS_STATE_UNSAFE')
    if resume and not (work/'state.sqlite').is_file():raise ValueError('READINESS_RECOVERY_MISSING')
    fd=os.open(work/'publish.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(fd,'a') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError as exc:raise ValueError('READINESS_BUSY') from exc
        store=RunStore(work/'state.sqlite');(work/'state.sqlite').chmod(0o600)
        try:
            run_id=uid('readiness-job',binding)
            if resume:
                row=store.row()
                if row['version']!=STATE_VERSION or row['fingerprint']!=binding or row['run_id']!=run_id:raise ValueError('READINESS_RECOVERY_STALE')
                if store.db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('READINESS_STATE_INVALID')
            else:store.create(run_id,binding,{'parent_fingerprint':fp,'version':VERSION,'destination':str(dest)})
            if dest.exists():
                m=verify_report(dest,parent)
                if store.row()['state']!='COMPLETED':store.record_publication(run_id,dest);store.transition(run_id,'COMPLETED')
                return m
            if store.row()['state']=='COMPLETED':raise ValueError('READINESS_COMPLETED_REPORT_MISSING')
            if store.row()['state']!='RUNNING':store.transition(run_id,'RUNNING')
            staging=work/'package'
            if store.committed(run_id,'report_ready'):
                verify_report(staging,parent)
            else:
                if staging.exists():
                    safe_path(staging);os.rename(staging,work/('interrupted-'+os.urandom(8).hex()))
                staging.mkdir(mode=0o700)
                for n,raw in files.items():
                    p=staging/'parent_package'/n;p.parent.mkdir(parents=True,exist_ok=True,mode=0o700);write_new(p,raw)
                report,values=outputs(files,fp)
                for n,raw in values.items():write_new(staging/n,raw)
                hashes={n:sha_bytes(raw) for n,raw in files_snapshot(staging).items()}
                m=ReportManifest(report_id=report.report_id,site_scope=report.site_scope,parent_run_id=pm['run_id'],parent_fingerprint=fp,
                    source_fingerprint=hashed(pm['input_hashes']),graph_sha256=sha_bytes(files['ontology.ttl']),implementation_fingerprint=implementation_fingerprint(),output_hashes=hashes)
                manifest=json_bytes(m.model_dump());write_new(staging/'report_manifest.json',manifest)
                write_new(staging/'COMPLETED.json',json_bytes({'report_id':report.report_id,'manifest_sha256':sha_bytes(manifest)}))
                secure_tree(staging);verify_report(staging,parent)
                for folder in sorted((p for p in staging.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):sync_directory(folder)
                sync_directory(staging)
                checkpoint_hook('before_checkpoint')
                store.commit_stage(run_id,'report_ready',[p for p in staging.rglob('*') if p.is_file()])
            checkpoint_hook('before_publication')
            if package(parent)[3]!=fp:raise ValueError('READINESS_PARENT_CHANGED')
            if dest.exists():raise ValueError('READINESS_EXISTS')
            os.rename(staging,dest);sync_directory(dest.parent);checkpoint_hook('after_publication')
            store.record_publication(run_id,dest);store.transition(run_id,'COMPLETED')
            return json.loads(read_bytes(dest/'report_manifest.json',64_000_000))
        finally:store.close()
