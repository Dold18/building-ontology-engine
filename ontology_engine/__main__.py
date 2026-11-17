import argparse
import os
import sys
from .pipeline import run, write_json
from .config import load_config
from .ingest import ingest
from .inventory import inventory
from .sources import snapshot_input
from .limits import load_limits
from .pipeline import resume
from .reference_converter import convert_reference
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Evidence-preserving Brick ontology generator')
    sub = parser.add_subparsers(dest='command', required=True)
    command = sub.add_parser('run')
    command.add_argument('--input', required=True)
    command.add_argument('--output', required=True)
    command.add_argument('--llm-provider', choices=['fixture', 'openai'], required=True)
    command.add_argument('--model', default=os.environ.get('ONTOLOGY_LLM_MODEL'))
    command.add_argument('--limits', help='JSON resource budget overrides within documented guardrails')
    command.add_argument('--config', help='JSON file/sheet/column mapping configuration')
    command.add_argument('--building-id', help='Stable site scope; default derives from canonical input root')
    command.add_argument('--allow-external-processing', action='store_true',
                         help='Explicit owner authorization to transmit source rows to OpenAI')
    listing = sub.add_parser('inventory', help='Read-only file/worksheet inventory; never calls an LLM')
    listing.add_argument('--input', required=True)
    listing.add_argument('--output', required=True)
    listing.add_argument('--config')
    conversion = sub.add_parser('convert-reference', help='Derive metadata inputs while withholding graph targets')
    conversion.add_argument('--source', required=True)
    conversion.add_argument('--output', required=True)
    conversion.add_argument('--source-url', required=True)
    conversion.add_argument('--license-note', required=True)
    conversion.add_argument('--max-equipment', type=int, default=5)
    conversion.add_argument('--max-points', type=int, default=20)
    validation = sub.add_parser('validate', help='Offline pinned Brick/SHACL check; does not establish evidence truth')
    validation.add_argument('--graph', required=True)
    validation.add_argument('--output', required=True)
    recovery = sub.add_parser('resume', help='Resume only hash-verified committed stages; never replay ambiguous model requests')
    recovery.add_argument('--output', required=True)
    web = sub.add_parser('web', help='Authenticated local browser application, bound only to 127.0.0.1')
    web.add_argument('--data-root', required=True, help='Private local application state directory')
    web.add_argument('--runs-root', help='Operator-selected directory of completed runs; defaults to data-root/runs')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--password-file', help='Owner-only file containing a 16–256 character local login password; otherwise prompt securely')
    from .review_cli import register, dispatch
    register(sub)
    from .commissioning.cli import register as register_commissioning, dispatch as dispatch_commissioning
    register_commissioning(sub)
    from .readiness.cli import register as register_readiness, dispatch as dispatch_readiness
    register_readiness(sub)
    args = parser.parse_args()
    if args.command == 'review':
        return dispatch(args)
    if args.command == 'readiness':
        return dispatch_readiness(args)
    if args.command == 'commissioning':
        return dispatch_commissioning(args)
    try:
        if args.command == 'web':
            from .web import serve
            serve(args.data_root, args.runs_root, args.port, args.password_file)
            return 0
        if args.command == 'validate':
            from rdflib import Graph
            from .standards import validate_external
            target = Path(args.output)
            if target.exists(): raise ValueError('Validation output must be new')
            graph_path = Path(args.graph)
            if graph_path.is_symlink() or graph_path.stat().st_size > 20_000_000:
                raise ValueError('Validation input must be a regular bounded Turtle file')
            result = validate_external(Graph().parse(graph_path, format='turtle'))
            target.parent.mkdir(parents=True, exist_ok=True)
            write_json(target, result)
            print('Pinned Brick SHACL: ' + result['full_brick_shacl'])
            return 0 if result['full_brick_shacl'] == result['released_vocabulary'] == 'passed' else 1
        if args.command == 'resume':
            manifest = resume(args.output)
            print('Recovered verified run: ' + manifest.run_id)
            return 0
        if args.command == 'convert-reference':
            result = convert_reference(args.source, args.output, args.source_url, args.license_note,
                                       args.max_equipment, args.max_points)
            print('PUBLIC REFERENCE DERIVED / NOT ORIGINAL BMS EXPORT', result['counts'])
            return 0
        if args.command == 'inventory':
            target = Path(args.output)
            if target.exists():
                raise ValueError('Inventory output must be new')
            snapshot = snapshot_input(args.input)
            records, hashes, _ = ingest(Path(args.input), load_config(args.config), snapshot=snapshot)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_json(target, inventory(args.input, records, hashes, snapshot))
            print('Read-only inventory exported to ' + str(target))
            return 0
        manifest = run(args.input, args.output, args.llm_provider, args.model,
                       args.allow_external_processing, args.building_id, args.config, load_limits(args.limits))
    except (ValueError, OSError) as exc:
        print('Run failed: ' + str(exc), file=sys.stderr)
        return 1
    print(f'Exported {manifest.counts["accepted_triples"]} accepted triples to {args.output}')
    print('SYNTHETIC / FOR TESTING ONLY' if manifest.synthetic else 'PROVISIONAL / UNREVIEWED INPUT')
    return 0

if __name__ == '__main__':
    sys.exit(main())
