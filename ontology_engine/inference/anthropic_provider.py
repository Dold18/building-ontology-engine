"""Minimal Anthropic Messages API provider.

Design choices (see docs/TASK_006_PHASE2_REPORT.md):
- Same stdlib-HTTP style and injectable transport as the existing OpenAI provider; no new runtime dependency and no
  change to pinned packages. Swapping `transport` for an SDK-backed callable is a one-function change.
- Structured output via output_config.format (json_schema) is a convenience: every response is still parsed with the
  existing strict pydantic models and then passed through the deterministic scope audit. The provider never produces RDF.
- Live calls are OFF unless `live_enabled=True`; Phase 2 never sets it. No sampling parameters are sent.
- Even with `live_enabled=True`, every request needs explicit L1 owner authorization through a real OwnerDecisionGate,
  checked first; the caller's data_origin label never waives it.
- The actual configuration is then bound to the granted decisions (provider, model, credential variable name, ledger cap,
  timeout/retry/max_tokens limits and the declared P-7 data class), and the body must match it, all before any credential
  read, budget reservation or transport call.
- Credentials are read from the environment at call time only and never stored, logged or placed in audit records.
- Model allow-list: Haiku 5.5 (economical) and Sonnet 5.5 (bounded escalation). Opus is not allowed.
NOTE: Wire details come from the claude-api documentation snapshot; they have NOT been exercised against the live API.
"""
import functools
import hashlib
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from pydantic import Field
from ..errors import EngineError
from ..models import Model
from .budget import BudgetLedger
from .gate import TASK_DATA_CLASS, require_live_authorization, require_provider_binding
from .provenance import SponsorIndex, trusted_body_origin
from .pricing import PricingTable, max_cost_usd, usage_cost_usd

PROVIDER = 'anthropic'
API_URL = 'https://api.anthropic.com/v1/messages'
DEFAULT_API_VERSION = '2026-10-21'
TOKEN_OVERHEAD = 256  # framing/special-token margin added to the byte-based input upper bound
MODEL_PROFILES = {
    # thinking omitted == provider default; Haiku 5.5 documents thinking "disabled" (low/medium effort only), Sonnet 5.5
    # rejects "disabled", so it keeps adaptive thinking at medium effort; thinking tokens are billed inside max_tokens.
    'claude-haiku-5-5': {'thinking': {'type': 'disabled'}, 'effort': 'low', 'role': 'economical'},
    'claude-sonnet-5-5': {'thinking': None, 'effort': 'medium', 'role': 'escalation'},
}
RETRYABLE_STATUS = {429, 503, 529}   # explicit capacity rejections: the request was not processed
SAFE_PERMANENT_STATUS = {400, 401, 403, 404, 413, 422}


class AnthropicSettings(Model):
    model: str
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    effort: Optional[str] = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_retries: int = Field(default=2, ge=0, le=3)
    api_version: str = DEFAULT_API_VERSION
    api_key_env: str = 'ANTHROPIC_API_KEY'
    max_input_tokens_per_request: int = Field(default=60_000, ge=1)
    max_cost_per_request_usd: float = Field(default=0.25, ge=0)

    def profile(self):
        if self.model not in MODEL_PROFILES:
            raise EngineError('MODEL_NOT_ALLOWED', 'Model %r is not in the Phase 2 allow-list (Haiku 5.5, Sonnet 5.5).' % self.model)
        return MODEL_PROFILES[self.model]


@dataclass
class TransportResponse:
    status: int
    headers: Dict[str, str]
    body: bytes


class TransportError(Exception):
    """Transport failure. `sent` is False only when no request bytes could have reached the provider."""
    def __init__(self, kind, sent):
        super().__init__(kind)
        self.kind, self.sent = kind, sent


def urllib_transport(url, headers, body, timeout):
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return TransportResponse(response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(8_000_000))
    except urllib.error.HTTPError as exc:
        return TransportResponse(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(65_536))
    except urllib.error.URLError as exc:
        pre_send = isinstance(exc.reason, (socket.gaierror, ConnectionRefusedError))
        raise TransportError(type(exc.reason).__name__, sent=not pre_send) from None
    except (TimeoutError, socket.timeout):
        raise TransportError('timeout', sent=True) from None
    except OSError as exc:
        raise TransportError(type(exc).__name__, sent=True) from None


def flatten_schema(model_class):
    """Inline $ref/$defs, require every property, forbid extras, drop defaults/titles (strict structured-output shape)."""
    return json.loads(_flattened_json(model_class))


