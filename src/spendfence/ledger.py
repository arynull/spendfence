"""Append-only, hash-chained spend ledger (JSONL).

Every metered chunk becomes one JSON object on its own line::

    {ts, session, project, model, input_tokens, output_tokens,
     cache_read, cache_write, cost_usd, prev_hash, hash}

``hash`` commits to the record's own fields *and* to ``prev_hash``, so editing
any earlier line invalidates every line after it. ``verify()`` walks the chain
and reports the first record that does not check out; the CLI turns that into
exit code 3.

Money is stored as a 6-decimal **string** so that a ledger written by one
process reads back byte-identically and exact-cent arithmetic survives the
round-trip; use :func:`record_cost` to get the ``Decimal``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .errors import LedgerError

#: ``prev_hash`` of the very first record.
GENESIS = "GENESIS"

#: Fields covered by the hash, in the order they are documented.
PAYLOAD_FIELDS = (
    "ts",
    "session",
    "project",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read",
    "cache_write",
    "cost_usd",
)

_COST_QUANT = Decimal("0.000001")
_FILE_MODE = 0o600
_DIR_MODE = 0o700


def canonical_json(obj: object) -> str:
    """The exact serialization the hash is computed over."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def record_digest(prev_hash: str, payload: dict[str, object]) -> str:
    """sha256 of ``prev_hash + canonical_json(payload)``."""
    blob = prev_hash + canonical_json(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _as_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise LedgerError(f"ledger field {field} must be a whole number, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            raise LedgerError(
                f"ledger field {field} must be a whole number, got {value!r}"
            ) from None
    raise LedgerError(
        f"ledger field {field} must be a whole number, got {type(value).__name__}"
    )


def _as_cost(value: object) -> str:
    if isinstance(value, bool):
        raise LedgerError(f"ledger field cost_usd must be a number, got {value!r}")
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, (int, float)):
        dec = Decimal(str(value))
    elif isinstance(value, str):
        try:
            dec = Decimal(value.strip())
        except InvalidOperation:
            raise LedgerError(
                f"ledger field cost_usd is not a number: {value!r}"
            ) from None
    else:
        raise LedgerError(
            f"ledger field cost_usd must be a number, got {type(value).__name__}"
        )
    if dec < 0:
        raise LedgerError(f"ledger field cost_usd cannot be negative ({dec})")
    return f"{dec.quantize(_COST_QUANT):f}"


def make_payload(
    *,
    ts: str,
    session: str,
    project: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    cost_usd: Decimal | str | float,
) -> dict[str, object]:
    """Build the hashed portion of a record, with types normalized."""
    return {
        "ts": str(ts),
        "session": str(session),
        "project": str(project),
        "model": str(model),
        "input_tokens": _as_int(input_tokens, field="input_tokens"),
        "output_tokens": _as_int(output_tokens, field="output_tokens"),
        "cache_read": _as_int(cache_read, field="cache_read"),
        "cache_write": _as_int(cache_write, field="cache_write"),
        "cost_usd": _as_cost(cost_usd),
    }


def record_cost(record: dict) -> Decimal:
    """Exact dollar amount for a record read back from the ledger."""
    return Decimal(str(record.get("cost_usd", "0")))


def validate_record(record: dict) -> list[str]:
    """Check one ledger record's schema; returns a list of human-readable reasons (empty = valid)."""
    reasons: list[str] = []
    for field in PAYLOAD_FIELDS:
        if field not in record:
            reasons.append(f"missing field '{field}'")
    for name in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
        if name not in record:
            continue
        try:
            value = _as_int(record[name], field=name)
        except LedgerError as exc:
            reasons.append(str(exc))
            continue
        if value < 0:
            reasons.append(f"field '{name}' is negative")
    if "cost_usd" not in record:
        pass
    else:
        try:
            _as_cost(record["cost_usd"])
        except LedgerError as exc:
            reasons.append(str(exc))
    return reasons


