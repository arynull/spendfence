"""Token → US dollar conversion.

``DEFAULT_PRICING`` below holds **sane defaults**, not a live price feed — this
tool never touches the network, so it cannot know today's prices. Treat the
numbers as a starting point and correct them with::

    spendfence models set <model> <input_per_1m> <output_per_1m>

Entries written to ``pricing.json`` override the matching defaults; new models
may be added the same way. A model that resolves to no price at all raises
:class:`~spendfence.errors.PricingError` naming the model — silently charging
zero would under-report spend, which is the one failure a spend fence must not
have.

All math is :class:`decimal.Decimal` and the result is quantized to six decimal
places (half-up), so the same tokens + same table always produce the same cents.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .errors import PricingError

PRICE_FIELDS = (
    "input_per_1m",
    "output_per_1m",
    "cache_read_per_1m",
    "cache_write_per_1m",
)

#: Six decimal places — well below a tenth of a cent, well above noise.
COST_QUANT = Decimal("0.000001")

#: Prices are quoted per this many tokens.
TOKENS_PER_UNIT = Decimal(1_000_000)

DEFAULT_MODEL = "unknown"

_FILE_MODE = 0o600

# --------------------------------------------------------------------------
# Built-in defaults (USD per 1M tokens). User-overridable via `models set`.
#
# Cache columns follow the vendors' published cache pricing where it exists:
# Anthropic-style tables read cached input at ~0.1x input and charge ~1.25x
# input to write it; OpenAI-style tables read cached input at ~0.5x input and
# charge input price to write; vendors without cache pricing reuse the input
# price so the math stays conservative (over-reporting beats under-reporting).
# --------------------------------------------------------------------------
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4": {
        "input_per_1m": 15.00,
        "output_per_1m": 75.00,
        "cache_read_per_1m": 1.50,
        "cache_write_per_1m": 18.75,
    },
    "claude-sonnet-4": {
        "input_per_1m": 3.00,
        "output_per_1m": 15.00,
        "cache_read_per_1m": 0.30,
        "cache_write_per_1m": 3.75,
    },
    "claude-haiku-4": {
        "input_per_1m": 1.00,
        "output_per_1m": 5.00,
        "cache_read_per_1m": 0.10,
        "cache_write_per_1m": 1.25,
    },
    "gpt-4o": {
        "input_per_1m": 2.50,
        "output_per_1m": 10.00,
        "cache_read_per_1m": 1.25,
        "cache_write_per_1m": 2.50,
    },
    "gpt-4o-mini": {
        "input_per_1m": 0.15,
        "output_per_1m": 0.60,
        "cache_read_per_1m": 0.075,
        "cache_write_per_1m": 0.15,
    },
    "o1": {
        "input_per_1m": 15.00,
        "output_per_1m": 60.00,
        "cache_read_per_1m": 7.50,
        "cache_write_per_1m": 15.00,
    },
    "o3-mini": {
        "input_per_1m": 1.10,
        "output_per_1m": 4.40,
        "cache_read_per_1m": 0.55,
        "cache_write_per_1m": 1.10,
    },
    "gemini-2.0-flash": {
        "input_per_1m": 0.10,
        "output_per_1m": 0.40,
        "cache_read_per_1m": 0.025,
        "cache_write_per_1m": 0.10,
    },
    "deepseek-chat": {
        "input_per_1m": 0.27,
        "output_per_1m": 1.10,
        "cache_read_per_1m": 0.07,
        "cache_write_per_1m": 0.27,
    },
    "grok-2": {
        "input_per_1m": 2.00,
        "output_per_1m": 10.00,
        "cache_read_per_1m": 2.00,
        "cache_write_per_1m": 2.00,
    },
}

#: Model ids seen in the wild that mean one of the entries above. Resolution is
#: exact-match first, then this table, then date-suffix stripping — never a
#: prefix guess, because a wrong price is worse than a clear error.
MODEL_ALIASES: dict[str, str] = {
    "claude-3-opus-20240229": "claude-opus-4",
    "claude-3-5-sonnet-latest": "claude-sonnet-4",
    "claude-3-5-sonnet-20240620": "claude-sonnet-4",
    "claude-3-5-sonnet-20241022": "claude-sonnet-4",
    "claude-3-5-haiku-20241022": "claude-haiku-4",
}

#: Trailing release/date markers, e.g. "-20250514", "-2024-11-20".
_DATE_SUFFIX = re.compile(r"[-_](?:\d{8}|\d{6}|\d{4}-\d{2}-\d{2}|\d{4}_\d{2}_\d{2})$")

#: Vendor prefixes that appear on gateway/Bedrock-style model ids.
_VENDOR_PREFIXES = (
    "us.anthropic.",
    "eu.anthropic.",
    "apac.anthropic.",
    "anthropic.",
    "openai.",
    "google.",
    "meta.",
    "xai.",
)


def _to_decimal(value: object, *, what: str) -> Decimal:
    """Coerce a price/token value to :class:`Decimal` without float drift."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise PricingError(f"{what} must be a number, got {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str() first: Decimal(0.1) would carry the binary approximation.
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation:
            raise PricingError(f"{what} is not a number: {value!r}") from None
    raise PricingError(f"{what} must be a number, got {type(value).__name__}")


def default_table() -> dict[str, dict[str, Decimal]]:
    """A fresh copy of :data:`DEFAULT_PRICING` with Decimal prices."""
    return {
        model: {field: Decimal(str(price)) for field, price in row.items()}
        for model, row in DEFAULT_PRICING.items()
    }


def known_models(table: dict[str, dict[str, Decimal]] | None = None) -> list[str]:
    """Sorted model names present in ``table`` (defaults + overrides)."""
    table = default_table() if table is None else table
    return sorted(table)


def _read_pricing_entries(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, dict]:
    """Read just the model entries the user's pricing file defines.

    Returns ``{}`` for a missing or empty file — no defaults involved. Raises
    :class:`~spendfence.errors.PricingError` with a one-line message on a file
    that cannot be understood, rather than falling back to defaults and quietly
    metering at the wrong price.
    """
    from . import state

    pricing_file = Path(path) if path is not None else state.pricing_path()
    if not pricing_file.exists():
        return {}

    try:
        raw_text = pricing_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingError(
            f"cannot read pricing file {pricing_file}: {exc.strerror}"
        ) from None
    if not raw_text.strip():
        return {}

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PricingError(
            f"pricing file {pricing_file} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from None
    if not isinstance(payload, dict):
        raise PricingError(
            f"pricing file {pricing_file} must contain a JSON object of model names"
        )

    # Accept either a bare {model: prices} object or a wrapped {"models": {...}}.
    if isinstance(payload.get("models"), dict):
        payload = payload["models"]
    elif any(not isinstance(v, dict) for v in payload.values()) and payload:
        # Tolerate a {"version": 1, ...} style wrapper by ignoring scalar keys.
        payload = {k: v for k, v in payload.items() if isinstance(v, dict)}
    if not payload:
        return {}

    for model, row in payload.items():
        if not isinstance(row, dict):
            raise PricingError(
                f"pricing entry for {model!r} must be a JSON object of price fields"
            )
    return {str(model): row for model, row in payload.items()}


def user_overrides(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Decimal]]:
    """Only the models the user has explicitly priced, as Decimal values.

    Lets a caller tell "the shipped default" from "a number someone typed",
    which is the difference between quietly trusting a price and flagging it.
    """
    payload = _read_pricing_entries(path)
    overrides: dict[str, dict[str, Decimal]] = {}
    for model, row in payload.items():
        merged: dict[str, Decimal] = {}
        for field in PRICE_FIELDS:
            if field in row:
                merged[field] = _to_decimal(
                    row[field], what=f"pricing for {model}: {field}"
                )
        overrides[model] = merged
    return overrides


def load_pricing(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Decimal]]:
    """Merge the user's pricing file over :data:`DEFAULT_PRICING`.

    Missing file (or an empty one) means "defaults only". A model may override
    just the fields it cares about; the rest fall back to the default row.
    """
    table = default_table()
    payload = _read_pricing_entries(path)
    if not payload:
        return table

    for model, row in payload.items():
        # _read_pricing_entries has already checked that row is a dict.
        merged = dict(table.get(model, {field: Decimal(0) for field in PRICE_FIELDS}))
        for field in PRICE_FIELDS:
            if field not in row:
                continue
            price = _to_decimal(row[field], what=f"pricing for {model}: {field}")
            if price < 0:
                raise PricingError(
                    f"pricing for {model}: {field} cannot be negative ({price})"
                )
            merged[field] = price
        table[model] = merged
    return table


