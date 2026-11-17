"""Owner-decision gate: sponsor-data payload construction and live calls are blocked until decisions are recorded.

Fail-closed decision model (owner-gate correction 1):
- every decision is validated against its meaning, not merely for being non-empty;
- permission decisions (P-8, P-10, the split confirmation, the L0 payload approval) are granted ONLY by the JSON literal
  `true`; `false`, "false", "declined", "denied" and every other value are refused;
- missing, malformed or contradictory entries, unknown keys and duplicate JSON keys refuse the whole stage;
- each entry needs a human decision-maker and a valid, non-future date; agent identities are refused, so the construction
  agent cannot record approvals on the owner's behalf;
- L1 needs its own decisions (P-10 and a separate L0-payload approval): an approved L0 never implies L1.
"""
import json
import re
from datetime import date
from pathlib import Path
from ..errors import EngineError

SCHEMA = 'owner-decisions-v1'
L0_REQUIRED = ('P-1', 'P-2', 'P-3', 'P-4', 'P-5', 'P-6', 'P-7', 'P-8', 'P-9', 'split')
L1_REQUIRED = L0_REQUIRED + ('P-10', 'l0_payload_approved')
SCOPE_KEY = 'inference_scope_approved'   # optional; required only for runs beyond the L1 smoke test
KNOWN = set(L1_REQUIRED) | {SCOPE_KEY}
SCOPE_FIELDS = {'development_group', 'data_classes', 'models', 'max_spend_usd', 'standing', 'plan_sha256', 'expires_on'}
ENTRY_KEYS = {'label', 'value', 'recorded_by', 'recorded_on', 'notes'}
PROVIDERS = {'anthropic', 'openai'}
ANTHROPIC_MODELS = {'claude-haiku-5-5', 'claude-sonnet-5-5'}   # must equal anthropic_provider.MODEL_PROFILES (tested)
DATA_CLASSES = {'raw_bas_point_metadata', 'drawing_text_layer', 'drawing_ocr_text', 'drawing_page_images'}
GROUPS = {'demo_building', 'second_building', 'synthetic_research_building'}
NEGATIVE = {'false', 'no', 'n', 'declined', 'decline', 'denied', 'deny', 'rejected', 'reject', 'revoked', 'withdrawn',
            'not approved', 'not_approved', 'unapproved', 'pending', 'tbd', 'todo', 'unknown', 'none', 'null', 'n/a', 'na', '0', ''}
NEGATIVE_FRAGMENTS = ('not ', 'deni', 'declin', 'reject', 'revok', 'withdr', 'refus', 'pending')
AGENT_IDENTITY = re.compile(r'\b(claude|assistant|agent|gpt|llm|chatbot|bot|copilot|ai model|language model|construction harness)\b', re.I)
ENV_NAME = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Duplicate('duplicate JSON key')
    return dict(pairs)


def _negative_text(value):
    text = str(value).strip().casefold()
    return text in NEGATIVE or any(f in text for f in NEGATIVE_FRAGMENTS)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _permission(value):
    if value is True:
        return 'granted', []
    if value is False or (isinstance(value, str) and _negative_text(value)):
        return 'denied', ['EXPLICIT_DENIAL']
    return 'malformed', ['PERMISSION_REQUIRES_LITERAL_TRUE']


def _text(value, minimum=3):
    if not isinstance(value, str) or len(value.strip()) < minimum:
        return 'malformed', ['TEXT_REQUIRED']
    if _negative_text(value):
        return 'denied', ['NEGATIVE_TEXT']
    return 'granted', []


