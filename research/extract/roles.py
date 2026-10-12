"""
Instrument role helpers: map Bookmap aliases/symbols to ES or NQ.
"""
from __future__ import annotations

import re


_NQ = re.compile(r"(?:^|[^A-Z])M?NQ[FGHJKMNQUVXZ]?\d|\bNASDAQ\b|^NQ", re.I)
_ES = re.compile(r"(?:^|[^A-Z])M?ES[FGHJKMNQUVXZ]?\d|\bS&P\b|\bSP500\b|^ES", re.I)


def classify_role(symbol: str | None, full_name: str | None = None, alias: str | None = None) -> str:
    combined = " ".join(x for x in (symbol, full_name, alias) if x).upper()
    if not combined.strip():
        return "OTHER"
    # Prefer explicit futures root at start of symbol
    sym = (symbol or alias or "").upper()
    if re.match(r"^M?NQ", sym) or "NASDAQ" in combined:
        return "NQ"
    if re.match(r"^M?ES", sym) or "S&P" in combined or "SP500" in combined:
        return "ES"
    if _NQ.search(combined):
        return "NQ"
    if _ES.search(combined):
        return "ES"
    return "OTHER"
