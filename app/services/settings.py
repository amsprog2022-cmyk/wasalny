"""Admin-editable settings backed by the Setting key/value table.

Currently exposes the five pricing knobs (base / per-km / min / max /
commission rate) used by `pricing.quote_by_coords`. On first boot the
table is empty — callers fall back to Flask config defaults so nothing
breaks before an admin edits anything.

Reads are cheap (single PK lookup on a tiny table) so we don't add a
Redis cache. If quote latency ever becomes a hotspot we can add one
without changing this API.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from flask import current_app

from app import db
from app.models.setting import Setting


# The five keys we manage today. Whitelisted so a stray form field can't
# poison the table.
PRICING_KEYS = (
    "pricing_base_egp",
    "pricing_per_km_egp",
    "pricing_min_egp",
    "pricing_max_egp",
    "commission_rate",
)

# Fallback source when a key hasn't been written yet. Reads the same
# Flask config knobs pricing.py used to consult directly.
_CONFIG_DEFAULTS = {
    "pricing_base_egp":   ("PRICING_BASE_EGP",   "10"),
    "pricing_per_km_egp": ("PRICING_PER_KM_EGP", "3"),
    "pricing_min_egp":    ("PRICING_MIN_EGP",    "15"),
    "pricing_max_egp":    ("PRICING_MAX_EGP",    "500"),
    "commission_rate":    ("WASSALNY_COMMISSION_RATE", "0.15"),
}


def _get_raw(key: str) -> Optional[str]:
    row = db.session.get(Setting, key)
    if row is None:
        return None
    return row.value


def _get_or_default(key: str) -> str:
    val = _get_raw(key)
    if val is not None:
        return val
    conf_key, conf_default = _CONFIG_DEFAULTS[key]
    return str(current_app.config.get(conf_key, conf_default))


def get_pricing() -> dict:
    """Return the current pricing configuration as a dict of Decimals.

    Each key falls back to the Flask config default when the DB row is
    absent so a fresh install works without any admin action.
    """
    def _dec(key: str) -> Decimal:
        try:
            return Decimal(_get_or_default(key))
        except Exception:  # noqa: BLE001
            _, fallback = _CONFIG_DEFAULTS[key]
            return Decimal(fallback)

    return {
        "base_egp":        _dec("pricing_base_egp"),
        "per_km_egp":      _dec("pricing_per_km_egp"),
        "min_egp":         _dec("pricing_min_egp"),
        "max_egp":         _dec("pricing_max_egp"),
        "commission_rate": _dec("commission_rate"),
    }


def set_pricing(updates: dict) -> dict:
    """Persist any subset of the five pricing knobs.

    Keys are validated against PRICING_KEYS; values are cast to Decimal to
    reject non-numeric input. Returns the resulting full config so the
    caller can log / echo it. Commits the transaction.
    """
    for key, raw in updates.items():
        if key not in PRICING_KEYS:
            continue
        try:
            # Validate numeric — raises InvalidOperation if raw is garbage.
            Decimal(str(raw))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"invalid value for {key}: {raw}") from e
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=str(raw))
            db.session.add(row)
        else:
            row.value = str(raw)
    db.session.commit()
    return get_pricing()