def save_pricing(
    path: str | os.PathLike[str], table: Mapping[str, Mapping[str, object]]
) -> None:
    """Write ``table`` to ``path`` as pretty JSON (mode 0o600, atomic replace).

    Writes exactly what it is given, so callers that pass only overrides keep
    future default updates flowing through.
    """
    target = Path(path)
    payload: dict[str, dict[str, float]] = {}
    for model, row in table.items():
        payload[str(model)] = {}
        for field, value in row.items():
            dec = _to_decimal(value, what=f"pricing for {model}: {field}")
            if dec < 0:
                raise PricingError(
                    f"pricing for {model}: {field} cannot be negative ({dec})"
                )
            payload[str(model)][str(field)] = float(dec)

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".pricing-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp_name, _FILE_MODE)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _normalise(model: str) -> str:
    """Lowercase, drop any gateway path prefix and vendor prefix."""
    name = model.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    for prefix in _VENDOR_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


def resolve_model(table: dict[str, dict[str, Decimal]], model: str) -> str | None:
    """Return the table key matching ``model``, or ``None`` if there is none.

    Exact (case-insensitive) match wins; then the alias table; then a release
    date suffix such as ``-20250514`` is stripped and retried. No prefix
    matching: two models can share a prefix and differ in price by 25x.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    candidates = [model.strip()]
    normalised = _normalise(model)
    if normalised != model.strip():
        candidates.append(normalised)

    lowered = {key.lower(): key for key in table}
    for candidate in candidates:
        hit = lowered.get(candidate.lower())
        if hit is not None:
            return hit

    for candidate in candidates:
        target = MODEL_ALIASES.get(candidate)
        if target is not None and target in table:
            return target

    for candidate in candidates:
        stripped = _DATE_SUFFIX.sub("", candidate)
        if stripped == candidate:
            continue
        hit = lowered.get(stripped)
        if hit is not None:
            return hit
        target = MODEL_ALIASES.get(stripped)
        if target is not None and target in table:
            return target
    return None


def cost_usd(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    table: dict[str, dict[str, Decimal]] | None = None,
) -> Decimal:
    """Dollar cost of one metered chunk, quantized to 6 decimals.

    Raises :class:`~spendfence.errors.PricingError` naming the model when no
    price is known for it.
    """
    table = load_pricing() if table is None else table
    key = resolve_model(table, model if isinstance(model, str) else "")
    if key is None:
        shown = model if isinstance(model, str) and model.strip() else DEFAULT_MODEL
        known = ", ".join(known_models(table)[:6])
        suffix = f" Known models include: {known}..." if known else ""
        raise PricingError(
            f"no price for model {shown!r}; set one with: spendfence models set {shown} <in_per_1m> <out_per_1m>{suffix}"
        )

    prices = table[key]
    total = Decimal(0)
    for field, count in (
        ("input_per_1m", input_tokens),
        ("output_per_1m", output_tokens),
        ("cache_read_per_1m", cache_read),
        ("cache_write_per_1m", cache_write),
    ):
        tokens = _to_decimal(count, what=f"token count for {key}")
        if tokens < 0:
            raise PricingError(f"token count for {key} cannot be negative ({tokens})")
        total += tokens * prices[field]
    return (total / TOKENS_PER_UNIT).quantize(COST_QUANT, rounding=ROUND_HALF_UP)


def format_usd(amount: Decimal | float | str, places: int = 2) -> str:
    """Render a dollar amount with a fixed number of places (no ``$``)."""
    value = _to_decimal(amount, what="amount")
    quant = Decimal(1).scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):f}"


__all__ = [
    "COST_QUANT",
    "DEFAULT_MODEL",
    "DEFAULT_PRICING",
    "MODEL_ALIASES",
    "PRICE_FIELDS",
    "TOKENS_PER_UNIT",
    "cost_usd",
    "default_table",
    "format_usd",
    "known_models",
    "load_pricing",
    "resolve_model",
    "save_pricing",
]
