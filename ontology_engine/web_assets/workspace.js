"use strict";
/* UX-E: verified-package workspace views. All data comes from existing authenticated
   read-only APIs. Review decisions remain in the classic audited workspace. */
const workspaceViews = ["overview","quality","insights"];
let workspaceGeneration = 0;
const commandDestinations = [
  ["Overview","overview"],["Ontology Explorer","explorer"],["Asset inventory","inventory"],
  ["Review & evidence","review"],["Data quality","quality"],["Engineering readiness","insights"],
  ["Generate ontology","generate"],["Runs & exports","exports"],["Guided walkthrough","tour"],
  ["Model setup","setup"]
];
function workspaceEl(id){return document.getElementById(id);}
function workspaceEmpty(el, message){el.replaceChildren();el.append(node("p",message,"muted"));}
function workspaceNote(parent, title, subtitle, action){
  const item=node("div",undefined,"work-item"),body=node("div");
  body.append(node("strong",title),node("p",subtitle,"small"));item.append(body);
  if(action){const b=node("button",action.text,"secondary");b.type="button";b.onclick=action.click;item.append(b);}
  parent.append(item);return item;
}
function workspaceSelectedRun(){return state.run && state.runMeta ? state.run : "";}
function workspaceEvidenceStatus(status){return statuses[status]||status||"Unspecified";}
function workspaceRunChanged(){
  workspaceGeneration++;
  if(workspaceViews.includes(state.view))safe(()=>workspaceRoute(state.view));
}
async function workspaceRoute(view){
  if(!workspaceViews.includes(view))return;
  const ticket=workspaceGeneration;
  if(view==="overview")await workspaceOverview(ticket);
  if(view==="quality")await workspaceQuality(ticket);
  if(view==="insights")await workspaceInsights(ticket);
  if(view==="review")await workspaceReview(ticket);
}
async function workspaceStats(){
  if(state.graph && state.run)return state.graph.stats;
  if(!state.run)return null;
  const g=await api(url("/api/explorer",{run:state.run,mode:"systems",limit:1}));
  return g.stats;
}
async function workspaceOverview(ticket){
  const metrics=workspaceEl("overviewMetrics"),statusesEl=workspaceEl("overviewStatuses"),actions=workspaceEl("overviewActions");
  workspaceEl("overviewOrigin").textContent=state.run ? (state.runMeta?.synthetic?"SYNTHETIC / TEST DATA":"PRIVATE BUILDING DATA") : "No building selected";
  if(!state.run){
    metrics.replaceChildren();
    for(const title of ["Systems","Equipment","Points","Claims"]){const k=node("div",undefined,"metric");k.append(node("strong","—"),node("span",title));metrics.append(k);}
    workspaceEmpty(statusesEl,"Select a completed run to see its actual claim-status distribution.");
    workspaceEmpty(actions,"No model selected. Start the synthetic demonstration, or select a completed building run.");
    return;
  }
  const data=await workspaceStats();
  if(ticket!==workspaceGeneration||!state.run)return;
  const values=[[data.systems,"Systems"],[data.equipment,"Equipment"],[data.points,"Points"],[data.claims,"Claims"]];
  metrics.replaceChildren();
  for(const [value,title] of values){const k=node("div",undefined,"metric");k.append(node("strong",Number(value).toLocaleString()),node("span",title));metrics.append(k);}
  statusesEl.replaceChildren();
  const statusCounts=data.statuses||{};
  for(const key of ["verified","supported_inference","needs_review","contradicted","rejected"]){
    const total=Number(statusCounts[key]||0),row=node("div",undefined,"status-summary");
    row.append(node("span",workspaceEvidenceStatus(key),"status-chip "+key),node("strong",total.toLocaleString()));statusesEl.append(row);
  }
  actions.replaceChildren();
  if(Number(statusCounts.needs_review||0)+Number(statusCounts.contradicted||0)>0)
    workspaceNote(actions,"Investigate unresolved evidence","Review the grouped questions and source-linked claims.",{text:"Open review",click:()=>route("review")});
  workspaceNote(actions,"Inspect physical relationships","Explore only relationships present in the assertion ledger.",{text:"Explore",click:()=>route("explorer")});
  workspaceNote(actions,"Check engineering findings","Separate data-quality issues and model conflicts from proven faults.",{text:"Data quality",click:()=>route("quality")});
  workspaceNote(actions,"Export reproducible results","Download the ontology, supporting records and run package.",{text:"Exports",click:()=>route("exports")});
}
function workspaceFindIssues(source){
  if(Array.isArray(source))return source;
  if(source && typeof source==="object"){
    for(const key of ["issues","records","items","findings"]){if(Array.isArray(source[key]))return source[key];}
  }
  return null;
}
function workspaceDisplay(value,limit=250){
  if(value==null)return "Not provided";
  if(typeof value==="string")return value.slice(0,limit);
  if(typeof value==="number"||typeof value==="boolean")return String(value);
  return JSON.stringify(value).slice(0,limit);
}
async function workspaceQuality(ticket){
  const host=workspaceEl("qualityList"),counter=workspaceEl("qualityCount");
  if(!state.run){counter.textContent="No run selected";workspaceEmpty(host,"Select a completed run to inspect its published findings.");return;}
  const result=await api(url("/api/view",{run:state.run}));
  if(ticket!==workspaceGeneration)return;
  const items=workspaceFindIssues(result.issues);
  host.replaceChildren();
  if(!items){
    counter.textContent="Schema not recognized";
    workspaceEmpty(host,"The run contains an issue report, but this view cannot safely interpret its structure. Download the complete package for inspection.");return;
  }
  counter.textContent=items.length.toLocaleString()+" published findings";
  if(!items.length){workspaceEmpty(host,"The report contains no findings. This does not establish that the building is fault-free.");return;}
  for(const issue of items.slice(0,60)){
    const title=workspaceDisplay(issue?.category||issue?.issue_type||issue?.rule_or_detection_method||"Reported finding",130);
    const asset=workspaceDisplay(issue?.point_or_asset_ids||issue?.asset_id||issue?.point_id||issue?.issue_id||"",140);
    const detail=workspaceDisplay(issue?.observed_evidence||issue?.description||issue?.message||issue?.reason||"See the complete report for details.",350);
    const box=node("article",undefined,"finding-card");
    box.append(node("strong",title),node("p",asset,"small"),node("p",detail,"muted"));
    const action=issue?.recommended_action||issue?.recommendation;
    if(action)box.append(node("p","Suggested verification: "+workspaceDisplay(action,350),"small"));
    host.append(box);
  }
  if(items.length>60)host.append(node("p","Showing 60 of "+items.length+" findings. Download the complete package for all records.","small"));
}
async function workspaceInsights(ticket){
  const host=workspaceEl("insightFacts");
  if(!state.run){workspaceEmpty(host,"Select a completed run. This page does not infer unavailable measurement readiness.");return;}
  const data=await workspaceStats();
  if(ticket!==workspaceGeneration)return;
  host.replaceChildren();
  const box=node("div",undefined,"facts-row");
  for(const [value,label] of [[data.systems,"Identified systems"],[data.equipment,"Equipment entities"],[data.points,"Point entities"]]){
    const t=node("div",undefined,"fact");t.append(node("strong",Number(value).toLocaleString()),node("span",label));box.append(t);
  }host.append(box);
  host.append(node("p","These counts describe the existing ontology projection. They do not prove that a particular calculation has sufficient measurements or independently validated relationships.","small"));
}
async function workspaceReview(ticket){
  const host=workspaceEl("reviewQueue");
  host.replaceChildren();host.append(node("h2","Consolidated engineering questions"));
  if(!state.run){host.append(node("p","Select a completed run to load its grouped review questions.","muted"));return;}
  const data=await api(url("/api/groups",{run:state.run,offset:0}));
  if(ticket!==workspaceGeneration)return;
  const items=Array.isArray(data.items)?data.items:[];
  host.append(node("p",data.total+" question groups · first "+items.length+" shown. Decisions remain in the audited classic review workspace.","small"));
  if(!items.length)host.append(node("p","No review groups were returned for this run.","muted"));
  for(const item of items.slice(0,20)){
    const id=item.group_id||item.review_id||item.cause_kind||"Unspecified group";
    const reason=item.question_or_test||item.question||item.reason_for_uncertainty||item.cause_kind||"Inspect source records to resolve this group.";
    const count=Array.isArray(item.claims)?item.claims.length:Array.isArray(item.affected_assertion_ids)?item.affected_assertion_ids.length:null;
    workspaceNote(host,workspaceDisplay(id,150),workspaceDisplay(reason,240)+(count===null?"":" · "+count+" linked claims"),
      {text:"Open review",click:()=>{location.assign("/legacy");}});
  }
  if(data.total>items.length)host.append(node("p","More groups are available in the classic review workspace.","small"));
}
workspaceEl("overviewStart").onclick=()=>route("generate");
workspaceEl("overviewTour").onclick=()=>route("tour");
workspaceEl("qualityRefresh").onclick=()=>safe(()=>workspaceQuality(workspaceGeneration));
for(const b of document.querySelectorAll(".insightExplore"))b.onclick=()=>{
  state.mode="points";state.search=b.dataset.search;state.offset=0;state.focus="";workspaceEl("search").value=state.search;
  for(const t of document.querySelectorAll(".tab")){const active=t.dataset.mode==="points";t.classList.toggle("active",active);t.setAttribute("aria-selected",String(active));}
  route("explorer");safe(load);
};
workspaceEl("tourGenerate").onclick=()=>{workspaceEl("guidedProvider").value="offline";guidedMode();route("generate");};
workspaceEl("tourExplore").onclick=()=>route("explorer");
workspaceEl("tourReview").onclick=()=>route("review");
/* Command navigation is deliberately client-local. Asset lookup uses the existing
   authenticated, bounded explorer query and does not prefetch private source data. */
