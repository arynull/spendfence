"""Budget definitions: per-session, per-project, per-day, per-week, and overall caps.

A budget is ``{scope, key, cap_usd, warn_pct}``:

* ``session`` / ``project`` / ``day`` / ``week`` — ``key`` identifies the session id, the
  project name, the calendar day (``YYYY-MM-DD``), or the ISO week (``YYYY-Www``),
  respectively.
* ``global`` — one machine-wide cap; ``key`` is ignored and stored as ``""``.

``warn_pct`` is where the CLI starts complaining on stderr (default 80). The cap
itself is the hard line: ``check`` exits 2 once spend reaches it.

Stored in ``budgets.json`` as ``{"version": 1, "budgets": [...]}``. ``cap_usd``
is written as a **string** ("25.00") so the cap survives a round-trip without
binary-float drift, and comes back out of :func:`list_budgets` as a
:class:`~decimal.Decimal` ready to compare against a ledger total.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from .errors import BudgetError

#: Scope names accepted by the CLI, in display order.
SCOPES = ("session", "project", "day", "week", "global")

#: Scopes whose ``key`` selects which bucket is being capped.
KEYED_SCOPES = ("session", "project", "day", "week")

DEFAULT_WARN_PCT = 80
MIN_WARN_PCT = 1
MAX_WARN_PCT_EXCLUSIVE = 100

_CENT = Decimal("0.01")
_FILE_MODE = 0o600
_DIR_MODE = 0o700
_VERSION = 1

#: Key stored for ``global`` budgets, which ignore any key the user passed.
GLOBAL_KEY = ""

_WEEK_RE = re.compile(r"^\d{4}-W(0[1-9]|[1-4][0-9]|5[0-3])$")


def _to_decimal(value: object, *, what: str) -> Decimal:
    if isinstance(value, bool):
        raise BudgetError(f"{what} must be a number, got {value!r}")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        try:
            dec = Decimal(value.strip())
        except InvalidOperation:
            raise BudgetError(f"{what} is not a number: {value!r}") from None
    else:
        raise BudgetError(f"{what} must be a number, got {type(value).__name__}")
    if not dec.is_finite():
        raise BudgetError(f"{what} must be a finite number, got {value!r}")
    return dec


def normalize_scope(scope: object) -> str:
    """Validate a scope name and return it lowercased."""
    if not isinstance(scope, str) or not scope.strip():
        raise BudgetError(f"scope is required; choose one of: {', '.join(SCOPES)}")
    cleaned = scope.strip().lower()
    if cleaned not in SCOPES:
        raise BudgetError(
            f"unknown scope {scope.strip()!r}; choose one of: {', '.join(SCOPES)}"
        )
    return cleaned


def _validate_week_key(key: object) -> str:
    if isinstance(key, str) and _WEEK_RE.match(key.strip()):
        return key.strip()
    raise BudgetError(f"a week budget needs an ISO week key like 2026-W41, got {key!r}")


def normalize_key(scope: str, key: object) -> str:
    """Return the stored key for ``scope``.

    Keyed scopes need a non-blank key; ``global`` discards whatever was passed
    so that one global cap cannot be duplicated under two different keys.
    """
    if scope == "global":
        return GLOBAL_KEY
    if scope == "week":
        return _validate_week_key(key)
    if key is None or (isinstance(key, str) and not key.strip()):
        raise BudgetError(
            f"a {scope} budget needs a key (the session id, project name, day as YYYY-MM-DD, or week as YYYY-Www); "
            "only the global scope can omit it"
        )
    return str(key).strip()


def _validate_cap(cap_usd: object) -> Decimal:
    dec = _to_decimal(cap_usd, what="cap")
    cents = dec.quantize(_CENT, rounding=ROUND_HALF_UP)
    # Quantize before validating: a sub-cent cap would otherwise be stored as
    # "0.00" and fail to re-read on the very next load.
    if cents <= 0:
        raise BudgetError(f"cap must be at least $0.01, got {dec:f}")
    return cents


def _validate_warn_pct(warn_pct: object) -> int:
    if isinstance(warn_pct, bool):
        raise BudgetError(
            f"warning percentage must be a number between {MIN_WARN_PCT} and {MAX_WARN_PCT_EXCLUSIVE - 1}, got {warn_pct!r}"
        )
    if isinstance(warn_pct, float) and not warn_pct.is_integer():
        raise BudgetError(f"warning percentage must be a whole number, got {warn_pct}")
    try:
        pct = int(warn_pct)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise BudgetError(
            f"warning percentage must be a whole number, got {warn_pct!r}"
        ) from None
    if pct < MIN_WARN_PCT or pct >= MAX_WARN_PCT_EXCLUSIVE:
        raise BudgetError(
            f"warning percentage must be between {MIN_WARN_PCT} and {MAX_WARN_PCT_EXCLUSIVE - 1}, got {pct}"
        )
    return pct


def _load(path: str | os.PathLike[str]) -> list[dict]:
    """Read budgets from disk, returning ``[]`` for a missing/empty file."""
    target = Path(path)
    if not target.exists():
        return []
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetError(
            f"cannot read budgets file {target}: {exc.strerror}"
        ) from None
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BudgetError(
            f"budgets file {target} is not valid JSON: {exc.msg} (line {exc.lineno})"
        ) from None

    rows: object
    if isinstance(payload, dict):
        rows = payload.get("budgets", [])
    elif isinstance(payload, list):
        rows = payload
    else:
        raise BudgetError(
            f"budgets file {target} must contain a JSON object with a 'budgets' list"
        )
    if not isinstance(rows, list):
        raise BudgetError(
            f"budgets file {target} has a 'budgets' entry that is not a list"
        )

    parsed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BudgetError(
                f"budgets file {target} contains an entry that is not a JSON object"
            )
        scope = normalize_scope(row.get("scope"))
        key = normalize_key(scope, row.get("key", GLOBAL_KEY))
        parsed.append(
            {
                "scope": scope,
                "key": key,
                "cap_usd": _validate_cap(row.get("cap_usd")),
                "warn_pct": _validate_warn_pct(row.get("warn_pct", DEFAULT_WARN_PCT)),
            }
        )
    return parsed


def _sort_key(budget: dict) -> tuple:
    order = {scope: index for index, scope in enumerate(SCOPES)}
    return (order.get(budget["scope"], len(SCOPES)), budget["key"])


def _save(path: str | os.PathLike[str], budgets: list[dict]) -> None:
    """Write budgets atomically with mode 0o600."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    payload = {
        "version": _VERSION,
        "budgets": [
            {
                "scope": budget["scope"],
                "key": budget["key"],
                "cap_usd": f"{budget['cap_usd']:.2f}",
                "warn_pct": int(budget["warn_pct"]),
            }
            for budget in sorted(budgets, key=_sort_key)
        ],
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".budgets-", suffix=".tmp"
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


