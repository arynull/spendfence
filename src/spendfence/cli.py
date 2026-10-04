"""Command-line interface.

``main(argv)`` returns the process exit code instead of raising, so the CLI can
be driven directly from tests and embedded in scripts:

===== ===========================================================
Code  Meaning
===== ===========================================================
0     success (a budget warning still counts as success)
1     usage error or a domain failure
2     budget breached, or the kill switch is engaged (``check``)
3     the ledger's hash chain does not verify
===== ===========================================================

Every expected failure prints one plain-language line on stderr and returns 1 —
no traceback ever reaches a terminal. Output that a script might parse goes to
stdout; warnings and failures go to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import __version__, state
from . import budgets as budgets_mod
from . import ingest as ingest_mod
from . import ledger as ledger_mod
from . import pricing as pricing_mod
from .errors import PricingError, SpendfenceError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BREACH = 2
EXIT_TAMPER = 3

USAGE = "spendfence"

_BAR_WIDTH = 10

# --------------------------------------------------------------------------
# Task -> model-class advice (non-enforcing)
#
# Keywords are matched on word boundaries, so "pr" fires on "review this PR"
# without also firing on "prompt", "provider", or "printing".
# --------------------------------------------------------------------------

TASK_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "fast chat",
        (
            "summarize",
            "summarise",
            "summary",
            "review",
            "pr",
            "explain",
            "translate",
            "docs",
            "documentation",
            "comment",
            "comment",
            "lint",
            "format",
            "typo",
            "rename",
            "boilerplate",
            "commit",
            "message",
            "log",
            "readme",
            "todo",
        ),
    ),
    (
        "strong reasoning",
        (
            "refactor",
            "migrate",
            "debug",
            "architect",
            "architecture",
            "design",
            "performance",
            "concurrency",
            "race",
            "deadlock",
            "leak",
            "algorithm",
            "optimize",
            "optimise",
            "root",
            "cause",
            "security",
            "schema",
            "rewrite",
        ),
    ),
)

#: Models considered for each class, cheapest-blended first is computed live.
CLASS_MODELS: dict[str, tuple[str, ...]] = {
    "fast chat": (
        "gemini-2.0-flash",
        "gpt-4o-mini",
        "claude-haiku-4",
        "deepseek-chat",
        "grok-2",
    ),
    "balanced": ("o3-mini", "gpt-4o", "claude-sonnet-4"),
    "strong reasoning": ("claude-sonnet-4", "o1", "claude-opus-4"),
}

DEFAULT_CLASS = "balanced"

#: Weighting used for the "blended $/1M" figure: 3 parts input to 1 part output,
#: which is close to ordinary chat traffic. Documented so the number is checkable.
_BLEND_INPUT_WEIGHT = Decimal(3)
_BLEND_OUTPUT_WEIGHT = Decimal(1)


class _TamperDetected(Exception):
    """Internal: the ledger's hash chain does not verify."""

    def __init__(self, index: int) -> None:
        super().__init__(f"record {index + 1}")
        self.index = index


# --------------------------------------------------------------------------
# Small formatting / parsing helpers
# --------------------------------------------------------------------------


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _usd(amount: Decimal, places: int = 2) -> str:
    return pricing_mod.format_usd(amount, places)


