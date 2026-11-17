"""Owner-reviewed, cost-bounded browser preflight for generic CSV/XLSX inference.

A preflight is a proposal, not an authorization. It binds exact uploaded bytes,
config, model, building scope and operator-priced conservative budget. No credentials
are accessed and no network request is performed here.

The sponsor-specific Anthropic pipeline has separate gates and is deliberately
not run through this generic browser approval mechanism.
"""
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .config import InputConfig
from .limits import Limits

MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
MAX_BYTES = 300_000
MAX_OUTPUT = 4096


def _number(value, label, maximum):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("PREFLIGHT_" + label)
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("PREFLIGHT_" + label) from None
    if not result.is_finite() or result <= 0 or result > Decimal(str(maximum)):
        raise ValueError("PREFLIGHT_" + label)
    return result


def _source_digest(directory):
    directory = Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("INPUT_MISSING")
    entries = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.is_symlink() or not path.is_file():
            raise ValueError("INPUT_INVALID")
        if path.name == ".input-config.json":
            # The config is also bound, separately.
            continue
        if path.suffix.lower() not in (".csv", ".xlsx") or not path.name or len(entries) >= 10:
            raise ValueError("INPUT_INVALID")
        data = path.read_bytes()
        if not data or len(data) > 2_000_000:
            raise ValueError("INPUT_LIMIT")
        entries.append({"name": path.name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    if not entries or sum(e["bytes"] for e in entries) > 8_000_000:
        raise ValueError("INPUT_LIMIT")
    return entries


def preflight(upload_root, input_id, body):
    if not isinstance(body, dict) or set(body) != {"provider", "model", "building_id", "input_rate",
                                                   "output_rate", "max_cost_usd", "input_id"}:
        raise ValueError("PREFLIGHT_SCHEMA")
    if body["provider"] != "openai":
        raise ValueError("PROVIDER_NOT_SUPPORTED_IN_BROWSER")
    model, building = body["model"], body["building_id"]
    if not isinstance(model, str) or not MODEL.fullmatch(model):
        raise ValueError("MODEL_INVALID")
    if not isinstance(building, str) or not building.strip() or len(building) > 200:
        raise ValueError("BUILDING_ID_INVALID")
    if body["input_id"] != input_id:
        raise ValueError("INPUT_MISMATCH")
    directory = Path(upload_root) / input_id
    files = _source_digest(directory)
    config_bytes = (directory / ".input-config.json").read_bytes()
    config = InputConfig.model_validate_json(config_bytes)
    # The quote uses the maximum number of input bytes a single request can
    # consume under the engine Limits, not an invented observed token estimate.
    input_rate = _number(body["input_rate"], "INPUT_RATE", 1000)
    output_rate = _number(body["output_rate"], "OUTPUT_RATE", 1000)
    cost_cap = _number(body["max_cost_usd"], "MAX_COST", 100)
    bound = (Decimal(MAX_BYTES) * input_rate + Decimal(MAX_OUTPUT) * output_rate) / Decimal(1_000_000)
    if cost_cap < bound:
        raise ValueError("COST_CAP_BELOW_CONSERVATIVE_BOUND")
    limits = Limits(max_requests=1, max_cost_usd=float(cost_cap),
                    input_usd_per_million=float(input_rate),
                    output_usd_per_million=float(output_rate),
                    max_input_tokens=MAX_BYTES, max_context_bytes=MAX_BYTES,
                    max_total_input_tokens=MAX_BYTES,
                    max_output_tokens=MAX_OUTPUT,
                    max_total_output_tokens=MAX_OUTPUT)
    settings = {
        "input_id": input_id, "provider": "openai", "model": model,
        "building_id": building.strip(), "files": files,
        "input_config_sha256": hashlib.sha256(config_bytes).hexdigest(),
        "inference_mode": config.inference_mode,
        "pricing": {"input_usd_per_million": str(input_rate),
                    "output_usd_per_million": str(output_rate),
                    "operator_supplied": True,
                    "verified_with_provider": False},
        "cost_cap_usd": str(cost_cap),
        "conservative_upper_bound_usd": str(bound),
        "limits": limits.model_dump(),
    }
    plan_hash = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"plan_hash": plan_hash, "settings": settings,
            "warning": "Operator-supplied prices are not verified. Actual model availability, provider pricing and downstream costs must be checked independently.",
            "method": "One request; conservative maximum 300000 UTF-8 request bytes and 4096 output tokens. No automatic retries.",
            "external_processing": "Raw building data and derived context may be transmitted to OpenAI after explicit approval."}


def verify_unchanged(upload_root, plan):
    """Reject changes to any uploaded input, mapping, model or pricing after preview."""
    settings = plan["settings"]
    current = _source_digest(Path(upload_root) / settings["input_id"])
    if current != settings["files"]:
        raise ValueError("SOURCE_CHANGED_AFTER_PREFLIGHT")
    config_path = Path(upload_root) / settings["input_id"] / ".input-config.json"
    if hashlib.sha256(config_path.read_bytes()).hexdigest() != settings["input_config_sha256"]:
        raise ValueError("CONFIG_CHANGED_AFTER_PREFLIGHT")
    expected = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if expected != plan["plan_hash"]:
        raise ValueError("PREFLIGHT_MISMATCH")
    return Limits.model_validate(settings["limits"])