def _validate(key, value, decisions):
    """Return (status, reasons) for one decision value according to its meaning."""
    if key in ('P-8', 'P-10', 'l0_payload_approved'):
        return _permission(value)
    if key == 'P-1':
        if isinstance(value, str) and _negative_text(value):
            return 'denied', ['EXPLICIT_DENIAL']
        return ('granted', []) if value in PROVIDERS else ('malformed', ['UNSUPPORTED_PROVIDER'])
    if key == 'P-2':
        if value is False or (isinstance(value, str) and _negative_text(value)):
            return 'denied', ['EXPLICIT_DENIAL']
        if not isinstance(value, list) or not value or not all(isinstance(m, str) and m.strip() for m in value) or len(set(value)) != len(value):
            return 'malformed', ['MODEL_LIST_REQUIRED']
        provider = ((decisions.get('P-1') or {}).get('value'))
        if provider == 'anthropic' and not set(value) <= ANTHROPIC_MODELS:
            return 'malformed', ['MODEL_NOT_IN_ALLOW_LIST']
        return 'granted', []
    if key in ('P-3', 'P-9'):
        status, reasons = _text(value, 2 if key == 'P-9' else 3)
        if status == 'granted' and key == 'P-9' and _agent_like(value):
            return 'malformed', ['APPROVER_MUST_BE_HUMAN']
        return status, reasons
    if key == 'P-4':
        if not isinstance(value, str) or not ENV_NAME.match(value) or 'SK-' in value:
            return 'malformed', ['ENVIRONMENT_VARIABLE_NAME_REQUIRED']
        return 'granted', []
    if key == 'P-5':
        if value is False or (isinstance(value, str) and _negative_text(value)):
            return 'denied', ['EXPLICIT_DENIAL']
        if not isinstance(value, dict) or set(value) != {'cap_usd', 'workspace_limit_configured'}:
            return 'malformed', ['CAP_OBJECT_REQUIRED']
        if not _is_number(value['cap_usd']) or not 0 < value['cap_usd'] <= 100:
            return 'malformed', ['CAP_USD_OUT_OF_RANGE']
        if value['workspace_limit_configured'] is not True:
            return ('denied', ['WORKSPACE_LIMIT_NOT_CONFIGURED']) if value['workspace_limit_configured'] is False else ('malformed', ['WORKSPACE_LIMIT_REQUIRES_LITERAL_TRUE'])
        return 'granted', []
    if key == 'P-6':
        if not isinstance(value, dict) or set(value) != {'timeout_seconds', 'max_retries', 'max_tokens'}:
            return 'malformed', ['LIMITS_OBJECT_REQUIRED']
        if not _is_number(value['timeout_seconds']) or not 0 < value['timeout_seconds'] <= 600:
            return 'malformed', ['TIMEOUT_OUT_OF_RANGE']
        if not isinstance(value['max_retries'], int) or isinstance(value['max_retries'], bool) or not 0 <= value['max_retries'] <= 3:
            return 'malformed', ['RETRIES_OUT_OF_RANGE']
        tokens = value['max_tokens']
        if not isinstance(tokens, dict) or not tokens or not all(isinstance(v, int) and not isinstance(v, bool) and 1024 <= v <= 32768 for v in tokens.values()):
            return 'malformed', ['MAX_TOKENS_OUT_OF_RANGE']
        return 'granted', []
    if key == 'P-7':
        if value is False or (isinstance(value, str) and _negative_text(value)):
            return 'denied', ['EXPLICIT_DENIAL']
        if not isinstance(value, dict) or set(value) != {'allowed_data_classes'}:
            return 'malformed', ['DATA_CLASS_OBJECT_REQUIRED']
        classes = value['allowed_data_classes']
        if not isinstance(classes, list) or not classes or len(set(classes)) != len(classes) or not set(classes) <= DATA_CLASSES:
            return 'malformed', ['DATA_CLASSES_UNKNOWN_OR_EMPTY']
        return 'granted', []
    if key == 'split':
        if value is False or (isinstance(value, str) and _negative_text(value)):
            return 'denied', ['EXPLICIT_DENIAL']
        if not isinstance(value, dict) or set(value) != {'confirmed', 'development', 'validation', 'holdout'}:
            return 'malformed', ['SPLIT_OBJECT_REQUIRED']
        if value['confirmed'] is not True:
            return ('denied', ['SPLIT_NOT_CONFIRMED']) if (value['confirmed'] is False or _negative_text(value['confirmed'])) else ('malformed', ['CONFIRMATION_REQUIRES_LITERAL_TRUE'])
        groups = [value['development'], value['validation'], value['holdout']]
        if not all(g in GROUPS for g in groups) or len(set(groups)) != 3:
            return 'malformed', ['SPLIT_GROUPS_UNKNOWN_OR_CONTRADICTORY']
        return 'granted', []
    if key == SCOPE_KEY:
        return _scope(value, decisions)
    return 'malformed', ['UNKNOWN_DECISION']


