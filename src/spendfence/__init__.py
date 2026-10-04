"""spendfence — offline token-spend metering and dollar budget enforcement.

The package is deliberately import-light: submodules are imported explicitly
(``from spendfence import ledger``) so that a single CLI command never pays for
modules it does not touch.
"""

__version__ = "0.1.3"

__all__ = ["__version__"]