@functools.lru_cache(maxsize=16)
def _flattened_json(model_class):
    schema = model_class.model_json_schema()
    defs = schema.pop('$defs', {})

    def walk(node):
        if isinstance(node, dict):
            if '$ref' in node:
                return walk(defs[node['$ref'].rsplit('/', 1)[-1]])
            out = {k: ({n: walk(v) for n, v in node[k].items()} if k == 'properties' else walk(v))
                   for k, v in node.items() if k not in ('title', 'default', 'description')}
            if out.get('type') == 'object':
                out['additionalProperties'] = False
                out['required'] = list(out.get('properties', {}))
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return json.dumps(walk(schema))   # insertion order kept; schema_sha256 sorts keys itself


def schema_sha256(model_class):
    return hashlib.sha256(json.dumps(flatten_schema(model_class), sort_keys=True).encode()).hexdigest()


def build_request(settings, system_prompt, content, response_type):
    profile = settings.profile()
    body = {'model': settings.model, 'max_tokens': settings.max_tokens,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': json.dumps(content, ensure_ascii=False, sort_keys=True)}],
            'output_config': {'effort': settings.effort or profile['effort'],
                              'format': {'type': 'json_schema', 'schema': flatten_schema(response_type)}}}
    if profile['thinking'] is not None:
        body['thinking'] = profile['thinking']
    return body


def input_upper_bound_tokens(body):
    """Conservative: one token per UTF-8 byte of the serialized body (tokens never exceed bytes) plus framing margin."""
    return len(json.dumps(body, ensure_ascii=False).encode('utf-8')) + TOKEN_OVERHEAD