def _parse_ts(value: object) -> datetime | None:
    """Parse a ledger timestamp into an aware UTC datetime, or ``None``.

    Accepts the ``...Z`` form written by ingest plus any ISO-8601 variant
    ``datetime.fromisoformat`` understands. Unparseable stamps are skipped
    rather than guessed at — a wrong clock skews the burn rate.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _day_of(record: dict) -> str | None:
    """The UTC calendar date (``YYYY-MM-DD``) a record belongs to."""
    stamp = _parse_ts(record.get("ts"))
    return stamp.strftime("%Y-%m-%d") if stamp else None


def _week_of(record: dict) -> str | None:
    """The UTC ISO week (``YYYY-Www``) a record belongs to."""
    stamp = _parse_ts(record.get("ts"))
    if stamp is None:
        return None
    iso = stamp.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _sum_cost(records) -> Decimal:
    total = Decimal(0)
    for record in records:
        total += ledger_mod.record_cost(record)
    return total


def _percent(spent: Decimal, cap: Decimal) -> Decimal:
    if cap <= 0:
        return Decimal(0)
    return (spent / cap) * Decimal(100)


def _bar(percent: Decimal, width: int = _BAR_WIDTH) -> str:
    """A fixed-width text gauge; overflow past 100% reads as a full bar."""
    if percent <= 0:
        filled = 0
    elif percent >= 100:
        filled = width
    else:
        filled = int((percent / 100) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _budget_label(budget: dict) -> str:
    key = budget.get("key") or ""
    return f"{budget['scope']}:{key}" if key else budget["scope"]


# --------------------------------------------------------------------------
# Ledger access
# --------------------------------------------------------------------------


def _read_ledger(verify: bool = True) -> list[dict]:
    """Load every ledger record, refusing to trust a tampered chain.

    Raises :class:`_TamperDetected` before any spending number is derived from
    the file, so a corrupted ledger can never produce a confident-looking
    total.
    """
    path = state.ledger_path()
    if verify:
        intact, index = ledger_mod.verify(path)
        if not intact:
            raise _TamperDetected(index)
    return ledger_mod.read_records(path)


# --------------------------------------------------------------------------
# Spend roll-ups
# --------------------------------------------------------------------------


def _applicable_budgets(
    configured: list[dict],
    *,
    session: str | None,
    project: str | None,
    today: str,
    this_week: str,
    records: list[dict],
) -> list[tuple[dict, Decimal]]:
    """Pair each budget that applies here with its spend.

    A budget is skipped when it cannot be evaluated: a session budget with no
    ``--session`` given, a day budget for a date other than today, or a week
    budget for a week other than this week (you cannot retroactively breach
    yesterday's cap).
    """
    pairs: list[tuple[dict, Decimal]] = []
    for budget in configured:
        scope = budget["scope"]
        key = budget["key"]
        if scope == "session":
            if not session or key != session:
                continue
            spend = _sum_cost(r for r in records if r.get("session") == session)
        elif scope == "project":
            if not project or key != project:
                continue
            spend = _sum_cost(r for r in records if r.get("project") == project)
        elif scope == "day":
            if key != today:
                continue
            spend = _sum_cost(r for r in records if _day_of(r) == today)
        elif scope == "week":
            if key != this_week:
                continue
            spend = _sum_cost(r for r in records if _week_of(r) == this_week)
        else:
            spend = _sum_cost(records)
        pairs.append((budget, spend))
    return pairs


def _burn_rate(records: list[dict]) -> Decimal:
    """Dollars per hour across a record set, from its own timestamps.

    Returns 0 when there are fewer than two timestamps — a single point has no
    elapsed time, and inventing a rate from it would be a guess.
    """
    stamps = sorted(
        stamp for stamp in (_parse_ts(r.get("ts")) for r in records) if stamp
    )
    if len(stamps) < 2:
        return Decimal(0)
    elapsed = (stamps[-1] - stamps[0]).total_seconds()
    if elapsed <= 0:
        return Decimal(0)
    hours = Decimal(str(elapsed)) / Decimal(3600)
    if hours <= 0:
        return Decimal(0)
    return (_sum_cost(records) / hours).quantize(pricing_mod.COST_QUANT)


def _projection(spent: Decimal, cap: Decimal, burn: Decimal) -> str:
    """``~3.2h at current burn`` — or ``n/a`` when there is no burn to project."""
    remaining = cap - spent
    if burn <= 0:
        return "n/a"
    if remaining <= 0:
        return "breached"
    hours = remaining / burn
    return f"~{hours.quantize(Decimal('0.1'))}h at current burn"


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_ingest(args: argparse.Namespace) -> int:
    ledger_file = state.ledger_path()
    intact, index = ledger_mod.verify(ledger_file)
    if not intact:
        raise _TamperDetected(index)

    errors: list[str] = []
    written, skipped = ingest_mod.ingest_file(
        args.logfile,
        session=args.session or ingest_mod.UNKNOWN_LABEL,
        project=args.project or ingest_mod.UNKNOWN_LABEL,
        shape=args.shape,
        field_map=args.field_map,
        ledger_path=ledger_file,
        errors_out=errors,
    )

    # Sum the records this run just appended (they are the tail of the ledger).
    records = ledger_mod.read_records(ledger_file)
    total = _sum_cost(records[-written:]) if written else Decimal(0)

    _out(f"ingested {written} records (${_usd(total, 6)}) from {args.logfile}")
    if skipped:
        _err(f"skipped {skipped} line(s) with no readable token usage")
        for message in errors[:3]:
            _err(f"  {message}")
    return EXIT_OK


def cmd_budget(args: argparse.Namespace) -> int:
    path = state.budgets_path()

    if args.budget_action == "set":
        budget = budgets_mod.set_budget(
            path, args.scope, args.key, args.cap, warn_pct=args.warn_pct
        )
        label = _budget_label(budget)
        _out(
            f"budget set: {label} cap ${_usd(budget['cap_usd'])} "
            f"(warn at {budget['warn_pct']}%)"
        )
        return EXIT_OK

    if args.budget_action == "remove":
        budgets_mod.remove_budget(path, args.scope, args.key)
        scope = budgets_mod.normalize_scope(args.scope)
        key = budgets_mod.normalize_key(scope, args.key)
        _out(f"budget removed: {scope}:{key}" if key else f"budget removed: {scope}")
        return EXIT_OK

    configured = budgets_mod.list_budgets(path)
    if not configured:
        _out("no budgets set — try: spendfence budget set --scope global --cap 25.00")
        return EXIT_OK
    _out(f"{'SCOPE':<9} {'KEY':<20} {'CAP':>10} {'WARN':>6}")
    for budget in configured:
        _out(
            f"{budget['scope']:<9} {budget['key'] or '-':<20} "
            f"{'$' + _usd(budget['cap_usd']):>10} {str(budget['warn_pct']) + '%':>6}"
        )
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    # Order matters: tamper -> STOP -> breach -> warn -> ok.
    records = _read_ledger(verify=True)

    if state.is_stopped():
        _err("STOPPED: kill switch engaged (spendfence resume to clear)")
        return EXIT_BREACH

    configured = budgets_mod.list_budgets(state.budgets_path())
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    iso = now.isocalendar()
    this_week = f"{iso.year}-W{iso.week:02d}"
    pairs = _applicable_budgets(
        configured,
        session=args.session,
        project=args.project,
        today=today,
        this_week=this_week,
        records=records,
    )

    breached: list[tuple[dict, Decimal]] = []
    warned: list[tuple[dict, Decimal, Decimal]] = []
    for budget, spend in pairs:
        cap = budget["cap_usd"]
        if spend >= cap:
            breached.append((budget, spend))
        elif _percent(spend, cap) >= Decimal(budget["warn_pct"]):
            warned.append((budget, spend, _percent(spend, cap)))

    if breached:
        for budget, spend in breached:
            _err(
                f"budget '{_budget_label(budget)}' ${_usd(budget['cap_usd'])} cap exceeded: "
                f"spent ${_usd(spend)}"
            )
        return EXIT_BREACH

    for budget, spend, percent in warned:
        _err(
            f"WARNING: budget '{_budget_label(budget)}' at {percent.quantize(Decimal(1))}% of cap "
            f"(spent ${_usd(spend)} of ${_usd(budget['cap_usd'])})"
        )

    if args.verbose:
        if pairs:
            for budget, spend in pairs:
                _out(
                    f"ok {_budget_label(budget)} ${_usd(spend)} / ${_usd(budget['cap_usd'])} "
                    f"{_bar(_percent(spend, budget['cap_usd']))}"
                )
        else:
            _out(
                "ok no budgets apply to this check (pass --session / --project, or set one)"
            )
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    records = _read_ledger(verify=True)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    iso = now.isocalendar()
    this_week = f"{iso.year}-W{iso.week:02d}"

    # status always shows the global picture, using defaults when nothing was named.
    session = args.session
    project = args.project
    configured = budgets_mod.list_budgets(state.budgets_path())
    pairs = _applicable_budgets(
        configured,
        session=session,
        project=project,
        today=today,
        this_week=this_week,
        records=records,
    )
    if not pairs and configured:
        _err("note: no configured budget applies here (pass --session / --project)")

    session_records = [r for r in records if session and r.get("session") == session]
    if session:
        burn = _burn_rate(session_records)
        burn_scope = f"session {session}"
    else:
        burn = _burn_rate(records)
        burn_scope = "all sessions"

    _out(f"spend as of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    _out("-" * 62)
    if pairs:
        for budget, spend in pairs:
            cap = budget["cap_usd"]
            percent = _percent(spend, cap)
            _out(
                f"{_budget_label(budget):<24} ${_usd(spend):>9} / ${_usd(cap):<9} "
                f"{_bar(percent)} {percent.quantize(Decimal(1)):>4}%"
            )
    else:
        _out(f"no caps tracked — total spend ${_usd(_sum_cost(records))}")

    _out("")
    _out(f"burn rate ({burn_scope}): ${_usd(burn)}/hr")
    _out("projected breach:")
    if not pairs:
        _out("  n/a (no cap tracked)")
    for budget, spend in pairs:
        projection = _projection(spend, budget["cap_usd"], burn)
        _out(f"  {_budget_label(budget):<24} {projection}")
    return EXIT_OK


def _parse_since(spec: str | None) -> datetime | None:
    """``24h`` / ``7d`` / ``30d`` -> the cutoff instant, or ``None`` for all time."""
    if not spec:
        return None
    match = re.fullmatch(r"(\d+)\s*([hd])", spec.strip().lower())
    if not match:
        raise SpendfenceError(f"--since must look like 24h, 7d, or 30d (got {spec!r})")
    amount = int(match.group(1))
    delta = timedelta(hours=amount) if match.group(2) == "h" else timedelta(days=amount)
    return datetime.now(timezone.utc) - delta


def cmd_report(args: argparse.Namespace) -> int:
    records = _read_ledger(verify=True)
    cutoff = _parse_since(args.since)

    if cutoff is not None:
        records = [
            r
            for r in records
            if (_parse_ts(r.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
            >= cutoff
        ]
    if args.project:
        records = [r for r in records if r.get("project") == args.project]

    by_session: dict[str, Decimal] = {}
    by_model: dict[str, Decimal] = {}
    by_day: dict[str, Decimal] = {}
    for record in records:
        cost = ledger_mod.record_cost(record)
        session = str(record.get("session") or "unknown")
        model = str(record.get("model") or pricing_mod.DEFAULT_MODEL)
        day = _day_of(record) or "unknown"
        by_session[session] = by_session.get(session, Decimal(0)) + cost
        by_model[model] = by_model.get(model, Decimal(0)) + cost
        by_day[day] = by_day.get(day, Decimal(0)) + cost

    total = _sum_cost(records)

    if args.format == "json":
        payload = {
            "total_usd": _round_float(total),
            "by_session": {
                k: _round_float(v)
                for k, v in sorted(by_session.items(), key=lambda kv: (-kv[1], kv[0]))
            },
            "by_model": {
                k: _round_float(v)
                for k, v in sorted(by_model.items(), key=lambda kv: (-kv[1], kv[0]))
            },
            "by_day": {k: _round_float(v) for k, v in sorted(by_day.items())},
        }
        _out(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK

    window = args.since or "all time"
    scope = f"project {args.project}" if args.project else "all projects"
    _out(f"spend report — {scope} — last {window}")
    _out(f"total: ${_usd(total, 6)}  across {len(records)} record(s)")

    _out("")
    _out("top sessions")
    _out(f"  {'SESSION':<28} {'SPEND':>12}")
    for name, spend in list(_sorted_desc(by_session).items())[:5]:
        _out(f"  {name:<28} {'$' + _usd(spend):>12}")
    if not by_session:
        _out("  (none)")

    _out("")
    _out("top models")
    _out(f"  {'MODEL':<28} {'SPEND':>12}")
    for name, spend in list(_sorted_desc(by_model).items())[:5]:
        _out(f"  {name:<28} {'$' + _usd(spend):>12}")
    if not by_model:
        _out("  (none)")

    _out("")
    _out("daily spend")
    if by_day:
        peak = max(by_day.values())
        for day, spend in sorted(by_day.items()):
            _out(f"  {day}  {_bar(_percent(spend, peak))} {'$' + _usd(spend):>10}")
    else:
        _out("  (none)")
    return EXIT_OK


def _sorted_desc(mapping: dict[str, Decimal]) -> dict[str, Decimal]:
    """Highest spend first; ties broken by name so output is reproducible."""
    return dict(sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0])))


def _round_float(amount: Decimal) -> float:
    return float(amount.quantize(pricing_mod.COST_QUANT))


def cmd_models(args: argparse.Namespace) -> int:
    table = pricing_mod.load_pricing()

    if args.models_action == "set":
        overrides = pricing_mod.user_overrides()
        known = pricing_mod.resolve_model(table, args.model)
        row: dict[str, Decimal] = {}

        row["input_per_1m"] = _positive_price(args.input_per_1m, "input price")
        row["output_per_1m"] = _positive_price(args.output_per_1m, "output price")

        assumed: list[str] = []
        if known is not None:
            # Only the named fields are written; the rest keep the default row,
            # so a future default update still applies.
            for field, flag in (
                ("cache_read_per_1m", args.cache_read),
                ("cache_write_per_1m", args.cache_write),
            ):
                if flag is not None:
                    row[field] = _positive_price(
                        flag, field.replace("_per_1m", "").replace("_", " ")
                    )
        else:
            # A brand-new model: cached tokens would otherwise price at $0.00 and
            # under-report spend, so assume cache costs the same as input.
            for field, flag in (
                ("cache_read_per_1m", args.cache_read),
                ("cache_write_per_1m", args.cache_write),
            ):
                if flag is not None:
                    row[field] = _positive_price(
                        flag, field.replace("_per_1m", "").replace("_", " ")
                    )
                else:
                    row[field] = row["input_per_1m"]
                    assumed.append(field.replace("_per_1m", "").replace("_", " "))

        overrides[args.model] = {**overrides.get(args.model, {}), **row}
        pricing_mod.save_pricing(state.pricing_path(), overrides)

        _out(
            f"pricing set: {args.model} in ${_usd(row['input_per_1m'])}/1M "
            f"out ${_usd(row['output_per_1m'])}/1M"
        )
        if assumed:
            _out(
                f"  assumed {' and '.join(assumed)} = input price (pass --cache-read / --cache-write to change)"
            )
        return EXIT_OK

    overrides = pricing_mod.user_overrides()
    _out(
        f"{'MODEL':<24} {'IN $/1M':>10} {'OUT $/1M':>10} {'CACHE R':>10} {'CACHE W':>10}"
    )
    for model in pricing_mod.known_models(table):
        row = table[model]
        cache_read = row["cache_read_per_1m"]
        cache_write = row["cache_write_per_1m"]
        default_read = Decimal(
            str(
                pricing_mod.DEFAULT_PRICING.get(model, {}).get(
                    "cache_read_per_1m", "-1"
                )
            )
        )
        default_write = Decimal(
            str(
                pricing_mod.DEFAULT_PRICING.get(model, {}).get(
                    "cache_write_per_1m", "-1"
                )
            )
        )
        mark = "*" if model in overrides else " "
        read_cell = f"{_usd(cache_read):>9}" if cache_read != default_read else " " * 10
        write_cell = (
            f"{_usd(cache_write):>9}" if cache_write != default_write else " " * 10
        )
        _out(
            f"{model + mark:<24} {_usd(row['input_per_1m']):>10} "
            f"{_usd(row['output_per_1m']):>10} {read_cell} {write_cell}"
        )
    _out("")
    _out(
        "* = locally set   (shipped prices are estimates — check them against your provider)"
    )
    return EXIT_OK


def _positive_price(value: str, what: str) -> Decimal:
    try:
        price = Decimal(str(value).strip())
    except InvalidOperation:
        raise PricingError(f"{what} must be a number, got {value!r}") from None
    if not price.is_finite() or price < 0:
        raise PricingError(f"{what} must be zero or more, got {value!r}")
    return price


def _task_class(task: str) -> str:
    """Map free text to a model class using word-boundary keyword matching."""
    tokens = set(re.findall(r"[a-z0-9_+-]+", task.lower()))
    for name, keywords in TASK_CLASSES:
        if tokens & set(keywords):
            return name
    return DEFAULT_CLASS


def _blended(row: dict) -> Decimal:
    return (
        row["input_per_1m"] * _BLEND_INPUT_WEIGHT
        + row["output_per_1m"] * _BLEND_OUTPUT_WEIGHT
    ) / (_BLEND_INPUT_WEIGHT + _BLEND_OUTPUT_WEIGHT)


def cmd_advise(args: argparse.Namespace) -> int:
    task = (args.task or "").strip()
    if not task:
        raise SpendfenceError(
            '--task needs a description, e.g. spendfence advise --task "summarize a PR"'
        )

    table = pricing_mod.load_pricing()
    model_class = _task_class(task)
    candidates = [m for m in CLASS_MODELS[model_class] if m in table]
    if not candidates:
        candidates = [m for m in pricing_mod.known_models(table)]
    best = min(candidates, key=lambda m: (_blended(table[m]), m))
    blended = _blended(table[best])

    _out(
        f'ADVICE ONLY — not enforced. Recommended: {best} (~${_usd(blended)}/1M blended) for "{task}"'
    )
    _out(f"  task class: {model_class}   blended = 3x input + 1x output per 1M tokens")
    _out(
        f"  set your own prices with: spendfence models set {best} <in_per_1m> <out_per_1m>"
    )
    return EXIT_OK


def cmd_stop(_args: argparse.Namespace) -> int:
    sentinel = state.stop_path()
    state.data_dir()
    sentinel.write_text("kill switch engaged\n", encoding="utf-8")
    _out("kill switch ENGAGED")
    return EXIT_OK


def cmd_resume(_args: argparse.Namespace) -> int:
    state.data_dir()
    sentinel = state.stop_path()
    if sentinel.exists():
        try:
            sentinel.unlink()
        except OSError as exc:
            raise SpendfenceError(
                f"could not remove {sentinel}: {exc.strerror}"
            ) from None
    _out("kill switch cleared")
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    ledger_file = state.ledger_path()
    intact, index = ledger_mod.verify(ledger_file)
    failures: list[dict] = []
    records: list[dict] = []
    if not intact:
        failures.append(
            {"record": index + 1, "reason": "hash chain broken at this record"}
        )
        try:
            records = ledger_mod.read_records(ledger_file)
        except OSError:
            records = []
    else:
        records = ledger_mod.read_records(ledger_file)
        for number, record in enumerate(records, start=1):
            reasons = ledger_mod.validate_record(record)
            if reasons:
                failures.append({"record": number, "reason": reasons[0]})
    head: str | None = None
    if records:
        last_hash = records[-1].get("hash")
        if isinstance(last_hash, str) and last_hash:
            head = last_hash
    is_json = getattr(args, "format", "human") == "json"
    if is_json:
        payload = {
            "failures": failures,
            "head": head,
            "intact": not failures,
            "records": len(records),
        }
        _out(json.dumps(payload, indent=2, sort_keys=True))
        if failures:
            _err(f"LEDGER TAMPER DETECTED at record {failures[0]['record']}")
        return EXIT_OK if not failures else EXIT_TAMPER
    if not failures:
        if not records:
            _out("ledger intact: 0 records")
        elif head is None:
            _out(f"ledger intact: {len(records)} records")
        else:
            _out(f"ledger intact: {len(records)} records, head {head[:8]}")
        return EXIT_OK
    _out(f"LEDGER TAMPER DETECTED at record {failures[0]['record']}")
    for failure in failures:
        _err(f"record {failure['record']}: {failure['reason']}")
    return EXIT_TAMPER


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    """argparse exits 2 on usage errors; this project reserves 2 for breaches."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.exit(
            EXIT_ERROR, f"{self.prog}: error: {message} (try '{self.prog} --help')\n"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=USAGE, description="Meter token spend and enforce dollar budgets, offline."
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version and exit"
    )
    subparsers = parser.add_subparsers(dest="command")

    # ingest
    ingest_parser = subparsers.add_parser(
        "ingest", help="meter a log file into the ledger"
    )
    ingest_parser.add_argument("logfile")
    ingest_parser.add_argument(
        "--session", default=None, help="label this session in the ledger"
    )
    ingest_parser.add_argument(
        "--project", default=None, help="label this project in the ledger"
    )
    ingest_parser.add_argument(
        "--shape", default="auto", choices=list(ingest_mod.SHAPES), help="log format"
    )
    ingest_parser.add_argument(
        "--field-map",
        "--map",
        dest="field_map",
        default=None,
        help="for --shape generic: input=usage.in,output=usage.out,model=model",
    )
    ingest_parser.set_defaults(func=cmd_ingest)

    # budget
    budget_parser = subparsers.add_parser("budget", help="manage spend caps")
    budget_actions = budget_parser.add_subparsers(dest="budget_action", required=True)

    set_parser = budget_actions.add_parser("set", help="create or replace a cap")
    set_parser.add_argument("--scope", required=True, choices=list(budgets_mod.SCOPES))
    set_parser.add_argument(
        "--key",
        default=None,
        help="session id, project name, YYYY-MM-DD, or YYYY-Www week (not for global)",
    )
    set_parser.add_argument("--cap", required=True, help="dollar cap, e.g. 25.00")
    set_parser.add_argument(
        "--warn-pct", type=int, default=budgets_mod.DEFAULT_WARN_PCT
    )
    set_parser.set_defaults(func=cmd_budget)

    list_parser = budget_actions.add_parser("list", help="show configured caps")
    list_parser.set_defaults(func=cmd_budget, scope=None, key=None)

    remove_parser = budget_actions.add_parser("remove", help="drop a cap")
    remove_parser.add_argument(
        "--scope", required=True, choices=list(budgets_mod.SCOPES)
    )
    remove_parser.add_argument("--key", default=None)
    remove_parser.set_defaults(func=cmd_budget)

    # check
    check_parser = subparsers.add_parser(
        "check", help="gate: exit 2 if a cap is breached"
    )
    check_parser.add_argument("--session", default=None)
    check_parser.add_argument("--project", default=None)
    check_parser.add_argument(
        "--verbose", action="store_true", help="print one ok line per budget"
    )
    check_parser.set_defaults(func=cmd_check)

    # status
    status_parser = subparsers.add_parser(
        "status", help="spend vs caps, burn rate, projection"
    )
    status_parser.add_argument("--session", default=None)
    status_parser.add_argument("--project", default=None)
    status_parser.set_defaults(func=cmd_status)

    # report
    report_parser = subparsers.add_parser("report", help="attribution tables")
    report_parser.add_argument("--project", default=None)
    report_parser.add_argument(
        "--since", default=None, help="24h, 7d, 30d (default: all time)"
    )
    report_parser.add_argument("--format", choices=("human", "json"), default="human")
    report_parser.set_defaults(func=cmd_report)

    # models
    models_parser = subparsers.add_parser("models", help="pricing table")
    models_actions = models_parser.add_subparsers(dest="models_action")
    models_set = models_actions.add_parser(
        "set", help="set a model's price per 1M tokens"
    )
    models_set.add_argument("model")
    models_set.add_argument("input_per_1m")
    models_set.add_argument("output_per_1m")
    models_set.add_argument("--cache-read", default=None, help="$/1M for cache reads")
    models_set.add_argument("--cache-write", default=None, help="$/1M for cache writes")
    models_set.set_defaults(func=cmd_models, models_action="set")
    models_parser.set_defaults(func=cmd_models, models_action="list")

    # advise
    advise_parser = subparsers.add_parser(
        "advise", help="suggest a cheaper model (advice only)"
    )
    advise_parser.add_argument("--task", default=None)
    advise_parser.set_defaults(func=cmd_advise)

    # stop / resume
    subparsers.add_parser("stop", help="engage the kill switch").set_defaults(
        func=cmd_stop
    )
    subparsers.add_parser("resume", help="clear the kill switch").set_defaults(
        func=cmd_resume
    )

    verify_parser = subparsers.add_parser("verify", help="verify ledger integrity")
    verify_parser.add_argument("--format", choices=("human", "json"), default="human")
    verify_parser.set_defaults(func=cmd_verify)

    return parser


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_code:
        # --help exits 0; usage errors exit 1 via _ArgumentParser.error.
        return int(exit_code.code or 0)

    if getattr(args, "version", False):
        _out(f"spendfence {__version__}")
        return EXIT_OK
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_ERROR

    try:
        return int(args.func(args))
    except _TamperDetected as tamper:
        _err(f"LEDGER TAMPER DETECTED at record {tamper.index + 1}")
        return EXIT_TAMPER
    except SpendfenceError as error:
        _err(f"error: {error}")
        return EXIT_ERROR
    except KeyboardInterrupt:
        _err("interrupted")
        return EXIT_ERROR
    except BrokenPipeError:
        return EXIT_ERROR
    except OSError as error:
        _err(f"error: {error.strerror or error}")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
