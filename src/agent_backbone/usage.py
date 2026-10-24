"""Usage metadata and arithmetic, independent of runtimes and persistence."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TOKEN_FIELDS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")


def usage_id(runtime: str, session_id: str) -> str:
    return hashlib.sha256(f"{runtime}\0{session_id}".encode()).hexdigest()[:24]


def timestamp(value: object) -> str:
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, UTC)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("usage timestamp requires timezone")
    return dt.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class UsageEvent(BaseModel):
    """Disjoint token buckets; reasoning is a subset of output, never added twice."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300)
    at: str
    revision_at: str | None = None
    model: str = Field(default="unknown", max_length=200)
    provider: str = Field(default="unknown", max_length=200)
    reported_cost_usd: Decimal | None = Field(default=None, ge=0)
    turn_id: str | None = Field(default=None, max_length=300)
    input_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)
    cache_duration_known: bool = True
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    context_tokens: int | None = Field(default=None, ge=0)
    service_tier: str = "unknown"
    coverage: Literal["measured", "partial"] = "measured"

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, key) for key in TOKEN_FIELDS)


class Price(BaseModel):
    """USD per million tokens; a versioned scenario, not a subscription bill."""

    model_config = ConfigDict(extra="forbid")
    as_of: str
    source: str
    input: Decimal = Field(ge=0)
    cache_read: Decimal = Field(ge=0)
    cache_write: Decimal = Field(ge=0)
    cache_write_1h: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    long_context_above: int | None = Field(default=None, ge=1)
    long_input_multiplier: Decimal = Field(default=Decimal(1), ge=1)
    long_output_multiplier: Decimal = Field(default=Decimal(1), ge=1)


def estimate(event: UsageEvent, prices: dict) -> dict:
    """Price known observations at the selected catalog's standard API rate."""
    raw = prices.get(event.model)
    if raw is None or event.coverage != "measured":
        return {"usd": None, "reason": "unknown model price or incomplete usage", "basis": None}
    price = Price.model_validate(raw)
    if (
        event.cache_write_tokens
        and not event.cache_duration_known
        and price.cache_write != price.cache_write_1h
    ):
        return {"usd": None, "reason": "cache write duration unavailable", "basis": None}
    if price.long_context_above and event.context_tokens is None:
        return {"usd": None, "reason": "request context size unavailable", "basis": None}
    long = price.long_context_above and event.context_tokens > price.long_context_above
    im = price.long_input_multiplier if long else Decimal(1)
    om = price.long_output_multiplier if long else Decimal(1)
    one_hour = min(event.cache_write_1h_tokens, event.cache_write_tokens)
    amount = (
        im
        * (
            event.input_tokens * price.input
            + event.cache_read_tokens * price.cache_read
            + (event.cache_write_tokens - one_hour) * price.cache_write
            + one_hour * price.cache_write_1h
        )
        + om * event.output_tokens * price.output
    ) / 1_000_000
    return {
        "usd": str(amount),
        "reason": None,
        "basis": {
            **price.model_dump(mode="json"),
            "scenario": "standard API token rates",
            "observed_service_tier": event.service_tier,
            "excludes": (
                "subscription fees, tools, images, taxes and provider-specific discounts/surcharges"
            ),
        },
    }


# Published standard API rates checked 2026-09-12. These are explicit model IDs,
# never fuzzy aliases; users can replace the catalog through Backbone settings.
DEFAULT_PRICES: dict[str, dict] = {}
for _model, _rates in {
    "gpt-6-astra": (10, 1, 12.5, 50),
    "gpt-5.6-sol": (4, 0.4, 5, 20),
    "gpt-5.6-terra": (2, 0.2, 2.5, 12),
    "gpt-5.6-luna": (0.2, 0.02, 0.25, 1.2),
}.items():
    DEFAULT_PRICES[_model] = {
        "as_of": "2026-09-12",
        "source": "https://developers.openai.com/api/docs/pricing",
        "input": _rates[0],
        "cache_read": _rates[1],
        "cache_write": _rates[2],
        "cache_write_1h": _rates[2],
        "output": _rates[3],
        "long_context_above": 272000,
        "long_input_multiplier": 2,
        "long_output_multiplier": 1.5,
    }
for _model, _input, _output, _read in (
    ("claude-fable-5-1", 10, 50, 0.25),
    ("claude-opus-5", 5, 25, 0.5),
    ("claude-opus-4-8", 5, 25, 0.5),
    ("claude-opus-4-7", 5, 25, 0.5),
    ("claude-opus-4-6", 5, 25, 0.5),
    ("claude-sonnet-5", 2, 10, 0.2),
    ("claude-sonnet-4-6", 3, 15, 0.3),
    ("claude-haiku-4-5", 1, 5, 0.1),
):
    DEFAULT_PRICES[_model] = {
        "as_of": "2026-09-12",
        "source": "https://platform.claude.com/docs/en/about-claude/pricing",
        "input": _input,
        "cache_read": _read,
        "cache_write": _input * 1.25,
        "cache_write_1h": _input * 2,
        "output": _output,
    }
