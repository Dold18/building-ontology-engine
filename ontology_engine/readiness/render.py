"""Portable inert report, with escaped original source content and strict JSON."""
import html
import json
from ..commissioning.render import json_bytes


def render(report):
    esc=lambda v:html.escape(str(v),quote=True)
    pretty=lambda v:esc(json.dumps(v,sort_keys=True,indent=2,ensure_ascii=True,allow_nan=False))
    chunks=['<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Energy analytics readiness</title></head><body><h1>Energy analytics readiness</h1>',
        '<p><strong>'+('SYNTHETIC / FOR TESTING ONLY' if report.synthetic else 'PROVISIONAL / SOURCE-BOUND ANALYSIS')+'</strong></p>',
        '<p>Site: '+esc(report.site_scope)+'</p><p>Parent run: '+esc(report.parent_run_id)+'</p>',
        '<p>Ready means prerequisite-complete for the bounded calculation; supplied evidence remains unconfirmed.</p>',
        '<h2>Graph queries</h2>']
    for q in report.queries:
        chunks+=['<section><h3>'+esc(q.kind)+' — '+esc(q.target_id)+' — '+esc(q.state)+'</h3>',
            '<p>'+esc(q.limitation)+'</p><pre>'+pretty(q.model_dump())+'</pre></section>']
    chunks+=['<h2>Sensible-water thermal readiness</h2>']
    for a in report.assessments:
        chunks+=['<section><h3>'+esc(a.target_id)+' — '+esc(a.state)+'</h3>',
            '<p>'+esc(a.next_action)+'</p><pre>'+pretty(a.model_dump())+'</pre></section>']
    for title,value in [('Limitations',report.limitations),('Entities and graph IRIs',report.entities),('Claim/status lineage',report.claims),
        ('Observations',report.observations),('Conflicts',report.conflicts),('Original source records, locations and hashes',[r.model_dump() for r in report.evidence]),
        ('All engineering checks',report.engineering_checks),('Original input hashes',report.input_hashes),('Parent artifact hashes',report.artifact_hashes)]:
        chunks+=['<details><summary>'+title+'</summary><pre>'+pretty(value)+'</pre></details>']
    chunks+=['<p>Complete immutable graph, source bytes and review history: parent_package/. Integrity binding: report_manifest.json.</p></body></html>']
    return {'readiness_report.json':json_bytes(report.model_dump()),'readiness_report.html':('\n'.join(chunks)+'\n').encode()}
