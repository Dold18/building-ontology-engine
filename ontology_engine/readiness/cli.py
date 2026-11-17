"""Local production CLI. No model, telemetry service, credential or control action."""
import json
import sys
import sqlite3
from .publication import create_report, verify_report


def register(sub):
    command=sub.add_parser('readiness',help='Evidence-backed analytics readiness over one verified completed run')
    commands=command.add_subparsers(dest='readiness_command',required=True)
    create=commands.add_parser('create',help='Publish a new immutable source-bound JSON/HTML report')
    create.add_argument('--parent',required=True);create.add_argument('--output',required=True)
    create.add_argument('--resume',action='store_true')
    verify=commands.add_parser('verify',help='Independently verify hashes, parent binding and deterministic report semantics')
    verify.add_argument('--report',required=True);verify.add_argument('--parent')


def dispatch(args):
    try:
        result=create_report(args.parent,args.output,args.resume) if args.readiness_command=='create' else verify_report(args.report,args.parent)
        print(json.dumps(result,sort_keys=True,ensure_ascii=True))
        return 0
    except (ValueError,OSError,KeyError,sqlite3.Error) as exc:
        # Avoid returning source/reviewer content or sensitive absolute paths.
        from ..errors import EngineError
        code=exc.code if isinstance(exc,EngineError) else str(exc) if isinstance(exc,ValueError) and str(exc).startswith('READINESS_') else 'READINESS_INVALID_INPUT'
        print(json.dumps({'error':code,'next_action':'Inspect package integrity, exact parent/version, report prerequisites or saved recovery state.'}),file=sys.stderr)
        return 1
