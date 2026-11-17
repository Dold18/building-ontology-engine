"use strict";
/* UX-H: inspect only exact claim/observation/source-record links already present
   in the verified run package. No PDF/file publication, graph inference, or
   source value HTML execution occurs here. */
const evidenceUI = {
  relations: [],
  truncated: 0,
  typeFilter: "",
  statusFilter: "",
};
const ev=id=>document.getElementById(id);
const evText=(tag,value,cls)=>{
  const element=document.createElement(tag);
  if(value!==undefined && value!==null)element.textContent=String(value);
  if(cls)element.className=cls;
  return element;
};
function evidenceEmptyRelationships(){
  evidenceUI.relations=[];
  evidenceUI.truncated=0;
  ev("relationshipRegisterCount").textContent="No relationships loaded";
  ev("relationshipRows").replaceChildren();
  const tr=document.createElement("tr"),td=evText("td","Select a completed run to inspect source-backed links.");
  td.colSpan=5;tr.append(td);ev("relationshipRows").append(tr);
  ev("relationshipRegisterNote").textContent="Only displayed, source-declared edges appear here. A missing relationship is not evidence that no physical connection exists.";
}
function evidenceSetRelationships(graph){
  evidenceUI.relations=Array.isArray(graph?.edges)?graph.edges:[];
  evidenceUI.truncated=Number(graph?.truncated_edges||0);
  const filter=ev("relationshipFilter"),previous=filter.value;
  filter.replaceChildren();
  const all=evText("option","All types");all.value="";filter.append(all);
  for(const name of [...new Set(evidenceUI.relations.map(e=>e.predicate))].sort()){
    const option=evText("option",name);option.value=name;filter.append(option);
  }
  filter.value=[...filter.options].some(o=>o.value===previous)?previous:"";
  evidenceUI.typeFilter=filter.value;
  evidenceRenderRelationships();
  evidenceHighlightSelection(state.selected);
}
function evidenceRenderRelationships(){
  const host=ev("relationshipRows");host.replaceChildren();
  const edges=evidenceUI.relations.filter(e=>
    (!evidenceUI.typeFilter||e.predicate===evidenceUI.typeFilter) &&
    (!evidenceUI.statusFilter||e.status===evidenceUI.statusFilter));
  ev("relationshipRegisterCount").textContent=edges.length+" visible claims";
  if(!edges.length){
    const tr=document.createElement("tr"),td=evText("td","No claims match the current page and register filters.");
    td.colSpan=5;tr.append(td);host.append(tr);
  }
  for(const edge of edges){
    const tr=document.createElement("tr");
    tr.dataset.claim=edge.id;
    for(const value of [edge.source,edge.predicate,edge.target])tr.append(evText("td",value));
    const state=document.createElement("td");
    state.append(evText("span",statuses[edge.status]||edge.status,"status-chip "+edge.status));
    tr.append(state);
    const action=document.createElement("td"),button=evText("button","Inspect source trail","secondary");
    button.type="button";button.className="evidence-inspect-action";
    button.setAttribute("aria-label","Inspect original evidence for "+edge.source+" "+edge.predicate+" "+edge.target);
    button.onclick=()=>safe(async()=>{
      await inspect("claim",edge.id);
      ev("inspector").scrollIntoView({behavior:"auto",block:"nearest"});
    });
    action.append(button);tr.append(action);host.append(tr);
  }
  evidenceHighlightSelection(state.selected);
  const note=ev("relationshipRegisterNote");
  const total=evidenceUI.relations.length+evidenceUI.truncated;
  note.textContent="Showing relationships between entities on the current graph page; "+
    evidenceUI.relations.length+" of "+total+" edges in this projection. "+
    (evidenceUI.truncated?
      evidenceUI.truncated+" edges were omitted by the established safety limit. ":
      "")+
    "Unshown edges, unselected pages and inferred proximity do not establish physical connectivity.";
}
function evidenceHighlightSelection(claimUid){
  const selected=typeof claimUid==="string"?claimUid:"";
  for(const element of document.querySelectorAll("#relationshipRows tr[data-claim],#graph [data-claim]")){
    const matches=Boolean(selected)&&element.dataset.claim===selected;
    element.classList.toggle("selected",matches);
    element.classList.toggle("evidence-selected",matches);
  }
}
ev("relationshipFilter").onchange=event=>{
  evidenceUI.typeFilter=event.target.value;evidenceRenderRelationships();
};
ev("relationshipStatus").onchange=event=>{
  evidenceUI.statusFilter=event.target.value;evidenceRenderRelationships();
};
function evidenceSourceBox(record,{context=false}={}){
  const box=evText("section",undefined,"evidence-source");
  const head=evText("div",undefined,"evidence-source-head");
  head.append(evText("strong",record.source_file||"Unknown source filename"));
  const parse=record.parse_status||"unavailable";
  head.append(evText("span","Parse: "+parse,"evidence-source-state "+(parse==="error"?"evidence-source-error":"")));
  box.append(head);
  const location=[record.sheet_name?"Worksheet "+record.sheet_name:"",record.source_location||""].filter(Boolean).join(" · ");
  box.append(evText("p",location||"Source location not provided","small"));
  if(context)box.append(evText("p","Context record only: this record is not directly linked by an observation for the displayed claim.","evidence-caveat"));
  if(record.source_record_uid)box.append(evText("p","Record UID: "+record.source_record_uid,"evidence-id"));
  if(record.source_id)box.append(evText("p","Source ID: "+record.source_id,"evidence-id"));
  if(record.source_hash)box.append(evText("p","Original file SHA-256: "+record.source_hash,"evidence-id"));
  if(typeof sourceViewerCanOpen==="function"&&sourceViewerCanOpen(record)){
    const button=evText("button","Match & inspect original file","secondary");
    button.type="button";button.className="evidence-file-button";
    button.dataset.sourceFile=record.source_file;
    button.onclick=()=>safe(()=>sourceViewerOpen(record));
    box.append(button);
  }
  if(record.errors?.length){
    const warn=evText("ul",undefined,"evidence-parse-warnings");
    for(const issue of record.errors.slice(0,8))warn.append(evText("li",issue));
    box.append(warn);
  }
  const details=document.createElement("details");
  details.className="evidence-original";
  details.append(evText("summary","Inspect original record / cell reference"));
  const parts=[["Raw value",record.raw_value],
    ["Original cells",record.raw_record],
    ["Cell coordinates",record.cell_locations],
    ["Normalized fields (derived)",record.normalized_fields]];
  for(const [label,value] of parts){
    if(value===undefined||value===null)continue;
    details.append(evText("h5",label));
    const printable=typeof value==="string"?value:JSON.stringify(value,null,2);
    details.append(evText("pre",printable.length>4000?printable.slice(0,4000)+"\n[excerpt truncated; use exported source register for complete record]":printable));
  }
  box.append(details);
  const lower=(record.source_file||"").toLowerCase();
  if(lower.endsWith(".pdf")||record.source_type==="pdf"){
    box.append(evText("p","Drawing reference only. If you possess the exact original PDF, the local hash-matched viewer can display it. No verified drawing crop, coordinates or topology overlay are available here.","evidence-caveat"));
  }
  return box;
}
function evidenceTrace(result,host){
  const claims=Array.isArray(result.claims)?result.claims:[];
  const observations=Array.isArray(result.observations)?result.observations:[];
  const sources=Array.isArray(result.sources)?result.sources:[];
  const bySource=new Map(sources.map(s=>[s.source_record_uid,s]));
  const observedSourceIds=new Set();
  const section=evText("section",undefined,"evidence-trail");
  section.append(evText("h3","Claim → observation → source"));
  section.append(evText("p","Only explicit observation-to-record provenance links are shown. A source association does not by itself validate the claim or establish a physical connection.","small"));
  if(!claims.length)section.append(evText("p","No claims available for this selection.","muted"));
  for(const claim of claims.slice(0,30)){
    const trail=evText("article",undefined,"evidence-chain");
    const top=evText("div",undefined,"evidence-chain-head");
    top.append(evText("strong",claim.subject_id+" · "+claim.predicate+" · "+claim.object_id));
    top.append(evText("span",statuses[claim.status]||claim.status,"status-chip "+claim.status));
    trail.append(top);
    trail.append(evText("div","Claim UID: "+claim.claim_uid,"evidence-id"));
    const linked=observations.filter(o=>o.claim_uid===claim.claim_uid);
    trail.append(evText("p",linked.length+" explicit observation(s) associated with this claim.","small"));
    if(!linked.length)trail.append(evText("p","No observation in the returned inspection directly resolves a source record for this claim.","evidence-caveat"));
    for(const observation of linked.slice(0,30)){
      const ob=evText("section",undefined,"evidence-observation");
      ob.append(evText("strong","Observation: "+observation.observation_uid,"evidence-id"));
      const uids=[...new Set(observation.resolved_source_uids||[])];
      if(!uids.length)ob.append(evText("p","No resolved source record referenced by this observation.","evidence-caveat"));
      for(const uid of uids.slice(0,30)){
        observedSourceIds.add(uid);
        if(bySource.has(uid))ob.append(evidenceSourceBox(bySource.get(uid)));
        else ob.append(evText("p","Resolved record UID "+uid+" is not present in the returned inspection. Source excerpt unavailable.","evidence-caveat"));
      }
      if(uids.length>30)ob.append(evText("p","Additional record IDs omitted from this bounded view; inspect the complete source register.","small"));
      trail.append(ob);
    }
    if(linked.length>30)trail.append(evText("p","Additional observations omitted from this view; inspect full export.","small"));
    section.append(trail);
  }
  if(claims.length>30)section.append(evText("p","Only the first 30 claims were rendered; review the full evidence package.","small"));
  const other=sources.filter(s=>!observedSourceIds.has(s.source_record_uid));
  if(other.length){
    const details=document.createElement("details");
    details.append(evText("summary","Other associated or conflicting source records ("+other.length+")"));
    details.append(evText("p","These sources may contextualize conflicts or review questions. They must not be treated as direct supporting observations unless independently linked.","evidence-caveat"));
    for(const source of other.slice(0,20))details.append(evidenceSourceBox(source,{context:true}));
    if(other.length>20)details.append(evText("p","Additional records omitted; inspect complete source register.","small"));
    section.append(details);
  }
  const footer=evText("div",undefined,"evidence-trail-actions");
  const button=evText("button","Download private source records","secondary");
  button.type="button";
  button.onclick=()=>{
    const link=document.createElement("a");
    link.href=url("/api/download",{run:state.run,artifact:"source_records.jsonl"});
    link.download="source_records.jsonl";
    document.body.append(link);link.click();link.remove();
  };
  footer.append(button);
  footer.append(evText("p","The downloaded JSONL may contain private building identifiers and source data. Share only with authorized recipients.","evidence-caveat"));
  section.append(footer);
  host.append(section);
}
