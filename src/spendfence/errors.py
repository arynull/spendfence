"""Error types.

Every error the CLI is expected to handle lives here so that command code can
catch one base class and print ``str(exc)`` as a single clean line on stderr
instead of leaking a traceback.

Messages are written for a human reading a terminal: name the offending value,
say what was expected, and (where possible) say how to fix it.
"""

from __future__ import annotations


class SpendfenceError(Exception):
    """Base class for every user-facing spendfence error."""


class PricingError(SpendfenceError):
    """Bad pricing table, bad token count, or a model with no known price."""


class BudgetError(SpendfenceError):
    """Invalid budget scope, key, cap, or warning threshold."""


class IngestError(SpendfenceError):
    """A log file could not be interpreted safely (CLI exit code 1)."""


class LedgerError(SpendfenceError):
    """The ledger could not be read or extended."""


__all__ = [
    "BudgetError",
    "IngestError",
    "LedgerError",
    "PricingError",
    "SpendfenceError",
]
