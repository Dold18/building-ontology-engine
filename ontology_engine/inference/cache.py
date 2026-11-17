"""Content-addressed inference cache. A key binds ALL governing inputs, so any change is a miss, never a stale hit.

Integrity (owner-gate correction 2):
- every entry binds its payload with `payload_sha256`; a missing, malformed or mismatched payload is rejected;
- a SHA-256 checksum only detects ACCIDENTAL change. Anyone who can write the cache can recompute it, so it is not
  authentication. When an HMAC key is supplied (from the environment, never stored in the cache), entries carry an
  HMAC-SHA256 over key, components and payload digests, and unauthenticated or mismatched entries are rejected as
  suspected tampering;
- even an authentic hit is only a candidate: callers re-run schema and scope validation, and cached model output can
  never raise evidence status.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path

CACHE_VERSION = 'inference-cache-v2'
KEY_ENV = 'ONTOLOGY_CACHE_HMAC_KEY'
ENTRY_FIELDS = {'cache_version', 'key', 'components_sha256', 'payload', 'payload_sha256', 'provenance', 'mac', 'authentication'}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def sha256_text(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def cache_components(evidence, context, model, prompt_version, schema_sha256, settings, provider, api_version, scope, policy_version):
    """Everything that governs an output. Evidence = the exact source facts sent; context = surrounding facts sent."""
    return {'cache_version': CACHE_VERSION, 'evidence': evidence, 'context': context, 'model': model,
            'prompt_version': prompt_version, 'schema_sha256': schema_sha256, 'settings': settings,
            'provider': provider, 'api_version': api_version, 'building_scope': scope, 'policy_version': policy_version}


def cache_key(components):
    return sha256_text(canonical(components))


def key_from_environment(environ=None):
    value = (os.environ if environ is None else environ).get(KEY_ENV)
    return value.encode('utf-8') if value else None


class InferenceCache:
    def __init__(self, directory, hmac_key=None, require_authentication=None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.hmac_key = hmac_key
        self.require_authentication = bool(hmac_key) if require_authentication is None else require_authentication
        if self.require_authentication and not hmac_key:
            raise ValueError('require_authentication needs an HMAC key')
        self.invalid, self.rejections = [], []

    def _path(self, key):
        return self.dir / (key + '.json')

    def _mac(self, key, components_sha, payload_sha, provenance):
        message = canonical({'key': key, 'components_sha256': components_sha, 'payload_sha256': payload_sha, 'provenance': provenance})
        return hmac.new(self.hmac_key, message.encode('utf-8'), hashlib.sha256).hexdigest()

    def _reject(self, key, reason, classification):
        self.invalid.append(key)
        self.rejections.append({'key': key, 'reason': reason, 'classification': classification})
        return None

    def get(self, components):
        key = cache_key(components)
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return self._reject(key, 'UNREADABLE_ENTRY', 'accidental_or_unauthenticated')
        if not isinstance(entry, dict) or not set(entry) <= ENTRY_FIELDS or 'payload' not in entry or 'payload_sha256' not in entry:
            return self._reject(key, 'MALFORMED_OR_MISSING_PAYLOAD', 'accidental_or_unauthenticated')
        if entry.get('cache_version') != CACHE_VERSION or entry.get('key') != key or entry.get('components_sha256') != sha256_text(canonical(components)):
            return self._reject(key, 'BINDING_MISMATCH', 'accidental_or_unauthenticated')
        if not isinstance(entry['payload'], dict) or entry['payload_sha256'] != sha256_text(canonical(entry['payload'])):
            return self._reject(key, 'PAYLOAD_DIGEST_MISMATCH', 'accidental_or_unauthenticated')
        if self.hmac_key:
            expected = self._mac(key, entry['components_sha256'], entry['payload_sha256'], entry.get('provenance'))
            if not isinstance(entry.get('mac'), str) or not hmac.compare_digest(entry['mac'], expected):
                return self._reject(key, 'MAC_MISSING_OR_INVALID', 'tamper_suspected')
        elif self.require_authentication:
            return self._reject(key, 'UNAUTHENTICATED_ENTRY', 'tamper_suspected')
        return entry

    def put(self, components, payload, provenance):
        key = cache_key(components)
        components_sha = sha256_text(canonical(components))
        payload_sha = sha256_text(canonical(payload))
        entry = {'cache_version': CACHE_VERSION, 'key': key, 'components_sha256': components_sha, 'payload': payload,
                 'payload_sha256': payload_sha, 'provenance': provenance,
                 'authentication': 'hmac-sha256' if self.hmac_key else 'checksum-only (detects accidental change; not authentication)'}
        if self.hmac_key:
            entry['mac'] = self._mac(key, components_sha, payload_sha, provenance)
        temporary = self._path(key).with_suffix('.tmp')
        temporary.write_text(canonical(entry), encoding='utf-8')
        temporary.chmod(0o600)
        os.replace(temporary, self._path(key))
        return key
