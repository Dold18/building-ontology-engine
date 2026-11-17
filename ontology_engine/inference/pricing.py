"""Per-model pricing configuration and conservative cost bounds. Prices are operator data, not code."""
import json
from pathlib import Path
from typing import Dict, Optional
from pydantic import Field
from ..models import Model
from ..errors import EngineError

DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / 'config' / 'inference' / 'model_pricing.json'


class ModelPrice(Model):
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    long_prompt_threshold_tokens: Optional[int] = Field(default=None, ge=1)
    long_input_usd_per_mtok: Optional[float] = Field(default=None, ge=0)
    long_output_usd_per_mtok: Optional[float] = Field(default=None, ge=0)


class PricingTable(Model):
    version: str
    currency: str
    source_note: str
    models: Dict[str, ModelPrice]

    def price(self, model):
        if model not in self.models:
            raise EngineError('PRICING_MISSING', 'No operator-configured pricing for model %r; no request sent.' % model)
        return self.models[model]


def load_pricing(path=None):
    return PricingTable.model_validate_json(Path(path or DEFAULT_PRICING_PATH).read_text(encoding='utf-8'))


def _rates(price, prompt_tokens):
    if price.long_prompt_threshold_tokens is not None and prompt_tokens > price.long_prompt_threshold_tokens:
        return (price.long_input_usd_per_mtok if price.long_input_usd_per_mtok is not None else price.input_usd_per_mtok,
                price.long_output_usd_per_mtok if price.long_output_usd_per_mtok is not None else price.output_usd_per_mtok)
    return price.input_usd_per_mtok, price.output_usd_per_mtok


def max_cost_usd(price, input_tokens_upper, max_output_tokens):
    """Upper bound used for reservation: the long-prompt tier applies when the input could exceed its threshold."""
    rate_in, rate_out = _rates(price, input_tokens_upper)
    return (input_tokens_upper * rate_in + max_output_tokens * rate_out) / 1_000_000


def usage_cost_usd(price, usage):
    """Actual cost from provider usage. Cache tokens are priced conservatively at the full input rate
    (cache write at 1.25x) because per-model cache prices are not configured; the Phase 2 design does not use caching."""
    base = int(usage.get('input_tokens') or 0)
    written = int(usage.get('cache_creation_input_tokens') or 0)
    read = int(usage.get('cache_read_input_tokens') or 0)
    out = int(usage.get('output_tokens') or 0)
    prompt = base + written + read
    rate_in, rate_out = _rates(price, prompt)
    return ((base + read + 1.25 * written) * rate_in + out * rate_out) / 1_000_000
