"""Money, refs, naming helpers — shared by bot + API."""
from __future__ import annotations

import re
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable

from .config import FEATURED_PRODUCT_KEYWORDS, MAIN_PRODUCT_KEYWORDS


def money(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"Invalid amount: {value!r}")


def fmoney(value: Any) -> str:
    return f"{money(value):.2f}"


def now_sql() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_ref(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def chunks(items: Iterable[Any], size: int):
    buf = []
    for item in items:
        buf.append(item)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def public_product_name(value: Any) -> str:
    name = str(value or "Unknown")
    name = re.sub(r"(?i)\[\s*(?:AIV|ETS)\s*\]", "", name)
    name = re.sub(r"(?i)\bAIVerse\b", "", name)
    name = re.sub(r"(?i)\bElite\s+Tools(?:\s+Store)?\b", "", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" -|•:[]")
    return name or "Digital Product"


def is_featured_product(x: dict) -> bool:
    name = public_product_name(x.get("name", "")).casefold()
    return any(keyword in name for keyword in FEATURED_PRODUCT_KEYWORDS)


def is_main_product(x: dict) -> bool:
    name = public_product_name(x.get("name", "")).casefold()
    return any(keyword in name for keyword in MAIN_PRODUCT_KEYWORDS)


def product_display_priority(x: dict) -> tuple:
    stock = int(x.get("stock", 0) or 0)
    in_stock = stock > 0
    own = str(x.get("supplier", "")).upper() == "OWN"

    if in_stock and own:
        tier = -1
    elif in_stock and is_main_product(x):
        tier = 0
    elif in_stock and is_featured_product(x):
        tier = 1
    elif in_stock:
        tier = 2
    elif own:
        tier = 3
    elif is_main_product(x):
        tier = 4
    elif is_featured_product(x):
        tier = 5
    else:
        tier = 6

    return (
        tier,
        public_product_name(x.get("name", "")).casefold(),
        str(x.get("product_key", "")),
    )