def _scope(value, decisions):
    """Bounded inference authorization: either one reviewed plan (standing=false, plan_sha256 set) or a standing scope for
    routine development-data inference until `expires_on`, always inside P-2 models, P-7 data classes, the P-5 cap and the
    confirmed development group."""
    if value is False or (isinstance(value, str) and _negative_text(value)):
        return 'denied', ['EXPLICIT_DENIAL']
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        return 'malformed', ['SCOPE_OBJECT_REQUIRED']
    get = lambda k: (decisions.get(k) or {}).get('value')
    split, models, p7, p5 = get('split'), get('P-2'), get('P-7'), get('P-5')
    reasons = []
    if not isinstance(split, dict) or value['development_group'] != split.get('development'):
        reasons.append('SCOPE_NOT_DEVELOPMENT_GROUP')
    if not isinstance(value['models'], list) or not value['models'] or not isinstance(models, list) or not set(value['models']) <= set(models):
        reasons.append('SCOPE_MODELS_NOT_APPROVED')
    if not isinstance(value['data_classes'], list) or not value['data_classes'] or not isinstance(p7, dict) or \
            not set(value['data_classes']) <= set(p7.get('allowed_data_classes') or []):
        reasons.append('SCOPE_DATA_CLASS_NOT_APPROVED')
    cap = p5.get('cap_usd') if isinstance(p5, dict) else None
    if not _is_number(value['max_spend_usd']) or value['max_spend_usd'] <= 0 or cap is None or value['max_spend_usd'] > cap:
        reasons.append('SCOPE_SPEND_OUT_OF_RANGE')
    if value['standing'] is not True and value['standing'] is not False:
        reasons.append('STANDING_REQUIRES_BOOLEAN')
    plan = value['plan_sha256']
    if value['standing'] is False and not (isinstance(plan, str) and re.fullmatch(r'[0-9a-f]{64}', plan)):
        reasons.append('PLAN_HASH_REQUIRED')
    if value['standing'] is True and plan is not None:
        reasons.append('STANDING_SCOPE_HAS_NO_PLAN_HASH')
    try:
        date.fromisoformat(value['expires_on'])
    except (TypeError, ValueError):
        reasons.append('EXPIRY_DATE_REQUIRED')
    return ('malformed', reasons) if reasons else ('granted', [])


def _agent_like(text):
    t = str(text).casefold().replace('-', ' ').replace('_', ' ')
    return bool(AGENT_IDENTITY.search(t)) or t.strip() in {'ai', 'model', 'system'}


def _valid_date(text, today):
    try:
        day = date.fromisoformat(text) if isinstance(text, str) and len(text) == 10 else None
    except ValueError:
        return False
    return day is not None and day <= today


class OwnerDecisionGate:
    def __init__(self, path=None, today=None):
        self.today = today or date.today()
        self.decisions, self.file_errors = {}, []
        if not path or not Path(path).is_file():
            return
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_no_duplicates)
        except (ValueError, _Duplicate):
            self.file_errors.append('UNREADABLE_OR_DUPLICATE_KEYS')
            return
        if not isinstance(data, dict) or not set(data) <= {'schema', 'note', 'decisions'} or data.get('schema') != SCHEMA \
                or not isinstance(data.get('decisions'), dict):
            self.file_errors.append('UNEXPECTED_FILE_STRUCTURE')
            return
        unknown = sorted(set(data['decisions']) - KNOWN)
        if unknown:
            self.file_errors.append('UNKNOWN_DECISION_KEYS:' + ','.join(unknown))
        self.decisions = data['decisions']

    def _entry(self, key):
        entry = self.decisions.get(key)
        if entry is None or (isinstance(entry, dict) and entry.get('value') is None):
            return 'missing', ['NOT_RECORDED']
        if not isinstance(entry, dict) or not set(entry) <= ENTRY_KEYS:
            return 'malformed', ['UNEXPECTED_ENTRY_FIELDS']
        by, on = entry.get('recorded_by'), entry.get('recorded_on')
        if not isinstance(by, str) or len(by.strip()) < 2 or _negative_text(by):
            return 'malformed', ['DECISION_MAKER_REQUIRED']
        if _agent_like(by):
            return 'malformed', ['AGENT_CANNOT_RECORD_OWNER_DECISIONS']
        if not _valid_date(on, self.today):
            return 'malformed', ['VALID_NON_FUTURE_DATE_REQUIRED']
        return _validate(key, entry['value'], self.decisions)

    def evaluate(self, stage):
        if stage not in ('L0', 'L1'):
            raise EngineError('UNKNOWN_STAGE', 'Unknown authorization stage %r.' % stage)
        required = L0_REQUIRED if stage == 'L0' else L1_REQUIRED
        if self.file_errors:
            return {k: {'status': 'malformed', 'reasons': list(self.file_errors)} for k in required}
        out = {}
        for key in required:
            status, reasons = self._entry(key)
            out[key] = {'status': status, 'reasons': reasons}
        return out

    def missing(self, stage):
        """Every required decision that is not affirmatively granted (missing, denied or malformed)."""
        return [k for k, v in self.evaluate(stage).items() if v['status'] != 'granted']

    def require_live(self):
        """Live (L1) calls need every L1 decision whatever the data origin: no label, `synthetic` included, opens them."""
        result = self.evaluate('L1')
        blocked = ['%s (%s)' % (k, v['status']) for k, v in result.items() if v['status'] != 'granted']
        if blocked:
            raise EngineError('OWNER_DECISIONS_MISSING', 'Live provider request blocked; L1 decisions not affirmatively granted: %s.'
                              % ', '.join(blocked))

    def require(self, stage, data_origin):
        """Only proven-synthetic data is ungated; everything else (sponsor, unverified, unknown) needs every decision.
        Offline (L0) use only for synthetic data; live calls go through require_live_authorization()."""
        if data_origin == 'synthetic':
            return
        result = self.evaluate(stage)
        blocked = ['%s (%s)' % (k, v['status']) for k, v in result.items() if v['status'] != 'granted']
        if blocked:
            raise EngineError('OWNER_DECISIONS_MISSING', 'Sponsor-data %s blocked; decisions not affirmatively granted: %s.'
                              % (stage, ', '.join(blocked)))


