"""Governing versions of the inference subsystem, bound into the durable execution fingerprint (owner-gate correction 4).

Any change here (prompt text, response schema, model profiles, API version, cache/unit/drawing/gate versions, pricing
config or routing defaults) makes earlier durable checkpoints incompatible, so resume is refused instead of silently
mixing outputs produced under different governing inputs.
"""
import hashlib
import json
from pathlib import Path


def _sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def governing_versions():
    from ..contextual import ContextResponse
    from . import anthropic_provider, cache, drawings, gate, router, units
    from .pricing import DEFAULT_PRICING_PATH
    pricing = Path(DEFAULT_PRICING_PATH)
    defaults = {name: f.default for name, f in router.RoutingConfig.model_fields.items() if name != 'scope'}
    return {'router_policy': router.POLICY_VERSION, 'router_prompt_version': router.PROMPT_VERSION,
            'system_prompt_sha256': _sha(router.SYSTEM_PROMPT), 'response_schema_sha256': anthropic_provider.schema_sha256(ContextResponse),
            'api_version': anthropic_provider.DEFAULT_API_VERSION, 'model_profiles': anthropic_provider.MODEL_PROFILES,
            'cache_version': cache.CACHE_VERSION, 'unit_rule': units.RULE_ID, 'drawing_method': drawings.METHOD_VERSION,
            'decision_schema': gate.SCHEMA, 'pricing_config_sha256': _sha(pricing.read_bytes()) if pricing.is_file() else 'missing',
            'routing_defaults_sha256': _sha(json.dumps(defaults, sort_keys=True, default=str))}
