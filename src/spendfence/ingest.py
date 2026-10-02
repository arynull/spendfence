"""Parse agent session logs and append metered records to the ledger.

Three shapes are understood out of the box — ``claude-code`` and ``codex`` are
sniffed from the line's own keys, and ``generic`` is driven by a caller-supplied
field map. Nothing is guessed: if the first :data:`DETECT_WINDOW` non-blank lines
match no known shape, ingestion stops with an error naming the field it was
looking for rather than writing plausible-looking zeroes.

Records that cannot be interpreted (a truncated JSON line, a usage block with no
token counts) are counted as skipped and reported, not fatal. Cost is computed
per record at ingest time from the pricing table as it stands *now*, and stored
in the record next to the token counts — every dollar figure in a report is
therefore traceable to the line of the log that produced it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from .errors import IngestError
from .ledger import LedgerWriter
from .pricing import DEFAULT_MODEL, cost_usd, load_pricing

#: How many non-blank lines ``shape="auto"`` examines before giving up.
DETECT_WINDOW = 50

#: Shape names accepted by ``ingest_file``.
SHAPES = ("auto", "claude-code", "codex", "generic")

#: Default session/project labels when the caller does not name them.
UNKNOWN_LABEL = "unknown"

#: Text used in errors so the user learns which field was missing.
EXPECTED_FIELDS = (
    "message.usage.input_tokens (Claude Code shape) or usage.input_tokens (Codex shape)"
)

_Record = dict[str, object]

#: A parser takes an already-decoded JSON object and returns a record or None.
_Parser = Callable[[object], _Record | None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loads(line: object) -> dict | None:
    """Coerce a JSONL line (or an already-parsed object) to a dict.

    Returns ``None`` for anything unusable — blank lines, non-JSON text, JSON
    scalars and arrays — so callers can treat "not a record" uniformly.
    """
    if isinstance(line, dict):
        return line
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _dig(payload: object, path: str) -> object:
    """Follow a dotted path such as ``"message.usage.input_tokens"``."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _as_int(value: object) -> int | None:
    """Coerce a JSON token count to ``int``; ``None`` when it is not a count."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _count(record: _Record, name: str) -> int:
    """Read a token count off a parsed record, treating junk as zero."""
    count = _as_int(record.get(name))
    return 0 if count is None else count


def _model_of(record: _Record) -> str:
    model = record.get("model")
    return model if isinstance(model, str) and model.strip() else DEFAULT_MODEL


def _pick_ts(payload: dict) -> str | None:
    for path in ("timestamp", "ts", "time", "created_at"):
        value = _dig(payload, path)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_usage(payload: dict, prefix: str) -> bool:
    usage = _dig(payload, prefix)
    return isinstance(usage, dict) and (
        _as_int(usage.get("input_tokens")) is not None
        or _as_int(usage.get("output_tokens")) is not None
    )


def _build(
    model: object,
    input_tokens: int | None,
    output_tokens: int | None,
    cache_read: int | None = None,
    cache_write: int | None = None,
    ts: str | None = None,
) -> _Record | None:
    """Assemble a parsed record, or ``None`` if it carries no token counts."""
    if input_tokens is None and output_tokens is None:
        return None
    record: _Record = {
        "model": model if isinstance(model, str) and model.strip() else DEFAULT_MODEL,
        "input_tokens": input_tokens or 0,
        "output_tokens": output_tokens or 0,
        "cache_read": cache_read or 0,
        "cache_write": cache_write or 0,
    }
    if ts:
        record["ts"] = ts
    return record


def parse_claude_code(line: object) -> _Record | None:
    """Parse a Claude-Code-style line, or return ``None``.

    Expects ``message.usage`` with ``input_tokens``/``output_tokens`` plus the
    optional ``cache_creation_input_tokens``/``cache_read_input_tokens``. The
    model comes from ``message.model``, falling back to a top-level ``model``
    and finally to ``"unknown"``.
    """
    payload = _loads(line)
    if payload is None:
        return None
    usage = _dig(payload, "message.usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None

    model = _dig(payload, "message.model") or payload.get("model")
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_write = _as_int(usage.get("cache_creation_input_tokens"))
    return _build(
        model, input_tokens, output_tokens, cache_read, cache_write, _pick_ts(payload)
    )


def parse_codex(line: object) -> _Record | None:
    """Parse a Codex-style line, or return ``None``.

    Expects a top-level ``usage`` with ``input_tokens``/``output_tokens`` (plus
    the optional ``cached_input_tokens``) and a top-level ``model``.
    """
    payload = _loads(line)
    if payload is None:
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    input_tokens = _as_int(usage.get("input_tokens"))
    output_tokens = _as_int(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    cache_read = _as_int(usage.get("cached_input_tokens"))
    return _build(
        payload.get("model"),
        input_tokens,
        output_tokens,
        cache_read,
        None,
        _pick_ts(payload),
    )


def parse_generic(line: object, field_map: dict[str, str]) -> _Record | None:
    """Parse a line using a caller-supplied dot-path ``field_map``.

    Recognised keys: ``input``, ``output`` (required-ish — at least one must
    resolve), ``model``, ``cache_read``, ``cache_write``. Missing paths read as
    zero rather than failing, so a minimal map of ``input``/``output`` works.
    """
    if not field_map:
        raise IngestError(
            "generic format needs a field map, e.g. --map input=usage.in,output=usage.out"
        )
    payload = _loads(line)
    if payload is None:
        return None

    input_tokens: int | None = None
    output_tokens: int | None = None
    for target, paths in (("input", ("input",)), ("output", ("output",))):
        value = None
        for key in paths:
            path = field_map.get(key)
            if path:
                value = _dig(payload, path)
                break
        count = _as_int(value)
        if target == "input":
            input_tokens = count
        else:
            output_tokens = count
    if input_tokens is None and output_tokens is None:
        return None

    def optional(name: str) -> int | None:
        path = field_map.get(name)
        return _as_int(_dig(payload, path)) if path else None

    model_path = field_map.get("model")
    model = _dig(payload, model_path) if model_path else None
    return _build(
        model,
        input_tokens,
        output_tokens,
        optional("cache_read"),
        optional("cache_write"),
        _pick_ts(payload),
    )


def detect_shape(line: object) -> str | None:
    """Identify which known shape ``line`` belongs to, or ``None``.

    Detection is by key presence only: ``message.usage`` means Claude Code, a
    bare top-level ``usage`` means Codex. Returns ``None`` when neither matches
    so the caller can keep sniffing or report.
    """
    payload = _loads(line)
    if payload is None:
        return None
    if _has_usage(payload, "message.usage"):
        return "claude-code"
    if _has_usage(payload, "usage"):
        return "codex"
    return None


def _parse_field_map(spec: str | dict | None) -> dict[str, str]:
    """Normalize ``--map`` input into ``{logical: dot.path}``."""
    if not spec:
        return {}
    if isinstance(spec, dict):
        return {str(k): str(v) for k, v in spec.items()}
    mapping: dict[str, str] = {}
    for chunk in str(spec).split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise IngestError(
                f"field map entry {item!r} is not in key=path.path form (e.g. input=usage.in)"
            )
        key, _, path = item.partition("=")
        key = key.strip()
        path = path.strip()
        if not key or not path:
            raise IngestError(f"field map entry {item!r} is missing a key or a path")
        mapping[key] = path
    if not mapping:
        raise IngestError(
            "field map is empty; expected entries like input=usage.in,output=usage.out"
        )
    return mapping


def _unknown_shape_error(path: Path, sample: object = None) -> IngestError:
    detail = ""
    if isinstance(sample, dict) and sample:
        keys = ", ".join(sorted(str(key) for key in list(sample)[:6]))
        detail = f" Top-level keys seen: {keys}."
    return IngestError(
        f"could not read the log format of {path}: no line in the first {DETECT_WINDOW} lines carried "
        f"token usage. Expected {EXPECTED_FIELDS}. Pass --format generic with --map key=dotted.path "
        f"for any other log shape.{detail}"
    )


def iter_log_lines(path: str | os.PathLike[str]) -> Iterator[str]:
    """Stream a log file line by line (never loads the whole file)."""
    target = Path(path)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            yield from handle
    except FileNotFoundError:
        raise IngestError(f"log file not found: {target}") from None
    except IsADirectoryError:
        raise IngestError(f"not a file: {target}") from None
    except OSError as exc:
        raise IngestError(f"cannot read log file {target}: {exc.strerror}") from None


def ingest_file(
    logfile: str | os.PathLike[str],
    *,
    session: str = UNKNOWN_LABEL,
    project: str = UNKNOWN_LABEL,
    shape: str = "auto",
    field_map: str | dict[str, str] | None = None,
    pricing_table: dict | None = None,
    ledger_path: str | os.PathLike[str] | None = None,
    errors_out: list[str] | None = None,
) -> tuple[int, int]:
    """Meter ``logfile`` into the ledger.

    Returns ``(records_written, lines_skipped)``. Malformed JSON, non-object
    JSON, and usage blocks without token counts all count as skipped (the first
    few reasons go to ``errors_out`` when given).

    Raises :class:`~spendfence.errors.IngestError` when the format cannot be
    identified, and :class:`~spendfence.errors.PricingError` when a model has no
    price — a wrong price would poison every later report, so the run stops.
    """
    from . import state

    path = Path(logfile)
    if not path.exists():
        raise IngestError(f"log file not found: {path}")
    if path.is_dir():
        raise IngestError(f"not a file: {path}")

    chosen = (shape or "auto").strip().lower()
    if chosen not in SHAPES:
        raise IngestError(
            f"unknown format {shape!r}; choose one of: {', '.join(SHAPES)}"
        )
    if chosen == "generic" and not field_map:
        raise IngestError(
            "generic format needs a field map, e.g. --map input=usage.in,output=usage.out"
        )

    mapping = _parse_field_map(field_map)
    table = load_pricing() if pricing_table is None else pricing_table
    target_ledger = state.ledger_path() if ledger_path is None else ledger_path

    session_label = (
        session if isinstance(session, str) and session.strip() else UNKNOWN_LABEL
    )
    project_label = (
        project if isinstance(project, str) and project.strip() else UNKNOWN_LABEL
    )

    def note(message: str) -> None:
        if errors_out is not None and len(errors_out) < 10:
            errors_out.append(message)

    written = 0
    skipped = 0
    examined = 0
    nonblank = 0
    auto_mode = chosen == "auto"
    resolved = "" if auto_mode else chosen
    parser: _Parser | None = None
    sample: object = None

    with LedgerWriter(target_ledger) as writer:
        for line_number, raw in enumerate(iter_log_lines(path), start=1):
            if not raw.strip():
                continue
            nonblank += 1

            payload = _loads(raw)
            if payload is None:
                skipped += 1
                note(f"line {line_number}: not a JSON object")
                continue

            if parser is None:
                # Auto mode keeps sniffing line after line until one of them
                # identifies the shape; an explicit --format resolves at once.
                if not resolved and auto_mode:
                    resolved = detect_shape(payload) or ""
                if resolved == "generic":
                    parser = lambda obj, _m=mapping: parse_generic(obj, _m)
                elif resolved == "claude-code":
                    parser = parse_claude_code
                elif resolved == "codex":
                    parser = parse_codex

            if parser is None:
                # Nothing recognized so far. Keep a sample for the error message
                # and count the line as skipped — a line we did not meter must
                # never vanish from the report.
                examined += 1
                skipped += 1
                note(f"line {line_number}: no recognizable token usage")
                if sample is None:
                    sample = payload
                if examined >= DETECT_WINDOW:
                    raise _unknown_shape_error(path, sample)
                continue

            record = parser(payload)
            if record is None:
                skipped += 1
                note(f"line {line_number}: no token usage fields")
                continue

            model = _model_of(record)
            input_tokens = _count(record, "input_tokens")
            output_tokens = _count(record, "output_tokens")
            cache_read = _count(record, "cache_read")
            cache_write = _count(record, "cache_write")
            stamp = record.get("ts")
            cost = cost_usd(
                model, input_tokens, output_tokens, cache_read, cache_write, table=table
            )
            writer.append(
                ts=str(stamp)
                if isinstance(stamp, str) and stamp.strip()
                else _utc_now(),
                session=session_label,
                project=project_label,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read=cache_read,
                cache_write=cache_write,
                cost_usd=cost,
            )
            written += 1

    if parser is None:
        # The file had lines but none identified a known shape. Never report a
        # clean run that metered nothing — that is the silent-misparse failure.
        if nonblank == 0:
            note(f"{path} is empty")
        else:
            raise _unknown_shape_error(path, sample)

    return written, skipped


__all__ = [
    "DETECT_WINDOW",
    "EXPECTED_FIELDS",
    "SHAPES",
    "UNKNOWN_LABEL",
    "detect_shape",
    "ingest_file",
    "iter_log_lines",
    "parse_claude_code",
    "parse_codex",
    "parse_generic",
]
