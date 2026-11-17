'use strict';
const $ = id => document.getElementById(id);
let csrf = '', selectedRun = '', reviewRun = '', workspace = null, revision = 0;
let selected = null, target = null, previewed = null, graphOffset = 0, groupOffset = 0, historyOffset = 0;
let graphData = null, camera = [0, 0, 900, 500];
const text = (id, value) => { $(id).textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2); };
const clear = id => $(id).replaceChildren();
function notice(value) { text('notice', value); }
function el(tag, value, className) { const e = document.createElement(tag); if (value !== undefined) e.textContent = value; if (className) e.className = className; return e; }
function button(value, handler, className) { const b = el('button', value, className); b.type = 'button'; b.onclick = () => safely(handler); return b; }
async function safely(handler) { try { await handler(); } catch (e) { notice(e.message); } }
async function api(path, body) {
  const res = await fetch(path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type':'application/json', 'X-CSRF-Token':csrf}, body: body === undefined ? undefined : JSON.stringify(body), credentials:'same-origin'});
  const data = await res.json();
  if (!res.ok) {
    if (res.status === 403) { lock(); throw Error('Access refused. Unlock again if the session expired; use the local address shown in the terminal.'); }
    throw Error(data.error + (data.next_action ? ': ' + data.next_action : ''));
  }
  return data;
}
function query(path, params) { return path + '?' + new URLSearchParams(params); }
function lock() { csrf=''; $('application').hidden=true; $('login').hidden=false; $('logout').hidden=true; }
async function unlock() { $('login').hidden=true; $('application').hidden=false; $('logout').hidden=false; await runs(); notice('Local session unlocked. Select a completed run or start an offline synthetic demonstration.'); }
$('loginForm').onsubmit = e => { e.preventDefault(); const password = $('password').value; $('password').value=''; safely(async()=>{const result = await api('/api/login', {password}); csrf=result.csrf; await unlock();}); };
$('logout').onclick = () => safely(async()=>{ await api('/api/logout', {}); lock(); notice('Session locked.'); });
async function runs(select) {
  const list = await api('/api/runs'); clear('runs'); $('runs').append(el('option','Select a run'));
  $('runs').firstChild.value='';
  for (const r of list) { const o=el('option', `${r.id} · ${r.synthetic ? 'SYNTHETIC' : 'BUILDING DATA'} · ${r.state}`); o.value=r.id; $('runs').append(o); }
  if (select || selectedRun) $('runs').value=select || selectedRun;
  if (select) await chooseRun();
}
$('refresh').onclick = () => safely(()=>runs());
$('runs').onchange = () => safely(chooseRun);
async function job(start) {
  $('runDemo').disabled=true; $('upload').disabled=true; $('export').disabled=true;
  try {
    const result=await start();
    while (true) {
      const j=await api(query('/api/job',{id:result.job_id}));
      text('progress', `${j.state.toUpperCase()} · ${j.progress}`);
      if (j.state==='failed') throw Error('Analysis/export failed: '+j.error+'. Original runs remain intact. Inspect local input formats and credentials; use CLI recovery for interrupted jobs.');
      if (j.state==='completed') { await runs(j.result.run); break; }
      await new Promise(resolve=>setTimeout(resolve,500));
    }
  } finally { $('runDemo').disabled=false; $('upload').disabled=false; $('export').disabled=false; }
}
$('runDemo').onclick = () => safely(()=>job(()=>api('/api/analyze',{demo:$('demo').value})));
$('upload').onclick = () => safely(async()=>{
  const files=[];
  for (const file of $('files').files) {
    if (file.size>2000000) throw Error('Upload limit: 2 MB per file.');
    const data=new Uint8Array(await file.arrayBuffer()); let binary='';
    for (let i=0;i<data.length;i+=8192) binary+=String.fromCharCode(...data.subarray(i,i+8192));
    files.push({name:file.name,data:btoa(binary)});
  }
  const synthetic=$('syntheticUpload').checked;
  if (!synthetic && !$('external').checked) throw Error('Real inference requires explicit external processing authorization. Synthetic test inputs can use the offline fixture interpreter.');
  const upload=await api('/api/upload',{files,config:JSON.parse($('mapping').value)});
  await job(()=>api('/api/analyze',{input_id:upload.input_id,synthetic,authorize_external_processing:$('external').checked,model:$('model').value,building_id:$('buildingId').value}));
});
async function chooseRun() {
  selectedRun=$('runs').value; graphOffset=0; previewed=null; selected=null; target=null;
  $('submit').disabled=true; $('editor').hidden=true;
  text('groups','Loading selected run clarifications…');
  clear('selection'); text('evidence','Select a node, edge or clarification.'); text('previewResult','');
  if (!selectedRun) { clear('graph'); clear('relations'); return; }
  $('search').value=''; $('predicate').value=''; $('status').value=''; $('kind').value=''; clear('focus');
  const all=el('option','All entities'); all.value=''; $('focus').append(all);
  await loadView();
  const derived=Boolean(graphData.changes);
  $('enroll').disabled=derived;
  if (!derived && reviewRun!==selectedRun) { reviewRun=''; workspace=null; revision=0; text('historyResult','No review history loaded for this run.'); }
  text('reviewStatus',derived ? `Viewing immutable derived package. Continue decisions on original run ${reviewRun || '(select original)'}.` : 'Enable local review to submit corrections.');
  groupOffset=0; historyOffset=0; await groups();
}
async function loadView() {
  if (!selectedRun) return;
  const params={run:selectedRun,query:$('search').value,focus:$('focus').value,predicate:$('predicate').value,status:$('status').value,kind:$('kind').value,offset:graphOffset};
  graphData=await api(query('/api/view',params));
  const g=graphData.graph; text('origin',graphData.manifest.synthetic?'SYNTHETIC / FOR TESTING ONLY':'PROVISIONAL BUILDING DATA · evidence may be sensitive');
  clear('summary');
  for (const status of ['verified','supported_inference','needs_review','contradicted','rejected']) { const m=el('div',undefined,'metric'); m.append(el('strong',String(g.statuses[status]||0)),el('span',status)); $('summary').append(m); }
  const existing=new Set([...$('focus').options].map(x=>x.value));
  for (const n of g.nodes.filter(n=>n.kind!=='point')) if(!existing.has(n.id)){const o=el('option',n.id);o.value=n.id;$('focus').append(o);}
  drawGraph(g);
  text('graphSize',`${g.nodes.length} nodes / ${g.edges.length} relationship edges · ${g.matched_claims} matching claims · page starts ${g.offset}. Bounds ${g.limits.nodes} nodes / ${g.limits.edges} edges. Pan for offscreen nodes or narrow with search/focus.`);
  $('nextGraph').hidden=g.next_offset===null;
  text('changes',graphData.changes || 'No derived changes in this original run.');drawChanges(graphData.changes);
  text('versionLabel',graphData.changes ? 'Actual parent → reviewed graph/claim changes. Removed claims retain their original evidence. Purple nodes were affected.' : 'Original immutable run. Corrections create a new version.');
  text('findings',{validation:graphData.validation,pinned_after_shacl:graphData.after_shacl,issues:graphData.issues,deterministic_correction_outcomes:graphData.outcomes});
  clear('downloads');
  const artifacts=['package.zip','graph.svg','ontology.ttl','candidate_claims.json','candidate_observations.json','relationships.csv','review_questions.csv','validation.json','engineering_results.json','source_records.jsonl'];
  if(graphData.changes)artifacts.push('review_basis.json','review_changes.json','after_shacl.json');
  for(const artifact of artifacts){const a=el('a',artifact,'download');a.href=query('/api/download',{run:selectedRun,artifact});a.download=artifact;$('downloads').append(a);}
}
$('filter').onclick=()=>safely(async()=>{graphOffset=0;await loadView();});
$('search').onkeydown=e=>{if(e.key==='Enter')$('filter').click();};
$('nextGraph').onclick=()=>safely(async()=>{graphOffset=graphData.graph.next_offset;await loadView();});
const ns='http://www.w3.org/2000/svg';
function svg(tag,attrs={},value){const e=document.createElementNS(ns,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,String(v));if(value!==undefined)e.textContent=value;return e;}
function cameraUpdate(){$('graph').setAttribute('viewBox',camera.join(' '));}
$('resetView').onclick=()=>{camera=[0,0,900,Math.min(650,Math.max(500,...graphData.graph.nodes.map(n=>n.y+80)))];cameraUpdate();};
$('zoomIn').onclick=()=>{camera[2]*=.8;camera[3]*=.8;cameraUpdate();};
$('zoomOut').onclick=()=>{camera[2]*=1.25;camera[3]*=1.25;cameraUpdate();};
function drawGraph(g){
  clear('graph');clear('relations');
  const defs=svg('defs'); const marker=svg('marker',{id:'arrow',viewBox:'0 0 10 10',refX:9,refY:5,markerWidth:5,markerHeight:5,orient:'auto-start-reverse'});marker.append(svg('path',{d:'M 0 0 L 10 5 L 0 10 z',fill:'#697d81'}));defs.append(marker);$('graph').append(defs);
  const nodes=new Map(g.nodes.map(n=>[n.id,{...n}]));const edgeEls=[];
  function updateEdges(){for(const {e,path,label}of edgeEls){const a=nodes.get(e.subject_id),b=nodes.get(e.object_id);if(!a||!b)continue;path.setAttribute('d',`M ${a.x} ${a.y} Q ${(a.x+b.x)/2+30} ${(a.y+b.y)/2-30} ${b.x} ${b.y}`);label.setAttribute('x',(a.x+b.x)/2);label.setAttribute('y',(a.y+b.y)/2-24);}}
  const changedEdges=new Set((graphData.changes?.claim_changes||[]).map(c=>c.claim_uid));
  for(const e of g.edges){
    const path=svg('path',{class:'edge '+(e.accepted?'accepted':'uncertain')+(changedEdges.has(e.claim_uid)?' affected':''),tabindex:0,role:'button','aria-label':`${e.accepted?'Accepted':'Candidate only'} ${e.subject_id} ${e.predicate} ${e.object_id}`,'marker-end':'url(#arrow)'});
    path.append(svg('title',{},`${e.status} · ${e.claim_uid}`));path.onclick=()=>safely(()=>inspect('claim',e.claim_uid));path.onkeydown=k=>{if(k.key==='Enter'||k.key===' ')path.onclick();};
    const label=svg('text',{},(e.accepted?'':'? ')+e.predicate);edgeEls.push({e,path,label});$('graph').append(path,label);
    $('relations').append(button(`${e.accepted?'ACCEPTED':'CANDIDATE ONLY'} · ${e.subject_id} → ${e.predicate} → ${e.object_id} · ${e.status}`,()=>inspect('claim',e.claim_uid),'relation'));
  }
  for(const n of nodes.values()){
    const group=svg('g',{class:'node'+(n.affected?' affected':''),transform:`translate(${n.x} ${n.y})`,tabindex:0,role:'button','aria-label':n.id});
    group.append(n.kind==='point'?svg('circle',{r:23,fill:'#e1f0ef',stroke:'#397e7b'}):svg('rect',{x:-110,y:-24,width:220,height:48,rx:8,fill:'#dce8ee',stroke:'#547b8c'}));
    const short=n.id.length>29?n.id.slice(0,26)+'…':n.id;
    group.append(svg('text',{'text-anchor':'middle',y:4},short),svg('title',{},n.id+' · '+n.types.map(t=>t.object_id+' ['+t.status+']').join(', ')));
    const choose=()=>safely(async()=>{if(n.claim_ids.length)await inspect('claim',n.claim_ids[0]);else notice('Select a linked relationship to inspect this entity.');});
    group.onkeydown=e=>{if(e.key==='Enter'||e.key===' ')choose();};
    group.onpointerdown=e=>{e.stopPropagation();group.setPointerCapture(e.pointerId);let moved=false;const initial={x:e.clientX,y:e.clientY,nx:n.x,ny:n.y};
      group.onpointermove=m=>{if(Math.abs(m.clientX-initial.x)+Math.abs(m.clientY-initial.y)>4)moved=true;if(moved){n.x=initial.nx+(m.clientX-initial.x)*camera[2]/$('graph').clientWidth;n.y=initial.ny+(m.clientY-initial.y)*camera[3]/$('graph').clientHeight;group.setAttribute('transform',`translate(${n.x} ${n.y})`);updateEdges();}};
      group.onpointerup=()=>{group.onpointermove=null;if(!moved)choose();};};
    $('graph').append(group);
    for(const t of n.types)$('relations').append(button(`${n.id} · type ${t.object_id} · ${t.status}`,()=>inspect('claim',t.claim_uid),'relation'));
  }
  updateEdges();camera=[0,0,900,Math.min(650,Math.max(500,...g.nodes.map(n=>n.y+80)))];cameraUpdate();
  if(!g.nodes.length)$('graph').append(svg('text',{x:30,y:60},'No matching graph elements. Clear filters or select another run.'));
}
$('graph').onpointerdown=e=>{if(e.target!==$('graph'))return;const initial={x:e.clientX,y:e.clientY,c:[...camera]};$('graph').setPointerCapture(e.pointerId);$('graph').onpointermove=m=>{camera[0]=initial.c[0]-(m.clientX-initial.x)*camera[2]/$('graph').clientWidth;camera[1]=initial.c[1]-(m.clientY-initial.y)*camera[3]/$('graph').clientHeight;cameraUpdate();};$('graph').onpointerup=()=>{$('graph').onpointermove=null;};};
async function groups(){
  if(!selectedRun)return;const result=await api(query('/api/groups',{run:selectedRun,offset:groupOffset}));clear('groups');
  for(const g of result.items){const b=button(`${g.cause_kind} · priority ${g.priority_score} · ${g.claims.length} claims`,()=>inspect('group',g.group_id),'group');b.append(el('small',' · '+(g.question||g.cause_id||'inspect evidence')));$('groups').append(b);}
  if(!result.items.length)text('groups','No unresolved clarification groups.');
  $('nextGroups').hidden=groupOffset+50>=result.total;$('previousGroups').hidden=groupOffset===0;
}
$('nextGroups').onclick=()=>safely(async()=>{groupOffset+=50;await groups();});$('previousGroups').onclick=()=>safely(async()=>{groupOffset=Math.max(0,groupOffset-50);await groups();});
$('enroll').onclick=()=>safely(async()=>{if(!selectedRun)return;workspace=await api('/api/enroll',{run:selectedRun,schema:$('reviewMode').value});reviewRun=selectedRun;const h=await api(query('/api/history',{run:reviewRun,schema:$('reviewMode').value}));revision=h.revision;text('reviewStatus',`Local principal ${workspace.reviewer.actor_id} · revision ${revision}. Verification authority NOT ESTABLISHED.`);notice('Review enabled. Inspect a clarification or claim, select exact evidence, and preview a supported correction.');});
function checkRow(label,value,kind){const row=el('label');const input=el('input');input.type='checkbox';input.value=value;input.dataset.kind=kind;row.append(input,document.createTextNode(label));return row;}
async function inspect(kind,id){
  selected=await api(query('/api/inspect',{run:selectedRun,kind,id,schema:$('reviewMode').value}));target={kind,id};previewed=null;$('submit').disabled=true;clear('selection');clear('edits');
  text('evidence',selected);text('previewResult','');
  $('selection').append(el('p','Exact claims (choose explicitly):'));
  for(const c of selected.claims){const row=checkRow(`${c.subject_id} · ${c.predicate} · ${c.object_id} [${c.status}]`,c.claim_uid,'claim');row.querySelector('input').onchange=updateEdits;$('selection').append(row);}
  $('selection').append(el('p','Original source records (choose evidence explicitly):'));
  for(const s of selected.sources){const row=checkRow(`${s.source_file} · ${s.source_location} · SHA-256 ${s.source_hash}`,s.source_record_uid,'source');row.append(el('pre',JSON.stringify({original_values:s.raw_value,original_fields:s.raw_record,normalized_fields:s.normalized_fields},null,2)));$('selection').append(row);}
  if(selected.alternatives.length)$('selection').append(el('pre','Alternative interpretations: '+JSON.stringify(selected.alternatives,null,2)));
  if(selected.conflicts.length)$('selection').append(el('pre','Contradictions: '+JSON.stringify(selected.conflicts,null,2)));
  $('editor').hidden=!(workspace && reviewRun===selectedRun);if(selected.revision!==null)revision=selected.revision;
}
function selectedInputs(kind){return [...$('selection').querySelectorAll('input:checked')].filter(i=>i.dataset.kind===kind).map(i=>i.value);}
function updateEdits(){
  clear('edits');const claims=selectedInputs('claim');
  if(claims.length){const row=el('div');const label=el('label','Apply one proposed class to all explicitly selected claims');const common=el('input');common.id='commonClass';label.append(common);row.append(label,button('Fill selected type edits',()=>{for(const input of $('edits').querySelectorAll('[data-object]'))input.value=common.value;invalidate();}));$('edits').append(row);}
  for(const uid of claims){const c=selected.claims.find(c=>c.claim_uid===uid);const box=el('div');box.dataset.uid=uid;
    for(const [field,value]of [['subject_id',c.subject_id],['predicate',c.predicate],['object_id',c.object_id]]){const label=el('label',field);const input=el('input');input.value=value;input.dataset.field=field;if(field==='object_id')input.dataset.object='yes';label.append(input);box.append(label);}$('edits').append(box);
  }invalidate();
}
function invalidate(){previewed=null;$('submit').disabled=true;}
$('editor').addEventListener('input',invalidate);$('editor').addEventListener('change',invalidate);$('selection').addEventListener('change',invalidate);
function action(){
  if(workspace.schema_version==='review-v1'){
    const claims=selectedInputs('claim');if(claims.length!==1)throw Error('Select exactly one claim for a pending proposal.');
    const intent=$('pendingAction').value;const a={schema_version:'review-v1',workspace_id:workspace.workspace_id,site_scope:workspace.site_scope,parent_fingerprint:workspace.parent_fingerprint,actor_id:workspace.reviewer.actor_id,action_id:crypto.randomUUID(),correlation_id:crypto.randomUUID(),expected_revision:revision,target_kind:'claim',target_id:claims[0],action:intent,requested_status:({accept:'supported_inference',reject:'rejected',defer:'needs_review'})[intent],reason:$('reason').value,evidence_uids:selectedInputs('source')};
    if($('supersedes').value)a.supersedes_event_id=$('supersedes').value;return a;
  }
  const kind=$('correctionKind').value;const correction={kind,clarification:$('reason').value,edits:[]};
  if(kind==='source_clarification')correction.field=$('field').value;
  else for(const box of $('edits').querySelectorAll('[data-uid]')){const edit={};for(const input of box.querySelectorAll('[data-field]'))edit[input.dataset.field]=input.value;correction.edits.push(edit);}
  if(kind==='naming_alias')correction.alias=$('alias').value;
  const a={schema_version:'review-v2',workspace_id:workspace.workspace_id,site_scope:workspace.site_scope,parent_fingerprint:workspace.parent_fingerprint,actor_id:workspace.reviewer.actor_id,action_id:crypto.randomUUID(),correlation_id:crypto.randomUUID(),expected_revision:revision,target_kind:target.kind,target_id:target.id,action:'correct',requested_status:'needs_review',reason:$('reason').value,evidence_uids:selectedInputs('source'),affected_claim_uids:selectedInputs('claim'),correction};
  if($('supersedes').value)a.supersedes_event_id=$('supersedes').value;
  return a;
}
$('preview').onclick=()=>safely(async()=>{const a=action();const result=await api('/api/preview',{run:reviewRun,action:a});previewed=a;text('previewResult',result);$('submit').disabled=false;notice('Preview only. Inspect applied/withheld consequences and actual graph/claim changes before submitting the pending proposal.');});
$('submit').onclick=()=>safely(async()=>{if(!previewed)throw Error('Preview the current correction first.');const event=await api('/api/decide',{run:reviewRun,action:previewed});revision=event.revision;invalidate();text('previewResult',event);await history();notice('Proposal recorded as PENDING. Generate a new reviewed version to apply documentary-supported deterministic consequences.');});
async function history(){if(!reviewRun)throw Error('Enable review on an original run first.');const h=await api(query('/api/history',{run:reviewRun,offset:historyOffset,schema:$('reviewMode').value}));revision=h.revision;text('historyResult',h);$('nextHistory').hidden=historyOffset+50>=h.total;$('previousHistory').hidden=historyOffset===0;}
$('history').onclick=()=>safely(history);$('nextHistory').onclick=()=>safely(async()=>{historyOffset+=50;await history();});$('previousHistory').onclick=()=>safely(async()=>{historyOffset=Math.max(0,historyOffset-50);await history();});
$('export').onclick=()=>safely(async()=>{if(!reviewRun)throw Error('Enable review and inspect decisions on an original run first.');if(workspace.schema_version!=='review-v2')throw Error('Pending v1 proposals have no graph consequences. Switch to v2 for documentary replay.');await job(()=>api('/api/export',{run:reviewRun,revision}));notice('New reviewed package published after publication verification and pinned Brick/SHACL. Parent preserved. Inspect actual changes and download exports below.');});
safely(async()=>{const s=await api('/api/session');csrf=s.csrf;await unlock();});

$('reviewMode').onchange=()=>{workspace=null;reviewRun='';revision=0;$('editor').hidden=true;$('pendingActionLabel').hidden=$('reviewMode').value!=='review-v1';text('reviewStatus','Enable the selected review mode. v1 proposals never apply graph changes; v2 corrections have separate history and deterministic replay.');};

function drawChanges(changes){
  clear('versionGraphs');text('changeSummary',changes ? `${changes.added_triples.length} added RDF triples · ${changes.removed_triples.length} removed RDF triples · ${changes.removed_claims.length} removed claims with prior evidence` : 'No reviewed graph changes.');
  if(!changes)return;
  for(const side of ['before','after']){
    const section=el('div');section.append(el('h3',side==='before'?'Before · frozen parent claims':'After · deterministic reviewed claims'));
    const entries=changes.claim_changes.filter(c=>c[side]).map(c=>c[side].claim).slice(0,24);
    const graph=svg('svg',{viewBox:`0 0 650 ${Math.max(90,entries.length*75+30)}`,role:'group','aria-label':side+' changed claim graph'});
    entries.forEach((c,i)=>{
      const y=35+i*75,accepted=['supported_inference','verified'].includes(c.status);
      const line=svg('line',{x1:180,y1:y,x2:390,y2:y,stroke:accepted?'#287c7a':'#b45f37','stroke-width':3});if(!accepted)line.setAttribute('stroke-dasharray','7 6');
      graph.append(line,svg('rect',{x:10,y:y-18,width:180,height:36,rx:6,fill:'#dce8ee'}),svg('rect',{x:390,y:y-18,width:250,height:36,rx:6,fill:accepted?'#e1f0ef':'#fff0e5'}),svg('text',{x:18,y:y+4},c.subject_id.slice(0,26)),svg('text',{x:240,y:y-8},c.predicate),svg('text',{x:398,y:y+4},c.object_id.slice(0,37)),svg('text',{x:200,y:y+32},c.status+' · '+(accepted?'accepted claim':'candidate only')));
    });
    if(!entries.length)graph.append(svg('text',{x:20,y:40},'No changed claims on this side.'));
    section.append(graph);if(changes.claim_changes.filter(c=>c[side]).length>24)section.append(el('p','First 24 changed claims shown. Complete changes and original evidence are available in the diff download.'));
    $('versionGraphs').append(section);
  }
}
