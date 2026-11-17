"use strict";
/* UX-F: native review-v2 interface for the existing authenticated local review
   service. No engineering authority or decision logic is invented here.
   Source-derived strings remain inert through textContent-based rendering. */
const reviewState={
  run:"",workspace:null,revision:0,target:null,evidence:null,
  queueOffset:0,queueTotal:0,historyOffset:0,historyTotal:0,
  preview:null,previewAction:null,busy:false,requestVersion:0,derived:false
};
const rv=id=>document.getElementById(id);
const rtext=(id,value)=>{rv(id).textContent=String(value);};
const rlabel=(name,value,cls="")=>{const el=document.createElement("div");el.className="review-pair "+cls;el.append(node("span",name,"small"),node("strong",value===undefined?"Not available":String(value)));return el;};
function rhelp(parent,value){parent.append(node("p",value,"small"));}
function rclear(id){rv(id).replaceChildren();}
function reviewAvailable(){return Boolean(state.run)&&!reviewState.derived;}
function reviewSelectedClaims(){return [...document.querySelectorAll('#reviewClaimList input[data-claim]:checked')].map(e=>e.value);}
function reviewSelectedSources(){return [...document.querySelectorAll('#reviewSourceList input[data-source]:checked')].map(e=>e.value);}
function reviewDisableSubmit(){
  reviewState.preview=null;reviewState.previewAction=null;
  rv("reviewRecord").disabled=true;
  rv("reviewConfirm").checked=false;
  rv("reviewConfirm").disabled=true;
  rv("reviewPreviewDetails").replaceChildren();
  rtext("reviewPreviewState","The previous preview is invalid or no proposal has been previewed.");
}
function reviewSetBusy(busy){
  reviewState.busy=busy;
  rv("reviewEnroll").disabled=busy||!reviewAvailable()||Boolean(reviewState.workspace);
  rv("reviewRefresh").disabled=busy||!state.run;
  rv("reviewActionFields").disabled=busy||!reviewState.workspace||!reviewState.evidence||!reviewAvailable();
  rv("reviewPublish").disabled=busy||!reviewState.workspace||reviewState.revision===0;
  rv("reviewHistoryRefresh").disabled=busy||!reviewState.workspace;
  rv("reviewPreview").disabled=busy||!reviewState.workspace||!reviewState.evidence||!reviewSelectedClaims().length||!reviewSelectedSources().length;
  rv("reviewRecord").disabled=busy||!reviewState.previewAction||!rv("reviewConfirm").checked;
}
function reviewOnError(error){say(error?.message||"Review action refused by existing server policy.",true);}
function reviewRunChanged(){
  if(state.run===reviewState.run)return;
  reviewState.run=state.run||""; reviewState.workspace=null;reviewState.revision=0;
  reviewState.target=null;reviewState.evidence=null;reviewState.queueOffset=0;reviewState.queueTotal=0;
  reviewState.historyOffset=0;reviewState.historyTotal=0;reviewState.derived=false;
  reviewState.requestVersion++;
  reviewResetSelection();reviewDisableSubmit();
  rtext("reviewWorkspaceName",state.run||"No run selected");
  rtext("reviewWorkspaceStatus","Select a completed original run to begin a documentary review.");
  rtext("reviewRevision","Not enrolled");
  rtext("reviewPublishStatus","No review workspace enrolled.");
  rclear("reviewPublishedLink");
  rclear("reviewHistoryList");rhelp(rv("reviewHistoryList"),"Enable review to inspect the append-only decision ledger.");
  reviewSetBusy(false);
  if(state.view==="review")safe(reviewRoute);
}
function reviewResetSelection(){
  reviewState.target=null;reviewState.evidence=null;
  rtext("reviewSelectedTitle","Select a clarification");
  rtext("reviewSelectedContext","Claims, sources, competing interpretations, and contradictions appear here.");
  for(const id of ["reviewSelectedCounts","reviewClaimList","reviewSourceList","reviewAlternativeList","reviewConflictList","reviewEditList"])rclear(id);
  rhelp(rv("reviewClaimList"),"No evidence selected.");
  rv("reviewClear").disabled=true;
  reviewSetBusy(reviewState.busy);
}
async function reviewRoute(){
  if(!state.run){
    rtext("reviewOrigin","No building selected");
    rtext("reviewQueueCount","—");
    rtext("reviewWorkspaceName","No run selected");
    rclear("reviewNativeGroups");rhelp(rv("reviewNativeGroups"),"Select a completed run above.");
    reviewSetBusy(false);return;
  }
  if(reviewState.run!==state.run)reviewRunChanged();
  const version=++reviewState.requestVersion;
  rtext("reviewOrigin",state.runMeta?.synthetic?"SYNTHETIC / FOR TESTING ONLY":"PRIVATE BUILDING DATA");
  rtext("reviewWorkspaceName",state.run);
  // Read-only verified package view: derived reviewed outputs are not enrolled again.
  const runInfo=await api(url("/api/view",{run:state.run}));
  if(version!==reviewState.requestVersion||state.run!==reviewState.run)return;
  reviewState.derived=Boolean(runInfo.changes);
  rtext("reviewWorkspaceStatus",reviewState.derived
    ?"Immutable reviewed output. Return to the original parent run to record new decisions."
    :"Original immutable run. Review enrollment creates a separate owner-only decision ledger.");
  if(reviewState.derived){
    reviewState.workspace=null;reviewState.revision=0;
    rtext("reviewRevision","Derived package — read-only");
  }
  reviewSetBusy(false);
  await reviewLoadGroups(version);
  if(version!==reviewState.requestVersion)return;
  if(reviewState.workspace)await reviewHistory(version);
}
async function reviewLoadGroups(version=reviewState.requestVersion){
  const host=rv("reviewNativeGroups");
  if(!state.run){rclear("reviewNativeGroups");return;}
  rtext("reviewQueueCount","Loading");
  const data=await api(url("/api/groups",{run:state.run,offset:reviewState.queueOffset}));
  if(version!==reviewState.requestVersion)return;
  reviewState.queueTotal=data.total||0;
  rtext("reviewQueueCount",reviewState.queueTotal+" groups");
  rtext("reviewPageLabel","Page "+(Math.floor(reviewState.queueOffset/50)+1));
  rv("reviewPrevious").disabled=reviewState.busy||reviewState.queueOffset===0;
  rv("reviewNext").disabled=reviewState.busy||reviewState.queueOffset+50>=reviewState.queueTotal;
  host.replaceChildren();
  if(!data.items.length){rhelp(host,"No open clarification groups in the selected package.");return;}
  for(const group of data.items){
    const title=group.question||group.cause_id||group.group_id;
    const b=node("button",undefined,"review-group");b.type="button";
    b.dataset.id=group.group_id;
    b.setAttribute("aria-pressed",String(reviewState.target?.id===group.group_id));
    const heading=node("div",undefined,"review-group-head");
    heading.append(node("strong",String(title)),node("span","Priority "+group.priority_score,"review-priority"));
    b.append(heading);
    b.append(node("span",(group.cause_kind||"individual")+" · "+group.claims.length+" linked claims · "+group.sources.length+" source records","small"));
    const extra=node("span","Review scope is source-declared · "+group.dependent_claim_uids.length+" candidate dependencies","small");
    b.append(extra);
    b.onclick=()=>safe(()=>reviewInspect("group",group.group_id));
    host.append(b);
  }
}
async function reviewInspect(kind,id){
  if(!state.run)throw Error("Select a completed run first.");
  reviewResetSelection();reviewDisableSubmit();rv("reviewClear").disabled=false;
  const version=++reviewState.requestVersion;
  reviewState.target={kind,id};
  rtext("reviewSelectedTitle","Loading source evidence…");
  const data=await api(url("/api/inspect",{run:state.run,kind,id,schema:"review-v2"}));
  if(version!==reviewState.requestVersion)return;
  reviewState.evidence=data;
  if(data.workspace && !reviewState.derived){
    reviewState.workspace=data.workspace;
    reviewState.revision=Number(data.revision||0);
    rtext("reviewRevision","Revision "+reviewState.revision+" · pending decisions");
  }
  const question=data.question?.question_or_test||data.question||data.cause_id||"Exact claim and documentary evidence";
  rtext("reviewSelectedTitle",kind==="group"?"Clarification group":"Selected assertion");
  rtext("reviewSelectedContext",typeof question==="string"?question:"Review documented source evidence and alternatives.");
  rclear("reviewSelectedCounts");
  for(const [count,label] of [[data.claims.length,"claims"],[data.sources.length,"sources"],[data.alternatives.length,"alternatives"],[data.conflicts.length,"conflicts"]]){
    rv("reviewSelectedCounts").append(rlabel(label,count,"review-count"));
  }
  for(const b of rv("reviewNativeGroups").querySelectorAll(".review-group")){
    b.setAttribute("aria-pressed",String(b.dataset.id===id));
  }
  reviewRenderEvidence(data);
  reviewRenderEdits();
  reviewSetBusy(false);
}
function reviewSection(id,title,list,formatter,empty){
  const host=rv(id);host.replaceChildren();
  const heading=node("h3",title);host.append(heading);
  if(!list.length){rhelp(host,empty);return;}
  for(const item of list.slice(0,80))host.append(formatter(item));
  if(list.length>80)rhelp(host,"Showing 80 of "+list.length+" records. Download the complete package for all evidence.");
}
function reviewRenderEvidence(data){
  reviewSection("reviewClaimList","Explicit claims",data.claims,c=>{
    const label=node("label",undefined,"review-choice");
    const input=node("input");input.type="checkbox";input.value=c.claim_uid;input.dataset.claim="yes";
    label.append(input);
    const summary=node("span");summary.append(node("strong",c.subject_id+" · "+c.predicate+" · "+c.object_id));
    summary.append(node("span",c.status||"needs_review","status-chip "+c.status));summary.append(node("small",c.claim_uid));
    label.append(summary);input.onchange=()=>{reviewDisableSubmit();reviewRenderEdits();reviewSetBusy(false);};
    return label;
  },"No linked claims in this target.");
  reviewSection("reviewSourceList","Original source records",data.sources,s=>{
    const label=node("label",undefined,"review-choice review-source");
    const input=node("input");input.type="checkbox";input.value=s.source_record_uid;input.dataset.source="yes";
    label.append(input);
    const summary=node("span");summary.append(node("strong",s.source_file||"Source record"));
    summary.append(node("small",s.source_location||s.source_id||"Source location not provided"));
    if(s.source_hash)summary.append(node("small","SHA-256 "+s.source_hash));
    if(s.raw_value!==undefined){
      const details=node("details");details.append(node("summary","Inspect original value"));
      const pre=node("pre",typeof s.raw_value==="string"?s.raw_value:JSON.stringify(s.raw_value,null,2));
      details.append(pre);summary.append(details);
    }
    label.append(summary);input.onchange=()=>{reviewDisableSubmit();reviewSetBusy(false);};
    return label;
  },"No documentary source records for this target. Corrections must not be invented.");
  reviewSection("reviewAlternativeList","Alternative interpretations",data.alternatives,a=>{
    const item=node("div",undefined,"review-evidence-card");
    item.append(node("strong",a.subject_id+" · "+a.predicate+" · "+a.object_id));
    item.append(node("p","Status: "+a.status+" · Candidate, not an accepted fact.","small"));
    return item;
  },"No alternative claims recorded.");
  reviewSection("reviewConflictList","Recorded contradictions",data.conflicts,c=>{
    const item=node("div",undefined,"review-evidence-card");
    item.append(node("strong",c.conflict_uid||"Documented conflict"));
    item.append(node("p","Claim IDs: "+(c.claim_uids||[]).join(", "),"small"));
    return item;
  },"No recorded conflicts for this selection. This does not prove independence or completeness.");
}
function reviewKindChange(){
  const kind=rv("reviewCorrectionKind").value;
  rv("reviewAliasWrap").hidden=kind!=="naming_alias";
  rv("reviewFieldWrap").hidden=kind!=="source_clarification";
  rv("reviewEditList").hidden=kind==="source_clarification";
  reviewDisableSubmit();reviewRenderEdits();reviewSetBusy(false);
}
function reviewRenderEdits(){
  const host=rv("reviewEditList");host.replaceChildren();
  if(!reviewState.evidence)return;
  const claims=reviewSelectedClaims();
  const selected=reviewState.evidence.claims.filter(c=>claims.includes(c.claim_uid));
  if(!selected.length){rhelp(host,"Select exact claims to create scoped edits.");return;}
  const kind=rv("reviewCorrectionKind").value;
  if(kind==="source_clarification"){rhelp(host,"The documented source field is proposed, without unrestricted manual edits.");return;}
  for(const c of selected){
    const item=node("div",undefined,"review-edit");item.dataset.claim=c.claim_uid;
    item.append(node("strong","Claim "+c.claim_uid));
    const fields=[
      ["subject_id","Subject entity",c.subject_id],
      ["predicate","Relationship",c.predicate],
      ["object_id","Proposed class / target",c.object_id]
    ];
    for(const [key,title,value] of fields){
      const label=node("label",title),inp=node("input");
      inp.value=value;inp.dataset.field=key;inp.maxLength=256;
      if(key==="predicate"){inp.readOnly=true;inp.title="Predicate remains linked to the selected original claim.";}
      if(key==="subject_id"&&kind!=="ownership")inp.readOnly=true;
      if(key==="object_id"&&kind==="reject_connection")inp.readOnly=true;
      inp.addEventListener("input",reviewDisableSubmit);
      label.append(inp);item.append(label);
    }
    host.append(item);
  }
}
function reviewAction(){
  const w=reviewState.workspace,d=reviewState.evidence, t=reviewState.target;
  if(!w||!d||!t||!reviewAvailable())throw Error("Enable local review on an original run and select exact evidence.");
  const ids=reviewSelectedClaims(),sources=reviewSelectedSources();
  if(!ids.length||!sources.length)throw Error("Select at least one claim and one related original source record.");
  if(ids.length>64)throw Error("Select no more than 64 explicit claims.");
  const reason=rv("reviewReason").value.trim();
  if(!reason)throw Error("Explain how the selected documentary records support the proposed correction.");
  const kind=rv("reviewCorrectionKind").value;
  let edits=[];
  if(kind!=="source_clarification"){
    edits=[...rv("reviewEditList").querySelectorAll(".review-edit")].map(row=>{
      const data={};for(const inp of row.querySelectorAll("input[data-field]"))data[inp.dataset.field]=inp.value.trim();
      return data;
    });
    if(edits.length!==ids.length)throw Error("Every selected claim needs an explicit edit.");
  }
  const correction={kind,clarification:reason,edits};
  if(kind==="naming_alias")correction.alias=rv("reviewAlias").value.trim();
  if(kind==="source_clarification")correction.field=rv("reviewField").value;
  const action={
    schema_version:"review-v2", workspace_id:w.workspace_id,site_scope:w.site_scope,
    parent_fingerprint:w.parent_fingerprint,actor_id:w.reviewer.actor_id,
    action_id:crypto.randomUUID(),correlation_id:crypto.randomUUID(),
    expected_revision:reviewState.revision,target_kind:t.kind,target_id:t.id,
    action:"correct",requested_status:"needs_review",reason,evidence_uids:sources,
    affected_claim_uids:ids,correction
  };
  const supersedes=rv("reviewSupersedes").value.trim();
  if(supersedes)action.supersedes_event_id=supersedes;
  return action;
}
async function reviewPreviewAction(event){
  event.preventDefault();if(reviewState.busy)return;
  const action=reviewAction();reviewSetBusy(true);
  rtext("reviewPreviewState","Validating exact documentary scope and calculating preview…");
  try{
    const result=await api("/api/preview",{run:reviewState.run,action});
    reviewState.preview=result;reviewState.previewAction=action;
    reviewRenderPreview(result);
    rv("reviewConfirm").disabled=false;rv("reviewConfirm").checked=false;
    rtext("reviewPreviewState","Preview only. No decision recorded, no ontology changed. Inspect all outcomes before confirming.");
  }catch(e){reviewDisableSubmit();reviewOnError(e);throw e;}
  finally{reviewSetBusy(false);}
}
function reviewRenderPreview(report){
  const host=rv("reviewPreviewDetails");host.replaceChildren();
  const outcomes=Array.isArray(report.outcomes)?report.outcomes:[];
  const changes=report.changes||{};
  const heading=node("div",undefined,"review-preview-metrics");
  for(const [label,items] of [["Added triples",changes.added_triples],["Removed triples",changes.removed_triples],["Removed claims",changes.removed_claims],["Outcomes",outcomes]]){
    heading.append(rlabel(label,Array.isArray(items)?items.length:"Not evaluated"));
  }host.append(heading);
  if(!outcomes.length)rhelp(host,"No replay outcome records returned. Inspect the full preview before deciding.");
  for(const x of outcomes.slice(0,30)){
    const item=node("div",undefined,"review-evidence-card");
    item.append(node("strong",x.outcome||"Unspecified replay outcome"),node("p",x.reason||"See full review export for reason details.","small"));
    host.append(item);
  }
  if(changes.claim_changes?.length){
    const detail=node("details"),summary=node("summary",changes.claim_changes.length+" exact claim changes (first 20 shown)");
    detail.append(summary);
    for(const c of changes.claim_changes.slice(0,20)){
      const before=c.before?.claim, after=c.after?.claim;
      const entry=node("div",undefined,"review-diff");
      entry.append(node("p","Before: "+(before?before.subject_id+" · "+before.predicate+" · "+before.object_id+" ["+before.status+"]":"not present"),"small"));
      entry.append(node("p","After: "+(after?after.subject_id+" · "+after.predicate+" · "+after.object_id+" ["+after.status+"]":"not present"),"small"));
      detail.append(entry);
    }host.append(detail);
  }
  host.append(node("p","Only explicit, documentary-supported changes can become supported inferences. A pending or withheld outcome is not a successful edit.","small"));
}
async function reviewRecordAction(){
  if(reviewState.busy||!reviewState.previewAction||!rv("reviewConfirm").checked)throw Error("Preview and explicitly confirm the current correction first.");
  const action=reviewState.previewAction;
  if(action.expected_revision!==reviewState.revision||reviewState.run!==state.run)throw Error("Review revision or run changed. Prepare a fresh preview.");
  reviewSetBusy(true);
  try{
    const event=await api("/api/decide",{run:reviewState.run,action});
    reviewState.revision=event.revision;
    rtext("reviewRevision","Revision "+event.revision+" · decision pending");
    reviewDisableSubmit();
    rtext("reviewPreviewState","Pending decision recorded at revision "+event.revision+". It has not been applied to the parent.");
    await reviewHistory();
    say("Audited pending correction saved. Original run preserved. Publish a reviewed version to apply eligible consequences.");
  }finally{reviewSetBusy(false);}
}
async function reviewHistory(version=reviewState.requestVersion){
  if(!reviewState.workspace)return;
  const h=await api(url("/api/history",{run:reviewState.run,offset:reviewState.historyOffset,schema:"review-v2"}));
  if(version!==reviewState.requestVersion)return;
  reviewState.revision=h.revision;reviewState.historyTotal=h.total;
  rtext("reviewRevision","Revision "+h.revision+" · pending decisions");
  const host=rv("reviewHistoryList");host.replaceChildren();
  if(!h.events.length)rhelp(host,"No decisions recorded yet.");
  for(const event of h.events){
    const item=node("div",undefined,"review-history");
    item.append(node("strong","Revision "+event.revision+" · "+event.outcome));
    item.append(node("p","Event ID "+event.event_id,"small"));
    item.append(node("p",(event.request?.target_kind||"scope")+" / "+(event.request?.target_id||"unknown")+" · "+(event.request?.correction?.kind||"correction"),"small"));
    if(event.request?.reason)item.append(node("p",event.request.reason,"muted"));
    const b=node("button","Use this event ID for explicit supersession","ghost");b.type="button";
    b.onclick=()=>{rv("reviewSupersedes").value=event.event_id;reviewDisableSubmit();say("Supersession event ID copied into the correction form. You must review exact claim scope before previewing.");};
    item.append(b);host.append(item);
  }
  rv("reviewHistoryPrevious").disabled=reviewState.historyOffset===0||reviewState.busy;
  rv("reviewHistoryNext").disabled=reviewState.historyOffset+50>=h.total||reviewState.busy;
  rtext("reviewHistoryPage","Page "+(Math.floor(reviewState.historyOffset/50)+1));
  reviewSetBusy(false);
}
async function reviewPublish(){
  if(reviewState.busy||!reviewState.workspace||reviewState.revision<1)throw Error("Record a pending documentary correction before publishing a reviewed package.");
  if(!reviewAvailable())throw Error("Reviewed packages cannot be used as the new mutable parent.");
  const run=reviewState.run,revision=reviewState.revision;
  reviewSetBusy(true);
  rtext("reviewPublishStatus","Submitting deterministic replay and validation…");
  try{
    const response=await api("/api/export",{run,revision});
    if(!response.job_id)throw Error("Backend did not return a job identifier.");
    for(let attempt=0;attempt<1800;attempt++){
      const job=await api(url("/api/job",{id:response.job_id}));
      rtext("reviewPublishStatus",job.state.toUpperCase()+" · "+(job.progress||""));
      if(job.state==="failed")throw Error("Reviewed export failed; the original run remains unchanged. "+(job.error||"Inspect server logs."));
      if(job.state==="completed"){
        if(!job.result?.run)throw Error("Export finished without a package identifier.");
        const publishedRun=job.result.run;
        rtext("reviewPublishStatus","Verified reviewed package: "+publishedRun+". Original run remains immutable.");
        const host=rv("reviewPublishedLink");host.replaceChildren();
        const b=node("button","Open published reviewed ontology","primary");b.type="button";
        b.onclick=()=>safe(async()=>{
          await runs();state.run=publishedRun;rv("runs").value=publishedRun;
          state.runMeta={synthetic:state.runMeta?.synthetic===true};
          state.focus="";state.offset=0;state.selected=null;route("explorer");await load();
        });
        host.append(b);return;
      }
      await new Promise(ok=>setTimeout(ok,600));
    }
    throw Error("Publication is still processing; inspect job status before any new attempt.");
  }finally{reviewSetBusy(false);}
}
rv("reviewEnroll").onclick=()=>safe(async()=>{
  if(!reviewAvailable())throw Error("Select an original completed run.");
  reviewSetBusy(true);
  try{
    const workspace=await api("/api/enroll",{run:reviewState.run,schema:"review-v2"});
    reviewState.workspace=workspace;
    const history=await api(url("/api/history",{run:reviewState.run,schema:"review-v2"}));
    reviewState.revision=history.revision;reviewState.historyOffset=0;
    rtext("reviewRevision","Revision "+reviewState.revision+" · pending decisions");
    rtext("reviewWorkspaceStatus","Enrolled local principal: "+workspace.reviewer.actor_id+". Professional verification authority NOT established.");
    if(reviewState.target)await reviewInspect(reviewState.target.kind,reviewState.target.id);
    await reviewHistory();say("Local review enabled. Select exact claims and documentary evidence to continue.");
  }finally{reviewSetBusy(false);}
});
rv("reviewRefresh").onclick=()=>safe(reviewRoute);
rv("reviewClear").onclick=()=>{reviewResetSelection();reviewDisableSubmit();};
rv("reviewPrevious").onclick=()=>safe(async()=>{reviewState.queueOffset=Math.max(0,reviewState.queueOffset-50);await reviewLoadGroups();});
rv("reviewNext").onclick=()=>safe(async()=>{reviewState.queueOffset+=50;await reviewLoadGroups();});
rv("reviewCorrectionKind").onchange=reviewKindChange;
for(const id of ["reviewAlias","reviewField","reviewReason","reviewSupersedes"]){
  rv(id).addEventListener("input",reviewDisableSubmit);
  rv(id).addEventListener("change",reviewDisableSubmit);
}
rv("reviewActionForm").onsubmit=e=>safe(()=>reviewPreviewAction(e));
rv("reviewConfirm").onchange=()=>reviewSetBusy(reviewState.busy);
rv("reviewRecord").onclick=()=>safe(reviewRecordAction);
rv("reviewHistoryRefresh").onclick=()=>safe(()=>reviewHistory());
rv("reviewHistoryPrevious").onclick=()=>safe(async()=>{reviewState.historyOffset=Math.max(0,reviewState.historyOffset-50);await reviewHistory();});
rv("reviewHistoryNext").onclick=()=>safe(async()=>{reviewState.historyOffset+=50;await reviewHistory();});
rv("reviewPublish").onclick=()=>safe(reviewPublish);
reviewKindChange();
reviewRunChanged();
