"use strict";
// UX-B: read-only presentation. No mutation endpoint or raw HTML insertion is used.
const $ = id => document.getElementById(id);
const svgNS = "http://www.w3.org/2000/svg";
const state = {csrf:"",run:"",mode:"systems",status:"",search:"",focus:"",offset:0,graph:null,runMeta:null,selected:null,view:"explorer",camera:[0,0,1000,620]};
const statuses = {verified:"Engineer / documentary verified",supported_inference:"Supported inference",needs_review:"Needs review",contradicted:"Contradicted",rejected:"Rejected"};
function node(tag,content,cls) {const n=document.createElement(tag);if(content!==undefined)n.textContent=String(content);if(cls)n.className=cls;return n;}
function svg(tag,attrs,text){const e=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs||{}))e.setAttribute(k,String(v));if(text!==undefined)e.textContent=text;return e;}
function clear(el){el.replaceChildren();}
function say(message,error=false){const n=$("notice");n.textContent=message;n.classList.toggle("error",error);}
async function api(path,body){
  const response=await fetch(path,{method:body===undefined?"GET":"POST",credentials:"same-origin",headers:body===undefined?{}:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:body===undefined?undefined:JSON.stringify(body)});
  const result=await response.json();
  if(!response.ok){if(response.status===403){lock(false);}throw Error(result.error||"Request failed");}
  return result;
}
const url=(path,params)=>path+"?"+new URLSearchParams(params);
async function safe(fn){try{await fn();}catch(err){say(err.message||"Unable to complete request",true);}}
function lock(message=true){if(typeof sourceViewerClose==="function")sourceViewerClose();state.csrf="";$("shell").hidden=true;$("lock").hidden=false;$("password").value="";if(message)say("Local session locked");}
async function unlocked(){ $("lock").hidden=true;$("shell").hidden=false;await runs();await credentialStatus();say("Select a completed run. Source evidence stays on this machine.");}
$("loginForm").addEventListener("submit",ev=>{ev.preventDefault();safe(async()=>{const password=$("password").value;const result=await api("/api/login",{password});state.csrf=result.csrf;await unlocked();});});
$("lockButton").onclick=()=>safe(async()=>{await api("/api/logout",{});lock();});
async function restore(){try{const r=await api("/api/session");state.csrf=r.csrf;await unlocked();}catch{lock(false);}}
async function runs(){
  const list=await api("/api/runs");clear($("runs"));
  const placeholder=node("option","Select a completed run");placeholder.value="";$("runs").append(placeholder);
  for(const r of list){if(r.state!=="COMPLETED")continue;const opt=node("option",r.site ? r.site+(r.synthetic?" · SYNTHETIC":" · BUILDING DATA") : r.id);opt.value=r.id;opt.dataset.synthetic=String(Boolean(r.synthetic));$("runs").append(opt);}
  $("runs").value=state.run;
  if(state.run&&!list.some(r=>r.id===state.run)){state.run="";state.graph=null;}
  const selected=list.find(r=>r.id===state.run);state.runMeta=selected||null;
}
$("refresh").onclick=()=>safe(()=>runs());
$("runs").onchange=()=>safe(async()=>{if(typeof sourceViewerClose==="function")sourceViewerClose();state.run=$("runs").value;state.focus="";state.offset=0;state.selected=null;const opt=$("runs").selectedOptions[0];state.runMeta={synthetic:opt?.dataset.synthetic==="true"};await load();if(typeof workspaceRunChanged==="function")workspaceRunChanged();if(typeof reviewRunChanged==="function")reviewRunChanged();});
function route(view){
  state.view=view;
  for(const b of document.querySelectorAll(".nav")){const active=b.dataset.view===view;b.classList.toggle("active",active);b.setAttribute("aria-current",active?"page":"false");}
  for(const p of ["overview","explorer","inventory","review","quality","insights","exports","tour","setup","generate"])$(p+"Page").hidden=view!==p;
  if(view==="inventory")safe(inventory);
  if(view==="exports")exportsView();
  if(view==="setup")safe(credentialStatus);
  if(typeof workspaceRoute==="function")safe(()=>workspaceRoute(view));
  if(view==="review" && typeof reviewRoute==="function")safe(reviewRoute);
}
for(const n of document.querySelectorAll(".nav"))n.onclick=()=>route(n.dataset.view);
function modeSwitch(mode){
  state.mode=mode;state.offset=0;state.focus="";state.selected=null;
  for(const b of document.querySelectorAll(".tab")){const on=b.dataset.mode===mode;b.classList.toggle("active",on);b.setAttribute("aria-selected",String(on));}
  $("clearFocus").hidden=true;safe(load);
}
for(const b of document.querySelectorAll(".tab"))b.onclick=()=>modeSwitch(b.dataset.mode);
$("applyFilter").onclick=()=>safe(async()=>{state.search=$("search").value.trim();state.status=$("status").value;state.offset=0;await load();});
$("search").onkeydown=e=>{if(e.key==="Enter")$("applyFilter").click();};
$("clearFocus").onclick=()=>safe(async()=>{state.focus="";state.offset=0;$("clearFocus").hidden=true;await load();});
$("next").onclick=()=>safe(async()=>{if(!state.graph||state.graph.next_offset===null)return;state.offset=state.graph.next_offset;await load();});
$("previous").onclick=()=>safe(async()=>{state.offset=Math.max(0,state.offset-64);await load();});
async function load(){
  if(!state.run){state.graph=null;$("runCrumb").textContent="Select a completed run";$("origin").textContent="No run selected";drawEmpty();if(typeof evidenceEmptyRelationships==="function")evidenceEmptyRelationships();return;}
  const graph=await api(url("/api/explorer",{run:state.run,mode:state.mode,query:state.search,status:state.status,focus:state.focus,offset:state.offset,limit:64}));
  state.graph=graph;
  const selected=[...$("runs").options].find(o=>o.value===state.run);
  $("runCrumb").textContent=selected?selected.textContent:state.run;
  $("origin").textContent=state.runMeta&&state.runMeta.synthetic?"SYNTHETIC / TEST DATA":"PRIVATE BUILDING DATA";
  const values=[[graph.stats.systems,"Systems"],[graph.stats.equipment,"Equipment"],[graph.stats.points,"Points"],[graph.stats.claims,"Claims"]];
  clear($("metrics"));
  for(const [value,label] of values){const item=node("div",undefined,"metric");item.append(node("strong",Number(value).toLocaleString()),node("span",label));$("metrics").append(item);}
  $("clearFocus").hidden=!state.focus;
  $("graphMeta").textContent=(state.focus?state.focus+" · ":"")+state.mode[0].toUpperCase()+state.mode.slice(1)+" view";
  $("count").textContent=graph.total ? ((graph.offset+1)+"–"+Math.min(graph.offset+64,graph.total)+" of "+graph.total+" entities · "+graph.edges.length+" visible evidence links"):"No matching entities";
  $("previous").disabled=graph.offset===0;$("next").disabled=graph.next_offset===null;
  draw(graph); if(typeof evidenceSetRelationships==="function")evidenceSetRelationships(graph); if(!state.selected)resetInspector();say("Loaded "+graph.nodes.length+" graph entities from verified run package.");
}
function drawEmpty(){clear($("graph"));$("graph").append(svg("text",{x:60,y:80,fill:"#65808d"},"Select a completed run to explore the graph."));}
function setCamera(){const [x,y,w,h]=state.camera;$("graph").setAttribute("viewBox",[x,y,w,h].join(" "));}
function colorFor(kind){return kind==="system"?"#d9eae9":kind==="point"?"#ecf4f4":"#e8eef4";}
function layout(g){
  // Layout is a visual grouping of *existing* entities, never a new physical
  // relationship or inferred direction. Only `feeds` edges use arrows.
  const rank=n=>n.is_system?0:n.kind==="equipment"?1:n.kind==="point"?2:3;
  const sorted=[...g.nodes].sort((a,b)=>rank(a)-rank(b)||a.id.localeCompare(b.id));
  const layers=new Map();
  for(const item of sorted){const k=rank(item);if(!layers.has(k))layers.set(k,[]);layers.get(k).push(item);}
  if(layers.size>1){
    const cols=[...layers.keys()].sort((a,b)=>a-b);
    const maxRows=Math.max(...[...layers.values()].map(x=>x.length));
    for(const [col,k] of cols.entries()){
      const entries=layers.get(k);
      entries.forEach((n,i)=>{n.x=150+col*260;n.y=110+(i+(maxRows-entries.length)/2)*120;});
    }
    return {nodes:sorted,width:Math.max(1000,cols.length*260+160),height:Math.max(620,maxRows*120+200)};
  }
  const count=sorted.length,cols=count<=6?Math.min(count,3):count<=20?4:Math.min(6,Math.ceil(Math.sqrt(count*1.25)));
  const cellW=235,cellH=115;
  sorted.forEach((n,i)=>{n.x=145+(i%cols)*cellW;n.y=110+Math.floor(i/cols)*cellH;});
  return {nodes:sorted,width:Math.max(1000,cols*cellW+100),height:Math.max(620,Math.ceil(count/Math.max(1,cols))*cellH+180)};
}
function draw(g){
  const root=$("graph");clear(root);$("empty").hidden=!!g.nodes.length;
  if(!g.nodes.length){state.camera=[0,0,1000,620];setCamera();return;}
  const l=layout(g),byId=new Map(l.nodes.map(n=>[n.id,n]));
  const defs=svg("defs");const marker=svg("marker",{id:"explorerArrow",viewBox:"0 0 10 10",refX:8,refY:5,markerWidth:5,markerHeight:5,orient:"auto"});
  marker.append(svg("path",{d:"M0 0 L10 5 L0 10z",fill:"#658b91"}));defs.append(marker);root.append(defs);
  for(const edge of g.edges){const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b||a===b)continue;
    const mid=(a.x+b.x)/2,top=Math.min(a.y,b.y)-17;
    const path=svg("path",{d:"M"+a.x+" "+a.y+" Q"+mid+" "+top+" "+b.x+" "+b.y,class:"edge "+edge.status,"data-claim":edge.id,tabindex:"0",role:"button","aria-label":edge.source+" "+edge.predicate+" "+edge.target+" · "+edge.status});
    if(edge.directed)path.setAttribute("marker-end","url(#explorerArrow)");
    path.append(svg("title",{},edge.predicate+" · "+edge.status+" · "+edge.id));
    const select=()=>safe(()=>inspect("claim",edge.id));
    path.onclick=select;path.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();select();}};
    root.append(path);
    if(g.edges.length<=18)root.append(svg("text",{x:mid,y:(a.y+b.y)/2-13,"text-anchor":"middle",class:"edge-label"},edge.predicate));
  }
  for(const n of l.nodes){const group=svg("g",{class:"node","data-claim":n.claim_uid||"",transform:"translate("+n.x+" "+n.y+")",role:"button",tabindex:"0","aria-label":n.label+" · "+n.class+" · "+n.status});
    const system=n.is_system,point=n.kind==="point";
    if(point)group.append(svg("circle",{r:33,fill:colorFor(n.kind),stroke:"#9dbcbc","stroke-width":"1.5"}));
    else group.append(svg("rect",{x:-91,y:-34,width:182,height:68,rx:14,fill:colorFor(system?"system":n.kind),stroke:"#c6d7df","stroke-width":"1.5"}));
    const label=n.label.length>20?n.label.slice(0,19)+"…":n.label;
    group.append(svg("text",{x:0,y:-3,"text-anchor":"middle",class:"node-label"},label));
    const className=n.class.length>23?n.class.slice(0,22)+"…":n.class;
    group.append(svg("text",{x:0,y:16,"text-anchor":"middle",class:"node-type"},className));
    group.append(svg("title",{},n.id+" · "+n.class+" · "+n.status));
    const select=()=>safe(()=>selectNode(n));group.onclick=select;group.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();select();}};
    root.append(group);
  }
  const hostWidth=Math.max(420,root.clientWidth),hostHeight=Math.max(350,root.clientHeight);
  const aspect=hostWidth/hostHeight,framedW=Math.max(l.width,l.height*aspect),framedH=framedW/aspect;
  state.camera=[-25,-20,framedW,framedH];setCamera();attachPan(root);
}
function attachPan(root){
  let start=null;
  root.onpointerdown=e=>{if(e.target!==root)return;start={x:e.clientX,y:e.clientY,c:[...state.camera]};root.setPointerCapture(e.pointerId);};
  root.onpointermove=e=>{if(!start)return;state.camera[0]=start.c[0]-(e.clientX-start.x)*state.camera[2]/Math.max(root.clientWidth,1);state.camera[1]=start.c[1]-(e.clientY-start.y)*state.camera[3]/Math.max(root.clientHeight,1);setCamera();};
  root.onpointerup=()=>{start=null;};root.onpointercancel=()=>{start=null;};
}
function zoom(factor){const c=state.camera;c[0]+=(c[2]*(1-factor))/2;c[1]+=(c[3]*(1-factor))/2;c[2]*=factor;c[3]*=factor;setCamera();}
$("zoomIn").onclick=()=>zoom(.8);$("zoomOut").onclick=()=>zoom(1.25);$("reset").onclick=()=>state.graph&&draw(state.graph);
$("graph").addEventListener("wheel",e=>{if(!state.graph)return;e.preventDefault();zoom(e.deltaY>0?1.1:.9);},{passive:false});
async function selectNode(n){
  state.selected=n.id;
  if(n.claim_uid){await inspect("claim",n.claim_uid);return;}
  $("inspectTitle").textContent=n.label;$("inspectSubtitle").textContent=n.class+" · No type claim available.";
  clear($("inspectBody"));const b=node("button","Focus related entities","claim-button");b.onclick=()=>focus(n.id);$("inspectBody").append(b);
}
function focus(id){state.focus=id;state.offset=0;$("clearFocus").hidden=false;route("explorer");safe(load);}
function detail(label,value){const wrap=node("div",undefined,"detail-row");wrap.append(node("small",label),node("strong",value||"Not provided"));return wrap;}
function resetInspector(){$("inspectTitle").textContent="Select an entity";$("inspectSubtitle").textContent="Click a node or connection to inspect exact claims and evidence.";clear($("inspectBody"));const div=node("div",undefined,"inspect-placeholder");div.append(node("div","⌕","big-icon"),node("p","Evidence, source rows and interpretations appear here."));$("inspectBody").append(div);}
async function inspect(kind,id){
  if(!state.run)return;
  const result=await api(url("/api/inspect",{run:state.run,kind,id}));
  state.selected=id;clear($("inspectBody"));if(typeof evidenceHighlightSelection==="function")evidenceHighlightSelection(id);const claims=result.claims||[];
  $("inspectTitle").textContent=claims[0]?(claims[0].subject_id||"Evidence claim").split("/").pop():"Evidence details";
  $("inspectSubtitle").textContent="Exact claim and immutable source records";
  if(!claims.length)$("inspectBody").append(node("p","No matching claim details were returned.","muted"));
  for(const c of claims.slice(0,6)){
    const block=node("div",undefined,"source-card");block.append(node("div",c.predicate+" → "+c.object_id));
    block.append(node("span",statuses[c.status]||c.status,"status-chip "+c.status));
    if(c.claim_uid)block.append(detail("Claim identifier",c.claim_uid));
    $("inspectBody").append(block);
    if(c.subject_id){const b=node("button","Focus "+c.subject_id,"claim-button");b.onclick=()=>focus(c.subject_id);$("inspectBody").append(b);}
  }
  if(typeof evidenceTrace==="function")evidenceTrace(result,$("inspectBody"));
  if((result.alternatives||[]).length)$("inspectBody").append(detail("Alternative hypotheses",result.alternatives.length+" recorded"));
  if((result.conflicts||[]).length)$("inspectBody").append(detail("Contradictions",result.conflicts.length+" recorded"));
  if(claims[0]?.claim_uid){
    const button=node("button","Review this exact claim","claim-button");
    button.type="button";
    button.onclick=()=>{route("review");if(typeof reviewInspect==="function")safe(()=>reviewInspect("claim",claims[0].claim_uid));};
    $("inspectBody").append(button);
  }
  $("inspectBody").append(node("p","A local credential or a schema pass does not establish professional engineering verification.","small"));
}
async function inventory(){
  if(!state.run){$("inventoryList").textContent="Select a completed run.";return;}
  const query=$("inventorySearch").value.trim();const g=await api(url("/api/explorer",{run:state.run,mode:"equipment",query,limit:64}));
  clear($("inventoryList"));
  for(const n of g.nodes){const row=node("button",undefined,"asset-row"),left=node("div");
    left.append(node("strong",n.label),node("small",n.class+" · "+n.id));
    row.append(left,node("span",statuses[n.status]||n.status,"status-chip "+n.status));
    row.onclick=()=>{state.mode="equipment";state.focus=n.id;state.offset=0;state.selected=null;for(const t of document.querySelectorAll(".tab")){let on=t.dataset.mode==="equipment";t.classList.toggle("active",on);t.setAttribute("aria-selected",String(on));}route("explorer");safe(load);};
    $("inventoryList").append(row);
  }
  if(!g.nodes.length)$("inventoryList").append(node("p","No matching equipment.","muted"));
}
$("inventorySearch").addEventListener("keydown",e=>{if(e.key==="Enter")safe(inventory);});
function exportsView(){
  $("exportRun").textContent=state.run||"Select a completed run.";
  clear($("downloads"));if(!state.run)return;
  for(const [label,artifact] of [["Complete package","package.zip"],["Accepted ontology (Turtle)","ontology.ttl"],["Relationship register","relationships.csv"],["Review questions","review_questions.csv"],["Evidence records","source_records.jsonl"]]){
    const a=node("a",label);a.href=url("/api/download",{run:state.run,artifact});a.download=artifact;$("downloads").append(a);
  }
}

