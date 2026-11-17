"""Offline JSON CLI over the shared Phase A review service (no graph replay)."""
import json
from pydantic import ValidationError
from .errors import EngineError
from .review import ReviewService, create_workspace, read_bytes, MAX_REQUEST_BYTES


def register(subparsers):
    review = subparsers.add_parser('review', help='Durable pending human review; never changes original graphs')
    commands = review.add_subparsers(dest='review_command', required=True)
    init = commands.add_parser('init', help='Enroll one local principal; issue a private credential file')
    init.add_argument('--parent', required=True)
    init.add_argument('--workspace', required=True)
    init.add_argument('--actor', required=True)
    init.add_argument('--role', choices=['reviewer', 'observer'], default='reviewer')
    init.add_argument('--credential-file', required=True)
    init.add_argument('--schema-version', choices=['review-v1', 'review-v2'], default='review-v1')
    for name in ('queue', 'groups', 'inspect', 'preview', 'decide', 'history', 'verify', 'export'):
        command = commands.add_parser(name)
        command.add_argument('--workspace', required=True)
        if name in ('queue', 'groups', 'history'):
            command.add_argument('--offset', type=int, default=0)
            command.add_argument('--limit', type=int, default=50)
        if name == 'queue': command.add_argument('--include-accepted', action='store_true')
        if name == 'inspect':
            command.add_argument('--kind', choices=['claim', 'question', 'group'], required=True)
            command.add_argument('--id', required=True)
        if name in ('preview', 'decide'):
            command.add_argument('--request', required=True, help='Bounded review-v1 JSON action file')
            command.add_argument('--credential-file', required=True, help='Owner-only local bearer file; never token text')
        if name == 'export':
            command.add_argument('--output', required=True)
            command.add_argument('--expected-revision', required=True, type=int)
            command.add_argument('--credential-file', required=True)
            command.add_argument('--resume', action='store_true')


def dispatch(args):
    try:
        if args.review_command == 'init':
            result = create_workspace(args.parent, args.workspace, args.actor, args.credential_file, args.role, args.schema_version).model_dump()
        else:
            service = ReviewService(args.workspace)
            name = args.review_command
            if name == 'queue': result = service.queue(args.offset, args.limit, args.include_accepted)
            elif name == 'groups': result = service.groups(args.offset, args.limit)
            elif name == 'history': result = service.history(args.offset, args.limit)
            elif name == 'inspect': result = service.inspect(args.kind, args.id)
            elif name == 'verify': result = service.verify()
            elif name == 'export':
                from .review_export import export_review
                result = export_review(service, args.output, args.credential_file, args.expected_revision, args.resume)
            else:
                request = json.loads(read_bytes(args.request, MAX_REQUEST_BYTES))
                result = service.decide(request, args.credential_file, preview=name=='preview').model_dump()
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    except (ValueError, OSError) as exc:
        # Do not echo malformed requests, secrets, or source text in diagnostics.
        code = exc.code if isinstance(exc, EngineError) else 'REVIEW_SCHEMA' if isinstance(exc, ValidationError) else 'REVIEW_INPUT_ERROR'
        print(json.dumps({'error_code': code, 'status': 'refused', 'graph_changes': 'not_applied'}))
        return 1
