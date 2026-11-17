"""Deterministic site-scoped retrieval over approved normalized records only (FR-02/04/11)."""
import json
import re
from collections import defaultdict
from typing import Dict,List,Literal
from pydantic import Field
from .models import Model
from .identity import uid,unique_source_map
from .limits import Limits

VERSION='retrieval-v1'

class RetrievedRecord(Model):
    source_id:str
    source_record_uid:str
    source_hash:str
    table_kind:str
    fields:Dict[str,str]
    reason_codes:List[str]
    truncated_fields:List[str]
    evidence_authority:Literal['supplied_unconfirmed']='supplied_unconfirmed'

class ContextBatch(Model):
    batch_id:str
    target_source_ids:List[str]
    records:List[RetrievedRecord]
    context_bytes:int
    omitted_relevant_records:int

class RetrievalPlan(Model):
    schema_version:Literal['retrieval-v1']=VERSION
    site_scope:str
    source_uids:List[str]
    target_source_ids:List[str]
    batches:List[ContextBatch]
    excluded_records:Dict[str,str]
    selection_operations:int
    limits:Dict[str,int]


def encoded(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()

def batch_content(batch,scope):
    return {'selection_version':VERSION,'site_scope':scope,'batch_id':batch.batch_id,
            'target_source_ids':batch.target_source_ids,'untrusted_records':[r.model_dump() for r in batch.records]}


def naming_group(fields):
    value=fields.get('point_id') or fields.get('equipment_id') or ''
    return re.split(r'[._/\-]',value, maxsplit=1)[0] if re.search(r'[._/\-]',value) else ''


class EvidenceIndex:
    def __init__(self,records,scope,limits=None):
        self.limits=limits or Limits();self.scope=scope;self.sources=unique_source_map(records)
        self.sort_key=lambda r:(r.table_kind,r.normalized_fields.get('point_id') or r.normalized_fields.get('equipment_id') or r.normalized_fields.get('connection_id') or r.source_id,r.source_id)
        self.records=sorted(records,key=self.sort_key)
        self.by_field=defaultdict(lambda:defaultdict(list));self.operations=0
        for r in self.records:
            if r.parse_status!='ok' or r.table_kind not in {'points','equipment','connections'}:continue
            for field in ('equipment_id','point_id','controller_id','channel_id','system_id','parent_equipment_id','from_equipment','to_equipment'):
                value=r.normalized_fields.get(field)
                if value:self.by_field[field][value].append(r.source_id)
            group=naming_group(r.normalized_fields)
            if group:self.by_field['naming_group'][group].append(r.source_id)
    def charge(self):
        self.operations+=1;self.limits.require(self.operations<=self.limits.max_retrieval_operations,'retrieval selection operations')
    def select(self,target):
        selected={target.source_id:(0,{'TARGET_RECORD'})};f=target.normalized_fields
        def add(field,value,rank,reason,condition=lambda r:True):
            if not value:return
            for key in self.by_field[field].get(value,[]):
                self.charge();r=self.sources[key]
                if not condition(r):continue
                old_rank,reasons=selected.get(key,(rank,set()))
                selected[key]=(min(rank,old_rank),reasons|{reason})
        eid=f.get('equipment_id')
        add('equipment_id',eid,1,'EXPLICIT_EQUIPMENT_ID')
        add('point_id',f.get('point_id'),1,'EXACT_POINT_ID')
        add('controller_id',f.get('controller_id'),2,'CONTROLLER_CHANNEL_JOIN',
            lambda r:bool(f.get('channel_id')) and r.normalized_fields.get('channel_id')==f['channel_id'])
        add('controller_id',f.get('controller_id'),3,'SHARED_CONTROLLER_WEAK')
        add('system_id',f.get('system_id'),5,'SHARED_SYSTEM_METADATA')
        add('equipment_id',f.get('parent_equipment_id'),1,'EXPLICIT_PARENT_ID')
        add('parent_equipment_id',eid,2,'DOCUMENTED_COMPONENT')
        for field in ('from_equipment','to_equipment'):add(field,eid,2,'EXPLICIT_CONNECTION_ENDPOINT')
        for field in ('from_equipment','to_equipment'):
            add('equipment_id',f.get(field),1,'CONNECTION_EQUIPMENT')
        add('naming_group',naming_group(f),6,'NAMING_PATTERN_WEAK')
        return sorted(selected,key=lambda k:(selected[k][0],self.sort_key(self.sources[k]))),selected
    def project(self,key,reasons):
        from .providers import redact
        r=self.sources[key];fields={};truncated=[]
        priority={'points':('point_id','equipment_id','controller_id','channel_id','point_class','units','description'),
                  'equipment':('equipment_id','equipment_type','controller_id','channel_id','measurement_role','parent_equipment_id','system_id'),
                  'connections':('from_equipment','to_equipment','predicate','medium','connection_scope','direction_exclusive','medium_exclusive')}.get(r.table_kind,())
        ordered=sorted(r.normalized_fields.items(),key=lambda item:(priority.index(item[0]) if item[0] in priority else len(priority),item[0]))
        for name,value in ordered:
            if len(fields)>=self.limits.max_context_fields:truncated.append(name);continue
            clean=redact({name:value},redact_environment=False)[name]
            fields[name]=clean[:self.limits.max_field_chars]
            if clean!=value or len(clean)>self.limits.max_field_chars:truncated.append(name)
        return RetrievedRecord(source_id=key,source_record_uid=r.source_record_uid,source_hash=r.source_hash,
            table_kind=r.table_kind,fields=fields,reason_codes=sorted(reasons),truncated_fields=truncated)


def build_plan(records,scope,limits=None):
    limits=limits or Limits();index=EvidenceIndex(records,scope,limits)
    targets=[r for r in index.records if r.parse_status=='ok' and r.table_kind in {'points','equipment','connections'}]
    target_ids={r.source_id for r in targets}
    excluded={r.source_id:'INVALID_OR_UNSUPPORTED_RECORD' for r in index.records if r.source_id not in target_ids}
    # Reserve space for trusted prompt/schema envelope; actual provider payload is preflighted too.
    cap=min(limits.max_context_bytes,limits.max_input_tokens)-min(8000,min(limits.max_context_bytes,limits.max_input_tokens)//2)
    pending=[];batches=[]
    def make(items):
        combined={};omitted=0
        for target,rows,skip in items:
            omitted+=skip
            for row in rows:
                if row.source_id in combined:
                    old=combined[row.source_id];old.reason_codes=sorted(set(old.reason_codes)|set(row.reason_codes))
                else:combined[row.source_id]=row.model_copy(deep=True)
        ids=[item[0] for item in items];rows=list(combined.values())
        key=uid('context-batch',scope,VERSION,ids,[r.model_dump() for r in rows])
        batch=ContextBatch(batch_id=key,target_source_ids=ids,records=rows,context_bytes=0,omitted_relevant_records=omitted)
        batch.context_bytes=len(encoded(batch_content(batch,scope)))
        return batch
    for target in targets:
        keys,selection=index.select(target);rows=[];skip=0
        for key in keys:
            row=index.project(key,selection[key][1]);proposed=make([(target.source_id,rows+[row],0)])
            if len(rows)<limits.max_context_records and proposed.context_bytes<=cap:rows.append(row)
            else:skip+=1
        limits.require(any(r.source_id==target.source_id for r in rows),'target record cannot fit bounded context')
        entry=(target.source_id,rows,skip);trial=make(pending+[entry])
        if pending and (len(pending)>=limits.max_batch_targets or len(trial.records)>limits.max_context_records or trial.context_bytes>cap):
            batches.append(make(pending));pending=[]
        pending.append(entry)
        limits.require(len(batches)<limits.max_batches,'retrieval batch count')
    if pending:batches.append(make(pending))
    limits.require(len(batches)<=limits.max_batches,'retrieval batch count')
    return RetrievalPlan(site_scope=scope,source_uids=sorted(r.source_record_uid for r in records),
        target_source_ids=[r.source_id for r in targets],batches=batches,excluded_records=excluded,selection_operations=index.operations,
        limits={k:getattr(limits,k) for k in ('max_context_records','max_batch_targets','max_field_chars','max_context_fields','max_batches','max_context_bytes','max_input_tokens','max_retrieval_operations')})