async function credentialStatus() {
  const data=await api("/api/credentials");
  const messages={
    session_memory:"Connected for this browser login. Key is not checked against the provider.",
    server_environment:"An API key is configured by the local server environment.",
    none:"No key connected. Offline synthetic demonstrations do not require one."
  };
  $("credentialStatus").textContent=messages[data.source]||"Connection status unavailable.";
  $("forgetKey").disabled=data.source!=="session_memory";
}
$("credentialForm").addEventListener("submit",e=>{
  e.preventDefault();
  safe(async()=>{
    const input=$("providerKey"),value=input.value;
    input.value="";
    if(!value.trim())throw Error("Enter an OpenAI API key.");
    const data=await api("/api/credentials",{provider:"openai",key:value});
    $("credentialStatus").textContent=data.configured
      ?"Saved for this local login. Not validated with the API provider.":"No key connected.";
    $("forgetKey").disabled=false;
    say("API key stored in this local browser session. No paid request was made.");
  });
});
$("forgetKey").onclick=()=>safe(async()=>{
  await api("/api/credentials/clear",{});
  await credentialStatus();
  say("Browser-provided key cleared. No paid request was made.");
});


/* Guided ingestion: server-side cost preflight, explicit approval, no inferred prices. */
let guidedInputId="",guidedPlan=null,guidedRunId="",guidedBusy=false;
function guidedStep(step){
  for(let n=1;n<=4;n++){
    const el=$("wizStep"+n);
    el.classList.toggle("wizard-current",n===step);
    el.classList.toggle("wizard-done",n<step);
  }
}
function guidedInvalidate(){
  guidedPlan=null;
  $("guidedApproval").hidden=true;
  $("guidedPlan").replaceChildren(node("p","Configuration changed. Generate a new processing plan before approving."));
  $("guidedAgreePrivacy").checked=false;$("guidedAgreeCost").checked=false;
}
function guidedMode(){
  const provider=$("guidedProvider").value;
  $("guidedOpenAIFields").hidden=provider!=="openai";
  $("guidedAnthropicNotice").hidden=provider!=="anthropic";
  const descriptions={
    offline:"Run a built-in synthetic fixture without uploading building data, an API key or any paid calls.",
    openai:"Use the generic OpenAI inference pipeline for uploaded CSV/XLSX inputs. You must approve the source data and pricing before an external request.",
    anthropic:"The sponsor-specific Claude pipeline has independent approval gates. Browser execution is disabled until those gates can be represented without bypassing them."
  };
  $("guidedProviderDescription").textContent=descriptions[provider];
  $("guidedPreflight").disabled=provider==="anthropic";
  guidedInvalidate();
}
$("guidedProvider").onchange=guidedMode;
for(const id of ["guidedFiles","guidedMapping","guidedBuildingId","guidedModel","guidedInputRate","guidedOutputRate","guidedCap"]){
  $(id).addEventListener("change",()=>{
    if(id==="guidedFiles"||id==="guidedMapping"){guidedInputId="";$("guidedUploadResult").textContent="Input changed. Upload locally before running inference.";}
    guidedInvalidate();
  });
}
$("guidedGoSetup").onclick=()=>route("setup");
$("setupToGenerate").onclick=()=>route("generate");
async function encodeFile(file){
  if(file.size>2_000_000)throw Error("Each file must be 2 MB or smaller.");
  const bytes=new Uint8Array(await file.arrayBuffer());
  let binary="";
  for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));
  return {name:file.name,data:btoa(binary)};
}
$("guidedFilesForm").addEventListener("submit",e=>{
  e.preventDefault();safe(async()=>{
    if(guidedBusy)throw Error("A workflow is running.");
    const input=[...$("guidedFiles").files];
    if(!input.length||input.length>10||input.reduce((sum,f)=>sum+f.size,0)>8_000_000){
      throw Error("Choose 1–10 files, with no more than 8 MB combined.");
    }
    let mapping;try{mapping=JSON.parse($("guidedMapping").value);}catch{throw Error("Data mapping must contain valid JSON.");}
    $("guidedUploadResult").textContent="Uploading source files to this local server…";
    const files=await Promise.all(input.map(encodeFile));
    const response=await api("/api/upload",{files,config:mapping});
    guidedInputId=response.input_id;guidedInvalidate();guidedStep(2);
    $("guidedUploadResult").textContent=response.files.length+" files accepted locally ("+Number(response.bytes).toLocaleString()+" bytes). No provider request was made.";
    say("Building inputs securely staged locally. Select a provider and review an execution plan.");
  });
});
function addPlanLine(parent,label,value){
  const wrap=node("div",undefined,"detail-row");wrap.append(node("small",label),node("strong",value));parent.append(wrap);
}
function showGuidedPlan(plan,offline=false){
  clear($("guidedPlan"));
  if(offline){
    addPlanLine($("guidedPlan"),"Provider","Offline synthetic fixture");
    addPlanLine($("guidedPlan"),"Network cost","$0 · No hosted inference");
    addPlanLine($("guidedPlan"),"Source","Built-in synthetic sample, not a real building");
    $("guidedAgreePrivacy").checked=false;$("guidedAgreeCost").checked=false;
    $("guidedApproval").hidden=false;
    for(const label of document.querySelectorAll("#guidedApproval .check-line"))label.hidden=true;
    $("guidedRun").textContent="Run synthetic demonstration";
    return;
  }
  const p=plan.settings;
  for(const label of document.querySelectorAll("#guidedApproval .check-line"))label.hidden=false;
  $("guidedRun").textContent="Approve & generate ontology";
  addPlanLine($("guidedPlan"),"Provider / model",p.provider+" / "+p.model);
  addPlanLine($("guidedPlan"),"Building",p.building_id);
  addPlanLine($("guidedPlan"),"Source inputs",p.files.map(f=>f.name+" ("+f.bytes.toLocaleString()+" bytes)").join(", "));
  addPlanLine($("guidedPlan"),"Maximum cost under entered rates","$"+p.conservative_upper_bound_usd);
  addPlanLine($("guidedPlan"),"Budget cap","$"+p.cost_cap_usd);
  addPlanLine($("guidedPlan"),"Operator-supplied rates","$"+p.pricing.input_usd_per_million+"/million input · $"+p.pricing.output_usd_per_million+"/million output");
  addPlanLine($("guidedPlan"),"Processing restrictions",plan.method);
  $("guidedPlan").append(node("p",plan.warning,"small"),node("p",plan.external_processing,"small"));
  $("guidedApproval").hidden=false;
}
$("guidedPreflight").onclick=()=>safe(async()=>{
  if(guidedBusy)throw Error("A workflow is already running.");
  const provider=$("guidedProvider").value;guidedInvalidate();
  if(provider==="anthropic")throw Error("Anthropic sponsor execution requires its separate owner-approval workflow.");
  if(provider==="offline"){guidedPlan={offline:true};showGuidedPlan(guidedPlan,true);guidedStep(3);return;}
  if(!guidedInputId)throw Error("Upload building files locally before previewing an OpenAI plan.");
  const body={input_id:guidedInputId,provider:"openai",model:$("guidedModel").value.trim(),
    building_id:$("guidedBuildingId").value.trim(),
    input_rate:$("guidedInputRate").value,output_rate:$("guidedOutputRate").value,
    max_cost_usd:$("guidedCap").value};
  guidedPlan=await api("/api/guided/preflight",body);
  showGuidedPlan(guidedPlan);
  guidedStep(3);say("Preflight complete. No external data was transmitted and no API cost was incurred.");
});
async function runGuidedJob(start){
  const response=await start();
  if(!response||!response.job_id)throw Error("Run was not accepted by the local server.");
  for(let attempts=0;attempts<3600;attempts++){
    const job=await api(url("/api/job",{id:response.job_id}));
    $("guidedRunStatus").textContent=job.state.toUpperCase()+" · "+job.progress;
    if(job.state==="failed")throw Error("Analysis failed: "+job.error+". No replacement ontology was published.");
    if(job.state==="completed"){
      if(!job.result?.run)throw Error("Completed job has no verifiable run identifier.");
      guidedRunId=job.result.run;state.run=guidedRunId;state.offset=0;state.focus="";state.selected=null;
      await runs();$("runs").value=guidedRunId;await load();
      guidedStep(4);$("guidedExplore").disabled=false;
      $("guidedRunStatus").textContent="Ontology generated and package-integrity verified. Open it in the Explorer.";
      say("A new ontology package is ready for evidence-based exploration.");
      return;
    }
    await new Promise(done=>setTimeout(done,600));
  }
  throw Error("Status polling timed out. Inspect the completed runs list; the job may still be running.");
}
$("guidedRun").onclick=()=>safe(async()=>{
  if(!guidedPlan||guidedBusy)throw Error("Create a fresh preflight before running.");
  if(!guidedPlan.offline&&(!$("guidedAgreePrivacy").checked||!$("guidedAgreeCost").checked))
    throw Error("You must explicitly approve external transmission and the configured cost cap.");
  guidedBusy=true;$("guidedRun").disabled=true;$("guidedPreflight").disabled=true;
  $("guidedRunStatus").textContent="Submitting analysis to the local engine…";
  try{
    if(guidedPlan.offline)await runGuidedJob(()=>api("/api/analyze",{demo:"shared"}));
    else{
      const hash=guidedPlan.plan_hash;
      guidedPlan=null;$("guidedApproval").hidden=true;
      await runGuidedJob(()=>api("/api/guided/execute",{plan_hash:hash,approve_external:true,approve_cost:true}));
    }
  }finally{
    guidedBusy=false;$("guidedRun").disabled=false;
    $("guidedPreflight").disabled=$("guidedProvider").value==="anthropic";
  }
});
$("guidedExplore").onclick=()=>{
  if(!guidedRunId)return;
  if(typeof sourceViewerClose==="function")sourceViewerClose();
  state.run=guidedRunId;
  route("explorer");
  safe(load);
};
guidedMode();

restore();