def inference_scope(gate):
    """The validated `inference_scope_approved` value, or raise. Requires every L1 decision too, and an unexpired scope."""
    OwnerDecisionGate.require_live(gate) if type(gate) is OwnerDecisionGate else require_live_authorization(gate)
    status, reasons = gate._entry(SCOPE_KEY)
    if status != 'granted':
        raise EngineError('INFERENCE_SCOPE_NOT_APPROVED', 'Broader inference needs an owner-recorded inference_scope_approved (%s: %s).'
                          % (status, ', '.join(reasons)))
    value = gate.decisions[SCOPE_KEY]['value']
    if date.fromisoformat(value['expires_on']) < gate.today:
        raise EngineError('INFERENCE_SCOPE_EXPIRED', 'The recorded inference scope expired on %s.' % value['expires_on'])
    return value


def require_live_authorization(gate):
    """Explicit L1 authorization for ANY live provider request, checked before credentials, budget or transport.

    Only a genuine OwnerDecisionGate counts: None, duck-typed stand-ins and subclasses (which could override the check)
    are refused, and the check is called on the base class so it cannot be replaced per instance.
    """
    if type(gate) is not OwnerDecisionGate:
        raise EngineError('OWNER_DECISIONS_MISSING', 'Live provider request blocked; no owner-decision gate supplied.')
    OwnerDecisionGate.require_live(gate)


# Request content task -> the P-7 data class it carries. A body whose task is not listed has an undeclared data class.
TASK_DATA_CLASS = {'classify_point_signatures': 'raw_bas_point_metadata'}


def provider_binding_violations(gate, provider_name, settings, ledger_cap_usd, data_class):
    """Differences between the ACTUAL live provider configuration and the granted owner decisions ([] when bound).

    P-1 provider, P-2 model, P-4 credential variable name, P-5 cap (the ledger may be smaller, never larger), P-6
    timeout / retries / max_tokens (each may be stricter, never looser) and P-7 data class.
    """
    value = {k: (gate.decisions.get(k) or {}).get('value') for k in ('P-1', 'P-2', 'P-4', 'P-5', 'P-6', 'P-7')}
    out = []
    if value['P-1'] != provider_name:
        out.append('P-1: provider %r is not the approved provider' % provider_name)
    if not isinstance(value['P-2'], list) or settings.model not in value['P-2']:
        out.append('P-2: model %r is not approved' % settings.model)
    if settings.api_key_env != value['P-4']:
        out.append('P-4: credential variable %r differs from the approved name' % settings.api_key_env)
    cap = (value['P-5'] or {}).get('cap_usd') if isinstance(value['P-5'], dict) else None
    if cap is None or ledger_cap_usd is None or ledger_cap_usd > cap + 1e-12:
        out.append('P-5: ledger cap %r exceeds the approved cap %r' % (ledger_cap_usd, cap))
    limits = value['P-6'] if isinstance(value['P-6'], dict) else {}
    tokens = (limits.get('max_tokens') or {}).get(settings.model)
    if tokens is None or settings.max_tokens > tokens:
        out.append('P-6: max_tokens %d exceeds the approved %r for %s' % (settings.max_tokens, tokens, settings.model))
    if limits.get('timeout_seconds') is None or settings.timeout_seconds > limits['timeout_seconds']:
        out.append('P-6: timeout %r exceeds the approved %r' % (settings.timeout_seconds, limits.get('timeout_seconds')))
    if limits.get('max_retries') is None or settings.max_retries > limits['max_retries']:
        out.append('P-6: max_retries %r exceeds the approved %r' % (settings.max_retries, limits.get('max_retries')))
    allowed = (value['P-7'] or {}).get('allowed_data_classes') if isinstance(value['P-7'], dict) else None
    if not data_class or not isinstance(allowed, list) or data_class not in allowed:
        out.append('P-7: data class %r is not approved' % (data_class,))
    return out


def require_provider_binding(gate, provider_name, settings, ledger_cap_usd, data_class):
    require_live_authorization(gate)
    violations = provider_binding_violations(gate, provider_name, settings, ledger_cap_usd, data_class)
    if violations:
        raise EngineError('PROVIDER_CONFIG_NOT_APPROVED', 'Live request blocked; configuration exceeds owner decisions: '
                          + '; '.join(violations) + '.')