def request_key(body, scope=''):
    return hashlib.sha256(json.dumps([scope, body], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def redact_text(text, environ=None):
    environ = os.environ if environ is None else environ
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN'):
        secret = environ.get(name)
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'sk-ant-[A-Za-z0-9_\-]+', '[REDACTED]', text)


@dataclass
class ProviderResult:
    parsed: Any
    usage: Dict[str, Any]
    cost_usd: Optional[float]
    audit: Dict[str, Any] = field(default_factory=dict)


class AnthropicProvider:
    name = PROVIDER

    def __init__(self, settings, pricing, budget, transport=urllib_transport, live_enabled=False, sleep=time.sleep, environ=None, gate=None,
                 sponsor_index=None, data_class=None):
        self.settings, self.pricing, self.budget, self.gate = settings, pricing, budget, gate
        self.data_class = data_class   # the P-7 data class this provider is used for; None is never approved
        self.sponsor_index = sponsor_index or SponsorIndex()
        self.transport, self.live_enabled, self.sleep = transport, live_enabled, sleep
        self.environ = os.environ if environ is None else environ
        self.model = settings.model
        settings.profile()

    # -- public ----------------------------------------------------------------------------------
    def complete(self, body, response_type, scope='', data_origin='synthetic'):
        """Send one request under the budget ledger and return a schema-validated, never-trusted response.

        Raises EngineError with a stable code; after an ambiguous outcome there is NO automatic resend.
        """
        if not self.live_enabled:
            raise EngineError('PERMISSION_DENIED', 'Live provider calls are disabled in this configuration (offline preflight).')
        # Every live request needs explicit L1 authorization (all L0 decisions + P-10 + l0_payload_approved) BEFORE any
        # credential read, budget reservation or transport call. No caller-provided data_origin, `synthetic` included,
        # bypasses it. Sponsor provenance is still derived from the body itself and checked again on the trusted origin.
        require_live_authorization(self.gate)
        require_provider_binding(self.gate, PROVIDER, self.settings, self.budget.cap_usd, self.data_class)
        self._require_body_matches(body)
        origin = trusted_body_origin(json.dumps(body, ensure_ascii=False), data_origin, self.sponsor_index)
        self.gate.require('L1', origin)
        price = self.pricing.price(self.settings.model)
        upper = input_upper_bound_tokens(body)
        if upper > self.settings.max_input_tokens_per_request:
            raise EngineError('BUDGET_EXCEEDED', 'Request exceeds per-request input token bound; not sent.')
        reserved = max_cost_usd(price, upper, body['max_tokens'])
        if reserved > self.settings.max_cost_per_request_usd:
            raise EngineError('BUDGET_EXCEEDED', 'Request maximum cost %.6f exceeds the per-request bound; not sent.' % reserved)
        key = getattr(self.budget, 'effective_key', lambda k: k)(request_key(body, scope))
        key_value = self.environ.get(self.settings.api_key_env)
        if not key_value:
            raise EngineError('PERMISSION_DENIED', 'Set %s in the environment (never in files).' % self.settings.api_key_env)
        self.budget.reserve(key, self.settings.model, reserved)
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
        audit = {'provider': PROVIDER, 'model': self.settings.model, 'request_key': key,
                 'request_sha256': hashlib.sha256(payload).hexdigest(), 'api_version': self.settings.api_version,
                 'reserved_max_usd': round(reserved, 9), 'trusted_data_origin': origin, 'attempts': 0}
        headers = {'content-type': 'application/json', 'anthropic-version': self.settings.api_version, 'x-api-key': key_value}
        self.budget.mark_sending(key)
        response = None
        for attempt in range(self.settings.max_retries + 1):
            audit['attempts'] = attempt + 1
            try:
                response = self.transport(API_URL, headers, payload, self.settings.timeout_seconds)
            except TransportError as exc:
                if not exc.sent:
                    self.budget.release(key, 'not sent: ' + exc.kind)
                    raise EngineError('PROVIDER_UNREACHABLE', 'Request was never sent (%s); nothing billed.' % exc.kind) from None
                self.budget.mark_ambiguous(key, 'transport: ' + exc.kind)
                raise EngineError('EXTERNAL_OUTCOME_UNKNOWN', 'Request outcome unknown (%s); reservation kept, no automatic resend.' % exc.kind) from None
            except Exception as exc:  # noqa: BLE001 - anything unexpected after SENDING may or may not have reached the provider
                self.budget.mark_ambiguous(key, 'unexpected transport failure: ' + type(exc).__name__)
                raise EngineError('EXTERNAL_OUTCOME_UNKNOWN', 'Unexpected transport failure; outcome unknown, no automatic resend.') from None
            if response.status in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                self.sleep(min(30.0, self._retry_after(response, attempt)))
                continue
            break
        status = response.status
        if status in RETRYABLE_STATUS or status in SAFE_PERMANENT_STATUS:
            self.budget.release(key, 'provider rejected HTTP %d' % status)
            raise EngineError('PROVIDER_HTTP_ERROR', 'Provider rejected the request (HTTP %d); not processed, nothing billed.' % status)
        if status != 200:
            self.budget.mark_ambiguous(key, 'unexpected HTTP %d' % status)
            raise EngineError('EXTERNAL_OUTCOME_UNKNOWN', 'Unexpected HTTP %d; billing outcome unknown, no automatic resend.' % status)
        return self._settle(key, price, response, response_type, audit)

    # -- internals -------------------------------------------------------------------------------
    def _require_body_matches(self, body):
        """The body must be built for these bound settings and carry only the declared data class."""
        try:
            task = json.loads(body['messages'][0]['content']).get('task')
            ok = body.get('model') == self.settings.model and int(body.get('max_tokens', 0)) <= self.settings.max_tokens
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            task, ok = None, False
        if not ok or TASK_DATA_CLASS.get(task) != self.data_class:
            raise EngineError('PROVIDER_CONFIG_NOT_APPROVED', 'Request body does not match the approved model, token limit or data class; not sent.')

    @staticmethod
    def _retry_after(response, attempt):
        try:
            return float(response.headers.get('retry-after', ''))
        except ValueError:
            return 1.0 * (2 ** attempt)

    def _settle(self, key, price, response, response_type, audit):
        audit['response_sha256'] = hashlib.sha256(response.body).hexdigest()
        audit['request_id'] = response.headers.get('request-id')
        try:
            message = json.loads(response.body.decode('utf-8'))
            usage = message['usage']
            cost = usage_cost_usd(price, usage)
        except (ValueError, KeyError, TypeError, UnicodeError):
            # A 200 we cannot read may still be billed: keep the full reservation as the settled cost.
            self.budget.settle(key, self.budget.db.execute('SELECT reserved_usd FROM requests WHERE request_key=?', (key,)).fetchone()[0],
                               {'unreadable_response': True})
            raise EngineError('SCHEMA_INVALID', 'Response body unreadable; settled at reserved maximum; no replay.') from None
        self.budget.settle(key, cost, usage)
        audit.update({'usage': {k: usage.get(k) for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')},
                      'cost_usd': round(cost, 9), 'stop_reason': message.get('stop_reason')})
        if message.get('stop_reason') == 'refusal':
            raise EngineError('PROVIDER_REFUSAL', 'Model refused; billed and settled; not retried.')
        if message.get('stop_reason') != 'end_turn':
            raise EngineError('SCHEMA_INVALID', 'Stop reason %r (truncated or unexpected); billed and settled; not replayed.' % message.get('stop_reason'))
        try:
            text = next(b['text'] for b in message['content'] if b.get('type') == 'text')
            parsed = response_type.model_validate_json(text)
        except (StopIteration, KeyError, TypeError, ValueError):
            raise EngineError('SCHEMA_INVALID', 'Response failed strict schema validation; billed and settled; not replayed.') from None
        return ProviderResult(parsed=parsed, usage=audit['usage'], cost_usd=cost, audit=audit)


def _make_body(self, system_prompt, content, response_type):
    return build_request(self.settings, system_prompt, content, response_type)


AnthropicProvider.make_body = _make_body