const dialog=workspaceEl("commandPalette"),commandQuery=workspaceEl("commandQuery");
const commandOpen=()=>{dialog.showModal();commandQuery.value="";workspaceCommandResults();commandQuery.focus();};
workspaceEl("commandOpen").onclick=commandOpen;
document.addEventListener("keydown",event=>{
  if((event.metaKey||event.ctrlKey)&&event.key.toLowerCase()==="k"){event.preventDefault();if(dialog.open)dialog.close();else commandOpen();}
});
function workspaceCommandResults(){
  const host=workspaceEl("commandResults");host.replaceChildren();
  const q=commandQuery.value.trim().toLowerCase();
  for(const [label,view] of commandDestinations.filter(x=>x[0].toLowerCase().includes(q)).slice(0,10)){
    const b=node("button",label,"command-result");b.type="button";b.setAttribute("role","option");
    b.onclick=()=>{dialog.close();route(view);};host.append(b);
  }
  if(q){
    const b=node("button",'Find "'+commandQuery.value.trim().slice(0,120)+'" in selected building',"command-result");
    b.type="button";b.onclick=()=>{dialog.close();state.search=commandQuery.value.trim().slice(0,120);workspaceEl("search").value=state.search;state.offset=0;route("explorer");safe(load);};host.append(b);
  }
}
commandQuery.oninput=workspaceCommandResults;
commandQuery.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();workspaceEl("commandResults").querySelector("button")?.click();}};
/* UX-G: worksheet-aware, bounded local-only onboarding is implemented in onboarding.js. */
/* Local-only visual preference. No persistent storage or network request. */
workspaceEl("themeToggle").onclick=()=>{
  const isDark=document.documentElement.classList.toggle("workspace-dark");
  workspaceEl("themeToggle").textContent=isDark?"Light theme":"Dark theme";
};

// Start new sessions on the building overview; the selected-run dropdown remains authoritative.
route("overview");