def set_budget(
    path: str | os.PathLike[str],
    scope: str,
    key: str | None,
    cap_usd: Decimal | float | str,
    warn_pct: int = DEFAULT_WARN_PCT,
) -> dict:
    """Create or replace the budget for ``(scope, key)``.

    Validation happens before anything is written, so a rejected value leaves
    the budgets file untouched. Returns the stored budget.
    """
    clean_scope = normalize_scope(scope)
    clean_key = normalize_key(clean_scope, key)
    cap = _validate_cap(cap_usd)
    warn = _validate_warn_pct(warn_pct)

    budgets = [
        b
        for b in _load(path)
        if not (b["scope"] == clean_scope and b["key"] == clean_key)
    ]
    budget = {"scope": clean_scope, "key": clean_key, "cap_usd": cap, "warn_pct": warn}
    budgets.append(budget)
    _save(path, budgets)
    return dict(budget)


def remove_budget(path: str | os.PathLike[str], scope: str, key: str | None) -> bool:
    """Remove the budget for ``(scope, key)``.

    Raises :class:`~spendfence.errors.BudgetError` when no such budget exists —
    a silent no-op here would look like the cap was lifted when it was not.
    """
    clean_scope = normalize_scope(scope)
    clean_key = normalize_key(clean_scope, key)
    budgets = _load(path)
    remaining = [
        b for b in budgets if not (b["scope"] == clean_scope and b["key"] == clean_key)
    ]
    if len(remaining) == len(budgets):
        label = clean_key or clean_scope
        raise BudgetError(
            f"no {clean_scope} budget found for {label!r}; nothing to remove"
        )
    _save(path, remaining)
    return True


def list_budgets(path: str | os.PathLike[str]) -> list[dict]:
    """All configured budgets, ordered by scope then key.

    Each entry is ``{"scope", "key", "cap_usd" (Decimal), "warn_pct" (int)}``.
    """
    return [dict(budget) for budget in sorted(_load(path), key=_sort_key)]


def find_budget(budgets: list[dict], scope: str, key: str | None = None) -> dict | None:
    """Look up one budget in an already-listed set, or ``None``.

    Callers that want the normalized key should pass the same ``key`` they
    would have stored (``None`` for ``global``).
    """
    try:
        clean_scope = normalize_scope(scope)
        clean_key = normalize_key(clean_scope, key)
    except BudgetError:
        return None
    for budget in budgets:
        if budget["scope"] == clean_scope and budget["key"] == clean_key:
            return dict(budget)
    return None


__all__ = [
    "DEFAULT_WARN_PCT",
    "GLOBAL_KEY",
    "KEYED_SCOPES",
    "MAX_WARN_PCT_EXCLUSIVE",
    "MIN_WARN_PCT",
    "SCOPES",
    "find_budget",
    "list_budgets",
    "normalize_key",
    "normalize_scope",
    "remove_budget",
    "set_budget",
]
