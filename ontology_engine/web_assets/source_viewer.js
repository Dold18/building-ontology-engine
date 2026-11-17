"use strict";
/* UX-I private source viewer.
 * Local File API only. No upload, external network call, persistence, inferred
 * PDF coordinates, or transfer of review authority. Hash binds to source record.
 */
const sourceViewerState={record:null,run:"",file:null,url:null,busy:false,epoch:0,verified:false};
const sv=id=>document.getElementById(id);
const svNode=(tag,value,className)=>{
  const el=document.createElement(tag);
  if(value!==undefined)el.textContent=String(value);
  if(className)el.className=className;
  return el;
};
const SOURCE_VIEWER_MAX_BYTES=128*1024*1024;
const SOURCE_VIEWER_TEXT_MAX_BYTES=8*1024*1024;
const SOURCE_VIEWER_TYPES=new Set(["pdf","csv","txt","json","jsonl","png","jpg","jpeg","xlsx"]);
function sourceViewerStatus(text,error=false){
  const box=sv("sourceViewerStatus");
  box.textContent=text;box.classList.toggle("error",error);
}
function sourceViewerRelease(){
  sourceViewerState.epoch++;
  sourceViewerState.record=null;sourceViewerState.run="";
  sourceViewerState.file=null;sourceViewerState.verified=false;sourceViewerState.busy=false;
  sv("sourceViewerPdf").removeAttribute("src");
  sv("sourceViewerImage").removeAttribute("src");
  if(sourceViewerState.url)URL.revokeObjectURL(sourceViewerState.url);
  sourceViewerState.url=null;
  sv("sourceViewerFile").value="";
  sv("sourceViewerPreview").replaceChildren();
  sv("sourceViewerPdfFrame").hidden=true;
  sv("sourceViewerImageFrame").hidden=true;
  sv("sourceViewerPageControl").hidden=true;
  sv("sourceViewerExternal").disabled=true;
  sv("sourceViewerVerify").disabled=false;
}
function sourceViewerClose(){
  if(sv("sourceViewerDialog").open)sv("sourceViewerDialog").close();
  sourceViewerRelease();
}
function sourceViewerCanOpen(record){
  const hash=record?.source_hash,ext=(record?.source_file||"").split(".").pop().toLowerCase();
  return typeof hash==="string" && /^[a-fA-F0-9]{64}$/.test(hash) &&
    SOURCE_VIEWER_TYPES.has(ext);
}
function sourceViewerOpen(record){
  if(!sourceViewerCanOpen(record))throw Error("This source lacks a supported filename and full SHA-256 digest. Local file verification is unavailable.");
  sourceViewerRelease();
  sourceViewerState.record=record;sourceViewerState.run=state.run;
  const label=(record.source_file||"Source file")+(record.source_location?" · "+record.source_location:"");
  sv("sourceViewerTitle").textContent="Inspect original source";
  sv("sourceViewerReference").textContent=label;
  sv("sourceViewerExpectedHash").textContent="Required SHA-256: "+record.source_hash.toLowerCase();
  sourceViewerStatus("Choose the original source file from your computer, then verify it against the recorded SHA-256. No file is sent to the server.");
  sv("sourceViewerDialog").showModal();
  sv("sourceViewerFile").focus();
}
function sourceViewerHex(buffer){
  return [...new Uint8Array(buffer)].map(b=>b.toString(16).padStart(2,"0")).join("");
}
function sourceViewerPDF(bytes){
  if(bytes.length<5)return false;
  return bytes[0]===37&&bytes[1]===80&&bytes[2]===68&&bytes[3]===70&&bytes[4]===45;
}
function sourceViewerPNG(bytes){
  const prefix=[137,80,78,71,13,10,26,10];
  return bytes.length>=8&&prefix.every((v,i)=>bytes[i]===v);
}
function sourceViewerJPEG(bytes){return bytes.length>=3&&bytes[0]===255&&bytes[1]===216&&bytes[2]===255;}
function sourceViewerXLSX(bytes){return bytes.length>=4&&bytes[0]===80&&bytes[1]===75&&bytes[2]===3&&bytes[3]===4;}
function sourceViewerFormatAllowed(ext,bytes){
  if(ext==="pdf")return sourceViewerPDF(bytes);
  if(ext==="png")return sourceViewerPNG(bytes);
  if(ext==="jpg"||ext==="jpeg")return sourceViewerJPEG(bytes);
  if(ext==="xlsx")return sourceViewerXLSX(bytes);
  if(!["csv","txt","json","jsonl"].includes(ext)||bytes.includes(0))return false;
  try{new TextDecoder("utf-8",{fatal:true}).decode(bytes);return true;}catch{return false;}
}
function sourceViewerText(file,bytes){
  const original=new TextDecoder("utf-8",{fatal:true}).decode(bytes);
  const lines=original.split(/\r\n|\n|\r/);
  const loc=sourceViewerState.record?.source_location||"";
  const match=/\brows (\d+)-(\d+)\b/.exec(loc);
  const lower=match?Number(match[1]):null,upper=match?Number(match[2]):null;
  const target=Number.isSafeInteger(lower)&&lower>=1&&Number.isSafeInteger(upper)&&upper>=lower?
    {lower,upper}:null;
  const first=target?Math.max(1,target.lower-12):1;
  const end=Math.min(lines.length,first+219);
  const info=svNode("p","Showing physical lines "+first+"–"+end+" of "+lines.length+
    ". Original source reference: "+(loc||"unspecified")+
    ". Highlighting is a location aid only; multiline CSV fields may span several physical lines.","small");
  sv("sourceViewerPreview").append(info);
  const pre=svNode("pre",undefined,"source-viewer-text");
  for(let line=first;line<=end;line++){
    const row=svNode("span",String(line).padStart(5," ")+"  "+lines[line-1]+"\n");
    if(target&&line>=target.lower&&line<=target.upper)row.className="source-viewer-highlight";
    pre.append(row);
  }
  sv("sourceViewerPreview").append(pre);
  if(target&&target.lower>lines.length)sv("sourceViewerPreview").append(
    svNode("p","The indexed source row exceeds the number of physical lines in this file. Check the original source metadata.","source-viewer-warning"));
}
function sourceViewerShowPreview(file,bytes,ext){
  const root=sv("sourceViewerPreview");root.replaceChildren();
  sv("sourceViewerPdfFrame").hidden=true;
  sv("sourceViewerImageFrame").hidden=true;
  sv("sourceViewerPageControl").hidden=true;
  if(["csv","txt","json","jsonl"].includes(ext)){
    if(file.size>SOURCE_VIEWER_TEXT_MAX_BYTES){
      root.append(svNode("p","Verified source identity. Inline text preview is limited to 8 MiB; open the validated file separately.","source-viewer-warning"));
      return;
    }
    sourceViewerText(file,bytes);return;
  }
  if(ext==="xlsx"){
    root.append(svNode("p","Workbook bytes match the recorded hash. Native spreadsheet rendering is not available in this offline viewer; use the exact worksheet/cell references in the evidence inspector and open the verified original in your spreadsheet program.","small"));
    return;
  }
  sourceViewerState.url=URL.createObjectURL(file);
  if(ext==="pdf"){
    sv("sourceViewerPdf").src=sourceViewerState.url;
    sv("sourceViewerPdfFrame").hidden=false;
    sv("sourceViewerPageControl").hidden=false;
    root.append(svNode("p","PDF viewer uses your browser's built-in renderer. No page number or drawing coordinate has been independently verified. Some mobile browsers require opening the verified file in a new tab.","source-viewer-warning"));
  }else{
    sv("sourceViewerImage").src=sourceViewerState.url;
    sv("sourceViewerImageFrame").hidden=false;
    root.append(svNode("p","Original image bytes match the source hash. No detected feature or connection is highlighted.","small"));
  }
}
async function sourceViewerVerify(){
  if(sourceViewerState.busy)return;
  const record=sourceViewerState.record,selected=sv("sourceViewerFile").files?.[0];
  if(!record||!selected||!state.run||state.run!==sourceViewerState.run)throw Error("Select a source record and file in the current run.");
  if(!selected.size||selected.size>SOURCE_VIEWER_MAX_BYTES)throw Error("File must be 1 byte to 128 MiB. No data was sent.");
  const epoch=++sourceViewerState.epoch;
  sourceViewerState.busy=true;sv("sourceViewerVerify").disabled=true;
  sv("sourceViewerExternal").disabled=true;
  sourceViewerStatus("Computing local SHA-256 of selected file. No upload or model call is occurring.");
  try{
    const raw=await selected.arrayBuffer();
    if(epoch!==sourceViewerState.epoch||state.run!==sourceViewerState.run)return;
    const digest=sourceViewerHex(await crypto.subtle.digest("SHA-256",raw));
    if(epoch!==sourceViewerState.epoch||state.run!==sourceViewerState.run)return;
    if(digest!==record.source_hash.toLowerCase()){
      sourceViewerStatus("SHA-256 MISMATCH. This is not the exact recorded source file. Preview refused.",true);
      sv("sourceViewerPreview").replaceChildren();return;
    }
    const ext=(record.source_file||"").split(".").pop().toLowerCase();
    if(!sourceViewerFormatAllowed(ext,new Uint8Array(raw))){
      sourceViewerStatus("The bytes match the recorded digest but do not match its declared file format. Preview refused.",true);
      return;
    }
    sourceViewerState.file=selected;
    sourceViewerState.verified=true;
    sourceViewerShowPreview(selected,new Uint8Array(raw),ext);
    sourceViewerStatus("SHA-256 verified against this source record. File identity confirmed; engineering conclusions and PDF coordinates are not validated.");
    sv("sourceViewerExternal").disabled=false;
  }finally{
    if(epoch===sourceViewerState.epoch){
      sourceViewerState.busy=false;sv("sourceViewerVerify").disabled=false;
    }
  }
}
function sourceViewerPage(){
  if(!sourceViewerState.verified||!sourceViewerState.url)return;
  const page=Number(sv("sourceViewerPage").value);
  if(!Number.isSafeInteger(page)||page<1||page>9999)return;
  sv("sourceViewerPdf").src=sourceViewerState.url+"#page="+page;
}
sv("sourceViewerPage").onchange=sourceViewerPage;
sv("sourceViewerVerify").onclick=()=>safe(sourceViewerVerify);
sv("sourceViewerExternal").onclick=()=>{
  if(!sourceViewerState.verified||!sourceViewerState.file||!state.run||state.run!==sourceViewerState.run)return;
  if(!sourceViewerState.url)sourceViewerState.url=URL.createObjectURL(sourceViewerState.file);
  window.open(sourceViewerState.url,"_blank","noopener,noreferrer");
};
sv("sourceViewerClose").onclick=sourceViewerClose;
sv("sourceViewerDialog").addEventListener("close",sourceViewerRelease);
sv("sourceViewerFile").addEventListener("change",()=>{
  sourceViewerState.epoch++;sourceViewerState.busy=false;sourceViewerState.verified=false;sourceViewerState.file=null;
  sv("sourceViewerVerify").disabled=false;
  sv("sourceViewerExternal").disabled=true;
  sv("sourceViewerPdf").removeAttribute("src");sv("sourceViewerImage").removeAttribute("src");
  if(sourceViewerState.url)URL.revokeObjectURL(sourceViewerState.url);
  sourceViewerState.url=null;
  sv("sourceViewerPdfFrame").hidden=true;sv("sourceViewerImageFrame").hidden=true;
  sv("sourceViewerPageControl").hidden=true;sv("sourceViewerPreview").replaceChildren();
  sourceViewerStatus("File selected. Verify its SHA-256 to unlock a source preview.");
});
window.addEventListener("pagehide",sourceViewerRelease);
