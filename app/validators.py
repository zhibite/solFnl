"""Shared validation helpers (Solana address / YYYY-MM month)."""

import re

SOL_ADDR_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def is_valid_sol_address(address: str) -> bool:
    """base58, 32-44 chars (same rule as the public API docs)."""
    return isinstance(address, str) and bool(SOL_ADDR_RE.match(address.strip()))


def is_valid_month(month: str) -> bool:
    """'YYYY-MM' with a real month number (01-12)."""
    return isinstance(month, str) and bool(MONTH_RE.match(month))


def parse_month(month: str) -> tuple[int, int]:
    """Split a validated 'YYYY-MM' into (year, month). Raises ValueError."""
    if not is_valid_month(month):
        raise ValueError(f"invalid month: {month!r}, expected YYYY-MM")
    y, m = month.split("-")
    return int(y), int(m)
