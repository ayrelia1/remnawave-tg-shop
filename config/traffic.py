"""Whole-GB offer and admin-input validation, independent of persisted usage."""

from decimal import Decimal, InvalidOperation

GIB = 2**30
MAX_TRAFFIC_GB = (2**63 - 1) // GIB


def traffic_bytes(value: object, *, allow_zero: bool = False) -> int:
    """Convert whole GB to signed-BIGINT-safe bytes without truncating input."""
    try:
        gb = Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid traffic amount") from exc
    if (not gb.is_finite() or gb < 0 or (gb == 0 and not allow_zero)
            or gb > MAX_TRAFFIC_GB or gb != gb.to_integral_value()):
        raise ValueError("Traffic must be a whole number of GB within the supported range")
    return int(gb) * GIB
