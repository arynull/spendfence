"""Filesystem locations for spendfence state.

All state lives in one directory: ``$SPENDFENCE_DATA_DIR`` when set, otherwise
``~/.spendfence``. The directory is created with mode 0o700 because a ledger is
a local record of what the machine spent.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_DATA_DIR = "SPENDFENCE_DATA_DIR"
DEFAULT_DIR_NAME = ".spendfence"

LEDGER_FILENAME = "ledger.jsonl"
BUDGETS_FILENAME = "budgets.json"
PRICING_FILENAME = "pricing.json"
STOP_FILENAME = "STOP"

_DIR_MODE = 0o700


def data_dir() -> Path:
    """Return the spendfence state directory, creating it if needed.

    ``SPENDFENCE_DATA_DIR`` wins when it is set to a non-blank value; it is the
    hook tests and sandboxes use. A relative value is resolved against the
    current working directory, which is what a user typing a bare path expects.
    """
    raw = os.environ.get(ENV_DATA_DIR)
    if raw is not None and raw.strip():
        path = Path(raw.strip()).expanduser()
    else:
        path = Path.home() / DEFAULT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    try:
        if (path.stat().st_mode & 0o777) != _DIR_MODE:
            path.chmod(_DIR_MODE)
    except OSError:
        pass
    return path


def ledger_path() -> Path:
    """Append-only hash-chained spend ledger (JSONL)."""
    return data_dir() / LEDGER_FILENAME


def budgets_path() -> Path:
    """Budget definitions."""
    return data_dir() / BUDGETS_FILENAME


def pricing_path() -> Path:
    """User pricing overrides merged over the built-in defaults."""
    return data_dir() / PRICING_FILENAME


def stop_path() -> Path:
    """Sentinel file: while it exists, ``check`` fails closed."""
    return data_dir() / STOP_FILENAME


def is_stopped() -> bool:
    """True when the STOP sentinel is present (the kill switch is engaged)."""
    return stop_path().exists()


__all__ = [
    "ENV_DATA_DIR",
    "DEFAULT_DIR_NAME",
    "LEDGER_FILENAME",
    "BUDGETS_FILENAME",
    "PRICING_FILENAME",
    "STOP_FILENAME",
    "data_dir",
    "ledger_path",
    "budgets_path",
    "pricing_path",
    "stop_path",
    "is_stopped",
]
