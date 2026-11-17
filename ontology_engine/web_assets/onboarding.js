"use strict";
/* UX-G: user-initiated, authenticated, local-only file inspection.
   Never submits source content to an external provider or approves inference.
   All source values are textContent, never executable markup. */
let onboardingReport=null, onboardingTables=[], onboardingEpoch=0, onboardingBusy=false;
const ob=id=>document.getElementById(id);
const obInfo=(v,cls="small")=>node("p",v,cls);
const obEl=(tag,value,cls)=>node(tag,value,cls);
function onboardingReset(){
  onboardingEpoch++;onboardingReport=null;onboardingTables=[];
  ob("mappingApply").disabled=true;
  ob("mappingInspect").disabled=false;
  ob("mappingPreview").replaceChildren(obInfo("Files changed. Inspect again to review fresh source headers and worksheets."));
}
ob("guidedFiles").addEventListener("change",onboardingReset);
function onboardingUnique(fields){
  const seen=new Set();
  for(const f of fields){if(!f)continue;if(seen.has(f))return false;seen.add(f);}return true;
}
function onboardingKindWarn(table){
  const required=onboardingReport.required_by_kind[table.kind]||[];
  const fields=Object.values(table.selection).filter(Boolean);
  const missing=required.filter(x=>!fields.includes(x));
  return missing;
}
function onboardingRender(){
  const host=ob("mappingPreview");host.replaceChildren();
  if(!onboardingReport){host.append(obInfo("Inspect locally to preview CSV and XLSX source headers."));return;}
  host.append(obInfo(onboardingReport.file_count+" files · "+onboardingTables.length+
    " tables/worksheets · "+Number(onboardingReport.bytes).toLocaleString()+
    " bytes inspected locally. No API inference request was made.","mapping-summary"));
  if(onboardingReport.warning)host.append(obInfo(onboardingReport.warning));
  for(const [index,table] of onboardingTables.entries()){
    const section=document.createElement("details");
    section.className="onboard-sheet";section.open=index===0;
    section.append(obEl("summary",table.file+(table.sheet?" / "+table.sheet:"")+
      " · "+table.kind+" · assumed header row 1"));
    const inner=obEl("div",undefined,"onboard-sheet-body");
    if(table.warnings?.length){
      const warning=obEl("div",undefined,"onboard-warnings");
      for(const msg of table.warnings)warning.append(obInfo(msg));inner.append(warning);
    }
    const kindLabel=obEl("label","Source table type","onboard-kind"),kind=document.createElement("select");
    for(const key of Object.keys(onboardingReport.required_by_kind)){
      const option=obEl("option",key);option.value=key;kind.append(option);
    }
    kind.value=table.kind;
    kind.onchange=()=>{table.kind=kind.value;onboardingInvalidate();};
    kindLabel.append(kind);inner.append(kindLabel);
    if(!table.headers.length){
      inner.append(obInfo("No readable column headers were found. This table cannot be reliably classified."));
    }else{
      const heading=obEl("h4","Match source columns to known fields");
      inner.append(heading);
      const grid=obEl("div",undefined,"onboard-column-grid");
      for(const header of table.headers){
        const l=obEl("label",header,"onboard-column");
        const select=document.createElement("select");
        const empty=obEl("option","Keep source-only / not mapped");empty.value="";select.append(empty);
        for(const canonical of onboardingReport.canonical_fields){
          const opt=obEl("option",canonical);opt.value=canonical;select.append(opt);
        }
        select.value=table.selection[header]||"";
        select.onchange=()=>{table.selection[header]=select.value;onboardingInvalidate();};
        l.append(select);grid.append(l);
      }
      inner.append(grid);
      const wrapper=obEl("div",undefined,"onboard-table-wrapper");
      const preview=obEl("table",undefined,"onboard-preview-table");
      const head=document.createElement("thead"),headRow=document.createElement("tr");
      for(const header of table.headers){headRow.append(obEl("th",header||"(blank)"));}
      head.append(headRow);preview.append(head);
      const body=document.createElement("tbody");
      for(const row of table.sample_rows){
        const tr=document.createElement("tr");
        for(let i=0;i<table.headers.length;i++)tr.append(obEl("td",row[i]||""));
        body.append(tr);
      }
      preview.append(body);wrapper.append(preview);inner.append(wrapper);
    }
    inner.append(obInfo("This is a bounded sample, not a comprehensive data-quality or classification assessment."));
    section.append(inner);host.append(section);
  }
  const validations=obEl("div",undefined,"onboard-validation");
  for(const table of onboardingTables){
    const missing=onboardingKindWarn(table);
    const dup=!onboardingUnique(Object.values(table.selection));
    if(table.invalid_headers)validations.append(obInfo(table.file+
      (table.sheet?" / "+table.sheet:"")+": blank or duplicate source headers require a corrected input file or manual header configuration.","onboard-error"));
    if(missing.length)validations.append(obInfo(table.file+
      (table.sheet?" / "+table.sheet:"")+": unmapped required fields — "+missing.join(", "),"onboard-error"));
    if(dup)validations.append(obInfo(table.file+": one canonical field was selected for multiple source columns.","onboard-error"));
    if(table.kind==="unknown")validations.append(obInfo(table.file+
      (table.sheet?" / "+table.sheet:"")+": unclassified; the backend will retain it as unknown and flag it during ingestion.","onboard-warning"));
  }
  host.append(validations);
  ob("mappingApply").disabled=onboardingBusy||onboardingTables.some(t=>
    t.invalid_headers||onboardingKindWarn(t).length||!onboardingUnique(Object.values(t.selection)));
}
function onboardingInvalidate(){
  ob("mappingApply").disabled=true;
  // Re-render to refresh required-field diagnostics. Original source previews remain unchanged.
  onboardingRender();
}
async function onboardingInspect(){
  if(onboardingBusy)throw Error("An input inspection is already in progress.");
  const input=[...ob("guidedFiles").files];
  if(!input.length||input.length>10||input.reduce((n,f)=>n+f.size,0)>8_000_000){
    throw Error("Select 1–10 files totaling no more than 8 MB.");
  }
  const epoch=++onboardingEpoch;
  onboardingBusy=true;onboardingReport=null;onboardingTables=[];
  ob("mappingInspect").disabled=true;ob("mappingApply").disabled=true;
  ob("mappingPreview").replaceChildren(obInfo("Inspecting file structure and sample rows on the local server…"));
  try{
    const files=await Promise.all(input.map(encodeFile));
    if(epoch!==onboardingEpoch)return;
    const result=await api("/api/input/inspect",{files});
    if(epoch!==onboardingEpoch)return;
    onboardingReport=result;
    onboardingTables=result.files.flatMap(f=>f.tables.map(t=>{
      const selection=Object.fromEntries(t.headers.map(h=>[h,""]));
      for(const [canonical,source] of Object.entries(t.proposed_columns||{})){
        if(Object.prototype.hasOwnProperty.call(selection,source))selection[source]=canonical;
      }
      return {...t,file:f.name,kind:t.proposed_kind,selection};
    }));
    onboardingRender();
    say("File structure inspected locally. Review mappings before applying; no external inference has been authorized.");
  }finally{
    if(epoch===onboardingEpoch){onboardingBusy=false;ob("mappingInspect").disabled=false;onboardingRender();}
  }
}
ob("mappingInspect").onclick=()=>safe(onboardingInspect);
ob("mappingApply").onclick=()=>{
  if(!onboardingReport||onboardingBusy)return;
  if(onboardingTables.some(t=>t.invalid_headers||onboardingKindWarn(t).length||
    !onboardingUnique(Object.values(t.selection))))throw Error("Resolve invalid headers or missing required mappings.");
  let existing;
  try{existing=JSON.parse(ob("guidedMapping").value);}
  catch{say("Advanced mapping JSON is invalid; correct it before applying suggestions.",true);return;}
  if(!existing||Array.isArray(existing)||typeof existing!=="object"){
    say("Advanced mapping must be a JSON object.",true);return;
  }
  if(Array.isArray(existing.tables)&&existing.tables.length){
    say("Existing table mappings are present. Clear them manually to avoid overwriting prior work.",true);return;
  }
  const tables=onboardingTables.map(t=>{
    const columns=Object.fromEntries(Object.entries(t.selection).filter(([,canonical])=>canonical)
      .map(([source,canonical])=>[canonical,source]));
    const mapping={file:t.file,kind:t.kind,header_row:t.header_row,columns};
    if(t.sheet!==null&&t.sheet!==undefined)mapping.sheet=t.sheet;
    return mapping;
  });
  existing.version=1;existing.tables=tables;
  ob("guidedMapping").value=JSON.stringify(existing,null,2);
  ob("guidedMapping").dispatchEvent(new Event("change",{bubbles:true}));
  say("Reviewed CSV/XLSX worksheet mappings applied. Source files are unchanged; upload locally for backend validation.");
};
