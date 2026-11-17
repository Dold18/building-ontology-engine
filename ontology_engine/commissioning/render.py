"""Deterministic inert HTML, original JSON and spreadsheet-safe CSV projections."""
import csv
import base64
import hashlib
import html
import io
import json

from ..csv_safety import encode_cell

CSS = '''body{font:16px/1.5 system-ui,sans-serif;color:#18343c;max-width:1160px;margin:2rem auto;padding:0 1rem;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;margin:1rem 0;table-layout:fixed}th,td{text-align:left;vertical-align:top;border:1px solid #aabfc5;padding:.6rem;overflow-wrap:anywhere}th{background:#e7f0f2}
pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:28rem;overflow:auto;background:#f2f6f7;padding:1rem;font-size:.86rem}
section{border-top:2px solid #ccdce0;padding-top:1rem;margin-top:2rem}details{margin:.7rem 0}summary{cursor:pointer;font-weight:600}h1,h2,h3{line-height:1.25}a{color:#075c67}.counts{display:flex;gap:1rem;flex-wrap:wrap}.counts table{flex:1;min-width:230px}
@media print{body{max-width:none}pre{max-height:none;overflow:visible}section{break-inside:avoid}}'''


def json_bytes(value):
    return (json.dumps(value,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False)+'\n').encode()


def render(report):
    data=report.model_dump()
    stream=io.StringIO(newline='')
    fields=['finding_uid','target_id','category','status','rule_id','rule_version','score','significance',
            'source_ids','source_uids','reason_codes','recommended_investigation','alternatives','confirmed_fault']
    writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
    for f in report.findings:
        row={k:getattr(f,k) for k in ('finding_uid','target_id','category','status','rule_id','rule_version','source_ids','source_uids','reason_codes','recommended_investigation','confirmed_fault')}
        row.update(score=f.priority.score,significance=f.priority.significance,alternatives=f.alternative_explanations)
        writer.writerow({k:encode_cell(json.dumps(v,sort_keys=True,ensure_ascii=True) if isinstance(v,(list,dict)) else str(v)) for k,v in row.items()})
    esc=lambda v:html.escape(str(v),quote=True)
    pretty=lambda v:esc(json.dumps(v,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False))
    style_hash=base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()
    chunks=['<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'sha256-'+style_hash+'\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Commissioning investigations</title><style>'+CSS+'</style></head><body><h1>Commissioning investigation report</h1>',
        '<p><strong>'+('SYNTHETIC / FOR TESTING ONLY' if report.synthetic else 'PROVISIONAL / SOURCE-BOUND SCREENING')+'</strong></p>',
        '<p>Findings identify questions to investigate. They are not confirmed equipment faults, repair orders or control instructions. No VERIFIED promotion occurs.</p>',
        '<h2>Executive summary</h2><p>Site: '+esc(report.site_scope)+'</p><p>'+str(len(report.findings))+
        ' rule findings; several may relate to one underlying issue. '+str(report.summary['by_status'].get('not_evaluable',0))+
        ' findings cannot be evaluated without further evidence. Confirmed faults: <strong>0</strong>.</p><div class="counts">']
    for label,key in [('Category','by_category'),('Status','by_status'),('Significance','by_significance')]:
        chunks.append('<table><thead><tr><th>'+label+'</th><th>Findings</th></tr></thead><tbody>')
        chunks.extend('<tr><td>'+esc(k)+'</td><td>'+str(v)+'</td></tr>' for k,v in report.summary[key].items())
        chunks.append('</tbody></table>')
    chunks += ['</div><details><summary>Exact run binding and machine summary</summary><pre>'+pretty(dict(report.summary,
        parent_run_id=report.parent_run_id,parent_fingerprint=report.parent_fingerprint))+'</pre></details>',
        '<h2>Prioritized investigation queue</h2><p>Scores order documentary/field investigation. Operational criticality, urgency, impact, probabilities and human minutes remain unknown.</p>',
        '<table><thead><tr><th>Finding / target</th><th>Category</th><th>Status / outcome</th><th>Significance / score</th><th>Next step</th></tr></thead><tbody>']
    for f in report.findings:
        chunks.append('<tr><td><a href="#'+esc(f.finding_uid)+'">'+esc(f.target_id)+'</a></td><td>'+esc(f.category)+
            '</td><td>'+esc(f.status)+' / '+esc(f.detection_outcome)+'</td><td>'+esc(f.priority.significance)+' / '+str(f.priority.score)+
            '</td><td>'+esc(' '.join(f.recommended_investigation))+'</td></tr>')
    chunks.append('</tbody></table><h2>Detailed findings and exact evidence</h2>')
    for f in report.findings:
        chunks.append('<section id="'+esc(f.finding_uid)+'"><h3>'+esc(f.rule_id)+' — '+esc(f.target_id)+'</h3>')
        for label,value in [('Observed evidence',f.observed_evidence),('Expected condition / prerequisites',f.expected_condition),
            ('Required investigation / field verification',f.recommended_investigation),('Plausible alternatives',f.alternative_explanations),
            ('Priority factors',f.priority.model_dump()),('Uncertainty',f.uncertainty),('Original source records / locations / hashes',[r.model_dump() for r in f.evidence]),
            ('Artifact evidence',[a.model_dump() for a in f.artifact_evidence]),('Related claims/conflicts/review questions',
             {'claims':f.claim_uids,'observations':f.observation_uids,'conflicts':f.conflict_uids,'questions':f.review_question_ids,'issues':f.original_issue_ids})]:
            if label in {'Priority factors','Original source records / locations / hashes','Artifact evidence','Related claims/conflicts/review questions'}:
                chunks.append('<details><summary>'+label+'</summary><pre>'+pretty(value)+'</pre></details>')
            else:chunks.append('<h4>'+label+'</h4><pre>'+pretty(value)+'</pre>')
        chunks.append('</section>')
    chunks.append('<h2>All existing checks, including unresolved / not_evaluable prerequisites</h2><details><summary>'+str(len(report.checks))+' exact engineering checks</summary><pre>'+pretty(report.checks)+'</pre></details>')
    chunks.append('<h2>Limitations and provenance</h2><pre>'+pretty(report.limitations)+'</pre><p>Full original package, source bytes, graph and correction history: parent_package/. Manifest: report_manifest.json. No live or model execution.</p></body></html>')
    return {'commissioning_report.json':json_bytes(data),'commissioning_report.html':('\n'.join(chunks)+'\n').encode(),
            'findings.csv':stream.getvalue().encode()}