def _prepare(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    return target


def _ensure_mode(target: Path) -> None:
    """Best-effort: keep the ledger at 0o600 even if it already existed looser."""
    try:
        if (target.stat().st_mode & 0o777) != _FILE_MODE:
            os.chmod(target, _FILE_MODE)
    except OSError:
        pass


def _tail_record(target: Path) -> dict | None:
    """Read the last non-blank line of the ledger without loading the file.

    Seeks to the end and grows the window backwards, so appending stays O(1)
    in the size of the ledger.
    """
    try:
        size = target.stat().st_size
    except FileNotFoundError:
        return None
    if size == 0:
        return None

    with open(target, "rb") as handle:
        window = 512
        while True:
            start = max(0, size - window)
            handle.seek(start)
            chunk = handle.read(size - start)
            lines = [line for line in chunk.split(b"\n") if line.strip()]
            if start > 0:
                # The window may begin mid-line; drop that first fragment, and
                # keep growing if the window holds no complete line at all.
                first_newline = chunk.find(b"\n")
                lines = [] if first_newline == -1 else lines[1:]
            if lines:
                break
            if start == 0:
                return None
            window *= 2

    if not lines:
        return None
    try:
        record = json.loads(lines[-1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LedgerError(
            f"the last record in {target} is not valid JSON — run 'spendfence check' before appending"
        ) from None
    if not isinstance(record, dict):
        raise LedgerError(
            f"the last record in {target} is not a JSON object — refusing to append"
        )
    return record


class LedgerWriter:
    """Append many records with one open handle (and one chain in memory).

    Use as a context manager so the chain state — the previous hash — is read
    from disk exactly once no matter how many records follow.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = _prepare(path)
        self._handle = None
        self._prev_hash = GENESIS
        self._appended = 0

    def __enter__(self) -> LedgerWriter:  # noqa: PYI034 -- typing.Self needs 3.11+; we support 3.10 stdlib-only
        tail = _tail_record(self.path)
        if tail is None:
            self._prev_hash = GENESIS
        else:
            last_hash = tail.get("hash")
            if not isinstance(last_hash, str) or not last_hash:
                raise LedgerError(
                    f"the last record in {self.path} has no hash — run 'spendfence check'"
                )
            self._prev_hash = last_hash
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._handle = os.fdopen(fd, "a", encoding="utf-8")
        _ensure_mode(self.path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, ValueError):
                pass
            handle.close()

    @property
    def appended(self) -> int:
        return self._appended

    @property
    def prev_hash(self) -> str:
        return self._prev_hash

    def append(self, **payload_kwargs: object) -> dict:
        """Hash and write one record; returns the stored record."""
        if self._handle is None:
            raise LedgerError("ledger writer is closed")
        payload = make_payload(**payload_kwargs)  # type: ignore[arg-type]
        record = dict(payload)
        record["prev_hash"] = self._prev_hash
        record["hash"] = record_digest(self._prev_hash, payload)
        self._handle.write(canonical_json(record) + "\n")
        self._prev_hash = record["hash"]
        self._appended += 1
        return record


def append_record(
    path: str | os.PathLike[str],
    *,
    ts: str,
    session: str,
    project: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    cost_usd: Decimal | str | float,
) -> dict:
    """Append exactly one metered record to ``path``; returns the record."""
    with LedgerWriter(path) as writer:
        return writer.append(
            ts=ts,
            session=session,
            project=project,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            cost_usd=cost_usd,
        )


def iter_records(path: str | os.PathLike[str]) -> Iterator[dict]:
    """Stream records from the ledger, skipping blank lines.

    Missing file yields nothing (an empty ledger is a valid ledger). Lines that
    are not JSON objects are skipped rather than raising, so that a truncated
    tail never hides the records that came before it; ``verify()`` is what
    reports damage.
    """
    target = Path(path)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except FileNotFoundError:
        return


def read_records(path: str | os.PathLike[str]) -> list[dict]:
    """All records as a list. Convenient for small ledgers and tests."""
    return list(iter_records(path))


def verify(path: str | os.PathLike[str]) -> tuple[bool, int]:
    """Check the hash chain.

    Returns ``(True, -1)`` when every record checks out, or ``(False, index)``
    with the 0-based index of the first record that does not — index 1 means
    the second line is where the chain first goes wrong. A missing or empty
    ledger is intact.
    """
    target = Path(path)
    if not target.exists():
        return True, -1

    prev = GENESIS
    index = 0
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    return False, index
                if not isinstance(record, dict):
                    return False, index
                if any(field not in record for field in PAYLOAD_FIELDS):
                    return False, index
                payload = {field: record[field] for field in PAYLOAD_FIELDS}
                if record.get("prev_hash") != prev:
                    return False, index
                if record.get("hash") != record_digest(prev, payload):
                    return False, index
                prev = record["hash"]
                index += 1
    except OSError:
        return False, 0
    return True, -1


__all__ = [
    "GENESIS",
    "PAYLOAD_FIELDS",
    "LedgerWriter",
    "append_record",
    "canonical_json",
    "iter_records",
    "make_payload",
    "read_records",
    "record_cost",
    "record_digest",
    "validate_record",
    "verify",
]
