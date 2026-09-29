"""
Premium Hub Unified Backend
- FastAPI serves Web + Admin APIs
- Same SQLite DB as Telegram Bot (ars_bot.db)
- Payment webhook shared
- Bot runs in parallel process (same volume/DB)
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
import uuid
from decimal import Decimal
from typing import Any, List, Optional

import requests
from collections import defaultdict
from time import time as _time

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .core.config import (
    ADMIN_IDS, BOT_TOKEN, CORS_ORIGINS, DB, PAYMENT_API_KEY, PAYMENT_BASE_URL,
    PAYMENT_WEBHOOK_SECRET, PHEAD, SHOP_NAME, SUPPORT_USERNAME, SUPPORT_URL,
    TELEGRAM_BOT_USERNAME, _PAYMENT_OK, AHEAD, BASE_URL, EHEAD, ELITE_BASE_URL,
    ELITE_API_KEY, ELITE_PRODUCTS_PATH, ELITE_ORDER_PATH, SHEAD, SHOPBOT_BASE_URL,
    SHOPBOT_API_KEY, AIVERSE_MARKUP_PERCENT, AIVERSE_MARKUP_USDT,
    ELITE_MARKUP_PERCENT, ELITE_MARKUP_USDT, SHOPBOT_MARKUP_PERCENT, SHOPBOT_MARKUP_USDT,
    _UPSTREAM_SUPPLIERS, FEATURED_PRODUCT_KEYWORDS, MAIN_PRODUCT_KEYWORDS,
)
from .core.db import init_db, db, get_setting, set_setting_value, row_to_dict, backup_database
from .core.helpers import (
    money, fmoney, new_ref, public_product_name, is_featured_product,
    is_main_product, product_display_priority,
)
from .core.users import (
    get_user, ensure_user, get_balance, is_banned, admin_add_balance,
    admin_remove_balance, search_users, list_users, user_stats, get_transactions,
    ban_user, unban_user,
)
from .auth.jwt_auth import (
    get_current_user, require_admin, login_from_telegram_widget,
    create_access_token, get_optional_user,
)

HTTP = requests.Session()

app = FastAPI(
    title="Premium Hub API",
    description="Unified backend for Telegram Bot + Web App",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SimpleRateLimit(BaseHTTPMiddleware):
    """In-memory rate limit for auth endpoints (per IP)."""

    def __init__(self, app, max_hits: int = 30, window: int = 60):
        super().__init__(app)
        self.max_hits = max_hits
        self.window = window
        self.hits: dict = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path or ""
        if path.startswith("/api/auth/"):
            ip = request.client.host if request.client else "unknown"
            now = _time()
            bucket = [t for t in self.hits[ip] if now - t < self.window]
            if len(bucket) >= self.max_hits:
                return JSONResponse({"detail": "Too many requests"}, status_code=429)
            bucket.append(now)
            self.hits[ip] = bucket
        return await call_next(request)


app.add_middleware(SimpleRateLimit)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class TelegramLoginBody(BaseModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""
    auth_date: int
    hash: str


class TopupCreate(BaseModel):
    amount: float = Field(..., gt=0)


class VerifyPayment(BaseModel):
    reference: str
    txid: str


class OrderCreate(BaseModel):
    product_key: str
    quantity: int = Field(1, ge=1, le=100)
    payment_method: str = Field("BALANCE", pattern="^(BALANCE|DIRECT)$")


class AdminBalanceBody(BaseModel):
    telegram_id: str
    amount: float = Field(..., gt=0)
    note: str = ""


class AdminProductCreate(BaseModel):
    name: str
    price: float = Field(..., gt=0)
    validity: str = ""
    warranty: str = "No Warranty"


class AdminStockAdd(BaseModel):
    product_key: str
    payloads: List[str]


class AdminBanBody(BaseModel):
    telegram_id: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Product helpers (mirrored from bot — same pricing & catalog rules)
# ---------------------------------------------------------------------------
_product_cache: dict = {
    "AIVERSE": {"at": 0.0, "services": []},
    "ELITE": {"at": 0.0, "services": []},
    "SHOPBOT": {"at": 0.0, "services": []},
}
_product_lock = threading.Lock()
PRODUCT_CACHE_SECONDS = 8


def _catalog_key(supplier: str, product_id: str) -> str:
    return hashlib.sha256(f"{supplier.upper()}|{product_id}".encode()).hexdigest()[:16]


def _supplier_markup(supplier: str):
    supplier = str(supplier or "AIVERSE").upper()
    if supplier == "ELITE":
        env_p, env_f = ELITE_MARKUP_PERCENT, ELITE_MARKUP_USDT
    elif supplier == "SHOPBOT":
        env_p, env_f = SHOPBOT_MARKUP_PERCENT, SHOPBOT_MARKUP_USDT
    else:
        env_p, env_f = AIVERSE_MARKUP_PERCENT, AIVERSE_MARKUP_USDT
    try:
        p_raw = get_setting(f"markup_percent:{supplier}", "")
        f_raw = get_setting(f"markup_fixed:{supplier}", "")
        percent = money(p_raw) if str(p_raw).strip() != "" else env_p
        fixed = money(f_raw) if str(f_raw).strip() != "" else env_f
    except Exception:
        percent, fixed = env_p, env_f
    return percent, fixed


def customer_price(x: dict) -> Decimal:
    supplier = str(x.get("supplier", "AIVERSE")).upper()
    base = money(x.get("price", 0))
    if supplier == "OWN":
        return base
    percent, fixed = _supplier_markup(supplier)
    if percent < 0:
        percent = Decimal("0")
    if fixed < 0:
        fixed = Decimal("0")
    return money(base * (Decimal("1") + (percent / Decimal("100"))) + fixed)


def supplier_enabled(supplier: str) -> bool:
    supplier = str(supplier or "").upper()
    if supplier not in _UPSTREAM_SUPPLIERS:
        return True
    default = "0" if supplier == "SHOPBOT" and not SHOPBOT_API_KEY else "1"
    if supplier == "SHOPBOT" and not SHOPBOT_API_KEY:
        return False
    return get_setting(f"supplier_enabled:{supplier}", default) == "1"


def get_hidden_product_keys() -> set:
    try:
        data = json.loads(get_setting("hidden_product_keys", "[]") or "[]")
        if isinstance(data, list):
            return {str(x) for x in data}
    except Exception:
        pass
    return set()


def own_services(include_disabled: bool = False) -> list:
    c = db()
    where = "" if include_disabled else "WHERE p.enabled=1"
    rows = c.execute(
        f"""SELECT p.*,
              COALESCE(SUM(CASE WHEN s.status='AVAILABLE' THEN 1 ELSE 0 END),0) AS available_stock
            FROM custom_products p
            LEFT JOIN custom_stock s ON s.product_key=p.product_key
            {where}
            GROUP BY p.product_key
            ORDER BY p.created_at"""
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        key = str(r["product_key"])
        out.append({
            "supplier": "OWN",
            "product_id": key,
            "service_id": key,
            "product_key": key,
            "name": str(r["name"]),
            "price": money(r["price"]),
            "stock": int(r["available_stock"] or 0),
            "validity": str(r["validity"] or ""),
            "warranty": str(r["warranty"] or "No Warranty"),
            "raw": {"validity": str(r["validity"] or ""), "warranty": str(r["warranty"] or "No Warranty"), "own_stock": True},
        })
    return out


def _normalize_product(raw: dict, supplier: str) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    supplier = supplier.upper()
    if supplier == "AIVERSE":
        pid = raw.get("service_id") or raw.get("productId") or raw.get("product_id") or raw.get("id")
        name = raw.get("name") or raw.get("title") or "Unknown"
        price = raw.get("price") or raw.get("resellerPrice") or raw.get("reseller_price")
        stock_raw = raw.get("stock") or raw.get("quantity") or raw.get("availableStock")
    elif supplier == "SHOPBOT":
        pid = raw.get("id") or raw.get("product_id")
        name = raw.get("name") or raw.get("title") or "Unknown"
        unit_p = raw.get("unit_price") or raw.get("price")
        list_p = raw.get("list_price") or unit_p
        try:
            unit_m = money(unit_p) if unit_p is not None else None
            list_m = money(list_p) if list_p is not None else None
        except Exception:
            return None
        if unit_m is None and list_m is None:
            return None
        price = max(x for x in [unit_m, list_m] if x is not None)
        stock_raw = raw.get("stock_count") or raw.get("stock") or raw.get("quantity")
        if raw.get("in_stock") is False:
            stock_raw = 0
    else:
        pid = raw.get("productId") or raw.get("product_id") or raw.get("id") or raw.get("service_id")
        name = raw.get("name") or raw.get("title") or "Unknown"
        price = raw.get("price") or raw.get("resellerPrice") or raw.get("unitPrice")
        stock_raw = raw.get("stock") or raw.get("quantity") or raw.get("availableStock")
    if pid is None or price is None:
        return None
    try:
        p = money(price)
    except Exception:
        return None
    try:
        stock = max(0, int(float(stock_raw))) if stock_raw is not None else 999999
    except Exception:
        stock = 999999
    return {
        "supplier": supplier,
        "product_id": str(pid),
        "service_id": str(pid),
        "name": str(name),
        "price": p,
        "stock": stock,
        "raw": raw,
    }


def _extract_product_list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("services", "products", "items", "data", "result"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for nested in ("services", "products", "items"):
                if isinstance(v.get(nested), list):
                    return v[nested]
    return []


def _supplier_get(urls: list, headers: dict, label: str) -> dict:
    last_error = None
    for i, url in enumerate(urls):
        try:
            r = HTTP.get(url, headers=headers, timeout=20)
        except requests.RequestException as e:
            last_error = e
            continue
        if r.status_code == 404 and i + 1 < len(urls):
            continue
        try:
            d = r.json()
        except Exception as e:
            raise RuntimeError(f"{label} non-JSON HTTP {r.status_code}") from e
        if r.status_code >= 400:
            msg = d.get("message") or d.get("error") if isinstance(d, dict) else None
            raise RuntimeError(msg or f"{label} HTTP {r.status_code}")
        return d
    raise RuntimeError(f"{label} unavailable: {last_error}")


def aiverse_services(force: bool = False) -> list:
    now = time.time()
    with _product_lock:
        cache = _product_cache["AIVERSE"]
        if not force and cache["services"] and now - float(cache["at"]) <= PRODUCT_CACHE_SECONDS:
            return [dict(x) for x in cache["services"]]
    d = _supplier_get([f"{BASE_URL}/api/v1/products"], AHEAD, "AIVerse")
    out = []
    for raw in _extract_product_list(d):
        x = _normalize_product(raw, "AIVERSE")
        if x:
            x["product_key"] = _catalog_key("AIVERSE", x["product_id"])
            out.append(x)
    with _product_lock:
        _product_cache["AIVERSE"] = {"at": now, "services": out}
    return [dict(x) for x in out]


def elite_services(force: bool = False) -> list:
    now = time.time()
    with _product_lock:
        cache = _product_cache["ELITE"]
        if not force and cache["services"] and now - float(cache["at"]) <= PRODUCT_CACHE_SECONDS:
            return [dict(x) for x in cache["services"]]
    urls = [f"{ELITE_BASE_URL}{ELITE_PRODUCTS_PATH}"]
    if ELITE_PRODUCTS_PATH != "/api/products":
        urls.append(f"{ELITE_BASE_URL}/api/products")
    d = _supplier_get(urls, EHEAD, "Elite")
    out = []
    for raw in _extract_product_list(d):
        x = _normalize_product(raw, "ELITE")
        if x:
            x["product_key"] = _catalog_key("ELITE", x["product_id"])
            out.append(x)
    with _product_lock:
        _product_cache["ELITE"] = {"at": now, "services": out}
    return [dict(x) for x in out]


def shopbot_services(force: bool = False) -> list:
    if not SHOPBOT_API_KEY:
        return []
    now = time.time()
    with _product_lock:
        cache = _product_cache["SHOPBOT"]
        if not force and cache["services"] and now - float(cache["at"]) <= PRODUCT_CACHE_SECONDS:
            return [dict(x) for x in cache["services"]]
    d = _supplier_get([f"{SHOPBOT_BASE_URL}/products"], SHEAD, "ShopAPI")
    out = []
    for raw in _extract_product_list(d):
        x = _normalize_product(raw, "SHOPBOT")
        if x:
            x["product_key"] = _catalog_key("SHOPBOT", x["product_id"])
            out.append(x)
    with _product_lock:
        _product_cache["SHOPBOT"] = {"at": now, "services": out}
    return [dict(x) for x in out]


def services(force: bool = False) -> list:
    all_products = []
    for supplier, loader in (
        ("AIVERSE", aiverse_services),
        ("ELITE", elite_services),
        ("SHOPBOT", shopbot_services),
    ):
        if not supplier_enabled(supplier):
            continue
        try:
            all_products.extend(loader(force=force))
        except Exception as e:
            print(f"{supplier} load error:", e)
    try:
        all_products.extend(own_services())
    except Exception as e:
        print("OWN load error:", e)
    hidden = get_hidden_product_keys()
    if hidden:
        all_products = [p for p in all_products if str(p.get("product_key")) not in hidden]
    return all_products


def customer_catalog(products: list) -> list:
    best: dict = {}
    for item in products:
        key = re.sub(r"\s+", " ", public_product_name(item.get("name", "")).casefold()).strip()
        current = best.get(key)
        if current is None:
            best[key] = item
            continue
        cur_stock = int(current.get("stock", 0) or 0) > 0
        new_stock = int(item.get("stock", 0) or 0) > 0
        cur_own = str(current.get("supplier", "")).upper() == "OWN"
        new_own = str(item.get("supplier", "")).upper() == "OWN"
        choose_new = False
        if new_stock != cur_stock:
            choose_new = new_stock
        elif new_stock and new_own != cur_own:
            choose_new = new_own
        elif new_own == cur_own:
            choose_new = customer_price(item) < customer_price(current)
        if choose_new:
            best[key] = item
    return list(best.values())


def service_by_key(token: str, force: bool = False) -> Optional[dict]:
    token = str(token)
    for p in own_services(include_disabled=False):
        if p["product_key"] == token:
            return p
    for p in services(force=force):
        if str(p.get("product_key")) == token:
            return p
    # catalog fallback
    c = db()
    cat = c.execute("SELECT * FROM supplier_catalog WHERE product_key=?", (token,)).fetchone()
    c.close()
    if cat and supplier_enabled(str(cat["supplier"])):
        return {
            "supplier": str(cat["supplier"]),
            "product_id": cat["product_id"],
            "service_id": cat["product_id"],
            "product_key": cat["product_key"],
            "name": cat["name"] or "Unknown",
            "price": money(cat["price"] or 0),
            "stock": int(cat["stock"] or 0),
            "raw": {},
        }
    return None


def product_public(x: dict) -> dict:
    raw = x.get("raw") if isinstance(x.get("raw"), dict) else {}
    validity = raw.get("validity") or x.get("validity") or "Not specified"
    warranty = raw.get("warranty") or x.get("warranty") or "No Warranty"
    stock = int(x.get("stock", 0) or 0)
    return {
        "product_key": str(x.get("product_key") or ""),
        "name": public_product_name(x.get("name")),
        "price": float(customer_price(x)),
        "stock": stock,
        "stock_text": "Available" if stock >= 999999 else str(stock),
        "in_stock": stock > 0,
        "validity": str(validity),
        "warranty": str(warranty),
        "featured": is_featured_product(x),
        "main_offer": is_main_product(x),
        "supplier_type": "OWN" if str(x.get("supplier", "")).upper() == "OWN" else "SUPPLIER",
    }


# ---------------------------------------------------------------------------
# Payment helpers
# ---------------------------------------------------------------------------
def create_invoice(telegram_id: Any, amount: Decimal) -> tuple:
    r = HTTP.post(
        f"{PAYMENT_BASE_URL}/api/v1/invoice",
        headers=PHEAD,
        json={"telegram_id": str(telegram_id), "amount": fmoney(amount), "currency": "USDT"},
        timeout=30,
    )
    try:
        d = r.json()
    except Exception:
        d = {}
    if r.status_code >= 400 or not d.get("ok"):
        raise RuntimeError(d.get("message") or d.get("error") or f"PayHub HTTP {r.status_code}")
    iid = d.get("invoice_id") or d.get("invoiceId") or d.get("invoice_no")
    uid = (
        d.get("uid") or d.get("binance_uid") or d.get("binanceUid")
        or d.get("pay_uid") or d.get("payment_uid") or d.get("wallet_id")
    )
    if not iid:
        raise RuntimeError("PayHub did not return invoice_id")
    return str(iid), str(uid) if uid else ""


def verify_payhub(iid: str, pid: str) -> tuple:
    last = {}
    for key in ("order_id", "txid", "tx_id"):
        try:
            r = HTTP.post(
                f"{PAYMENT_BASE_URL}/api/v1/verify",
                headers=PHEAD,
                json={"invoice_id": str(iid), key: str(pid)},
                timeout=30,
            )
            d = r.json()
        except Exception as e:
            last = {"error": str(e)}
            continue
        last = d
        st = str(d.get("status", "")).upper()
        if (
            d.get("paid") is True or d.get("verified") is True
            or d.get("confirmed") is True or st in _PAYMENT_OK
        ):
            return True, d
    return False, last


def _claim_payment(c, payment_id: str, invoice_id: str, kind: str, reference: str) -> bool:
    pid = (payment_id or f"INVOICE:{invoice_id}").strip()
    try:
        c.execute(
            "INSERT INTO payment_claims(payment_id,invoice_id,kind,reference) VALUES(?,?,?,?)",
            (pid, str(invoice_id), kind, reference),
        )
        return True
    except Exception:
        r = c.execute(
            "SELECT invoice_id,kind,reference FROM payment_claims WHERE payment_id=?", (pid,)
        ).fetchone()
        return bool(
            r and str(r["invoice_id"]) == str(invoice_id)
            and r["kind"] == kind and r["reference"] == reference
        )


def _topup_paid_once(topup_ref: str, payment_id: str, data: dict) -> tuple:
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM topups WHERE topup_ref=?", (topup_ref,)).fetchone()
        if not row:
            c.rollback()
            return "not-found", None
        if row["status"] == "PAID":
            bal = c.execute("SELECT balance FROM users WHERE telegram_id=?", (row["telegram_id"],)).fetchone()
            c.commit()
            return "duplicate", money(bal["balance"] if bal else 0)
        if row["status"] != "PENDING":
            c.rollback()
            return "invalid-status", None
        amt = data.get("amount")
        if amt is not None:
            try:
                if money(amt) != money(row["amount"]):
                    c.rollback()
                    return "amount-mismatch", None
            except Exception:
                pass
        if not _claim_payment(c, payment_id, row["invoice_id"], "TOPUP", row["topup_ref"]):
            c.rollback()
            return "payment-already-used", None
        u = c.execute("SELECT balance FROM users WHERE telegram_id=?", (row["telegram_id"],)).fetchone()
        if not u:
            c.rollback()
            return "user-not-found", None
        before = money(u["balance"])
        amount = money(row["amount"])
        after = money(before + amount)
        c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (float(after), row["telegram_id"]))
        c.execute(
            """INSERT INTO transactions(telegram_id,kind,amount,balance_before,balance_after,reference,status,note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (row["telegram_id"], "TOPUP", float(amount), float(before), float(after),
             row["topup_ref"], "COMPLETED", f"Invoice {row['invoice_id']}"),
        )
        c.execute(
            "UPDATE topups SET txid=?,status='PAID',updated_at=CURRENT_TIMESTAMP WHERE topup_ref=?",
            (payment_id, row["topup_ref"]),
        )
        c.commit()
        return "ok", after
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Order / delivery (simplified shared path for web)
# ---------------------------------------------------------------------------
def _reserve_own_stock_tx(c, product_key: str, quantity: int, order_ref: str) -> None:
    rows = c.execute(
        """SELECT id FROM custom_stock WHERE product_key=? AND status='AVAILABLE'
           ORDER BY id LIMIT ?""",
        (str(product_key), int(quantity)),
    ).fetchall()
    if len(rows) != int(quantity):
        raise ValueError("OUT_OF_STOCK")
    ids = [int(r["id"]) for r in rows]
    marks = ",".join("?" for _ in ids)
    c.execute(
        f"""UPDATE custom_stock SET status='RESERVED',order_ref=?,reserved_at=CURRENT_TIMESTAMP
            WHERE id IN ({marks}) AND status='AVAILABLE'""",
        [str(order_ref), *ids],
    )
    if c.total_changes < len(ids):
        raise ValueError("OUT_OF_STOCK")


def create_balance_order_web(cid: Any, x: dict, quantity: int = 1) -> tuple:
    sid = str(x.get("product_id") or x.get("service_id"))
    supplier = str(x.get("supplier", "AIVERSE")).upper()
    product_key = str(x.get("product_key") or (_catalog_key(supplier, sid) if supplier != "OWN" else sid))
    name = str(x.get("name", "Unknown"))
    quantity = max(1, min(int(quantity), 100))
    cp_unit = customer_price(x)
    cp = money(cp_unit * quantity)
    sp_unit = Decimal("0.00") if supplier == "OWN" else money(x.get("price", 0))
    sp = money(sp_unit * quantity)
    ref = new_ref("ORD")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        u = c.execute("SELECT balance FROM users WHERE telegram_id=?", (str(cid),)).fetchone()
        if not u:
            raise RuntimeError("User not found")
        before = money(u["balance"])
        if before < cp:
            c.rollback()
            raise ValueError("INSUFFICIENT_BALANCE")
        if supplier == "OWN":
            _reserve_own_stock_tx(c, product_key, quantity, ref)
        after = money(before - cp)
        c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (float(after), str(cid)))
        c.execute(
            """INSERT INTO transactions(telegram_id,kind,amount,balance_before,balance_after,reference,status,note)
               VALUES(?,?,?,?,?,?,?,?)""",
            (str(cid), "PURCHASE", -float(cp), float(before), float(after), ref, "COMPLETED", name),
        )
        c.execute(
            """INSERT INTO orders(
                 order_ref,telegram_id,service_id,product_name,quantity,
                 supplier_price,customer_price,status,payment_method,paid_at,supplier,product_key,source
               ) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?)""",
            (ref, str(cid), sid, name, quantity, float(sp), float(cp), "PAID", "BALANCE", supplier, product_key, "web"),
        )
        c.commit()
        return ref, cp, after
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


# Delivery is delegated: for web orders we mark PAID and rely on the same
# delivery machinery. To keep one source of truth, web triggers delivery via
# a lightweight internal call; full supplier delivery lives in the bot process.
# For OWN stock we complete delivery here.
def finish_own_delivery(order_ref: str, quantity: int) -> list:
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        rows = c.execute(
            """SELECT id,payload FROM custom_stock
               WHERE order_ref=? AND status='RESERVED' ORDER BY id LIMIT ?""",
            (str(order_ref), int(quantity)),
        ).fetchall()
        if len(rows) != int(quantity):
            c.rollback()
            raise RuntimeError("Reserved own stock incomplete")
        ids = [int(r["id"]) for r in rows]
        payloads = [str(r["payload"]) for r in rows]
        marks = ",".join("?" for _ in ids)
        c.execute(
            f"""UPDATE custom_stock SET status='DELIVERED',delivered_at=CURRENT_TIMESTAMP
                WHERE id IN ({marks}) AND status='RESERVED'""",
            ids,
        )
        c.execute(
            """UPDATE orders SET status='COMPLETED',delivery_payload=?,
               delivery_error=NULL,delivered_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE order_ref=?""",
            (json.dumps(payloads, ensure_ascii=False), str(order_ref)),
        )
        c.commit()
        return payloads
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    from .core.db import integrity_check, backup_database

    init_db()
    ok, msg = integrity_check()
    print(f"🗄 DB integrity: {msg}")
    if not ok:
        print("⚠️ Database integrity check failed — restore from backup if needed")
    # Auto backup on boot when BACKUP_DIR is set
    try:
        if os.getenv("BACKUP_DIR") or os.getenv("DB_BACKUP_DIR"):
            backup_database("startup")
    except Exception as e:
        print("Startup backup skip:", e)
    print(f"✅ Premium Hub API ready | DB={os.path.abspath(DB)} | Shop={SHOP_NAME}")


# ---------------------------------------------------------------------------
# Health & public
# ---------------------------------------------------------------------------
@app.get("/health")
@app.get("/api/health")
def health():
    from .core.db import integrity_check

    ok, msg = integrity_check()
    return {
        "ok": True,
        "service": "premium-hub",
        "shop": SHOP_NAME,
        "db_ok": ok,
        "db_path": os.path.abspath(DB),
    }


@app.get("/api/public/info")
def public_info():
    return {
        "shop_name": SHOP_NAME,
        "support_username": SUPPORT_USERNAME,
        "support_url": SUPPORT_URL or f"https://t.me/{SUPPORT_USERNAME}",
        "bot_username": TELEGRAM_BOT_USERNAME,
        "telegram_login_bot": TELEGRAM_BOT_USERNAME,
    }


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.post("/api/auth/telegram")
def auth_telegram(body: TelegramLoginBody):
    return login_from_telegram_widget(body.model_dump())


class EmailRegister(BaseModel):
    email: str
    password: str
    name: str = ""
    phone: str = ""


class EmailLogin(BaseModel):
    email: str
    password: str


class EmailVerify(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordReset(BaseModel):
    token: str
    new_password: str


@app.post("/api/auth/register")
def auth_register(body: EmailRegister):
    from .auth.email_auth import register_email

    return register_email(body.email, body.password, body.name, body.phone)


@app.post("/api/auth/login")
def auth_login(body: EmailLogin):
    from .auth.email_auth import login_email

    return login_email(body.email, body.password)


@app.post("/api/auth/verify-email")
def auth_verify_email(body: EmailVerify):
    from .auth.email_auth import verify_email_token

    return verify_email_token(body.token)


@app.post("/api/auth/forgot-password")
def auth_forgot_password(body: PasswordResetRequest):
    from .auth.email_auth import request_password_reset

    return request_password_reset(body.email)


@app.post("/api/auth/reset-password")
def auth_reset_password(body: PasswordReset):
    from .auth.email_auth import reset_password

    return reset_password(body.token, body.new_password)


class LinkEmailBody(BaseModel):
    email: str
    password: str


class LinkTelegramBody(BaseModel):
    telegram_id: str
    hash: str = ""
    auth_date: int = 0
    first_name: str = ""
    username: str = ""


@app.post("/api/auth/link-email")
def auth_link_email(body: LinkEmailBody, user: dict = Depends(get_current_user)):
    """Telegram account → add email login (merges email wallet if exists)."""
    from .auth.email_auth import link_email_to_telegram

    return link_email_to_telegram(str(user["telegram_id"]), body.email, body.password)


@app.post("/api/auth/link-telegram")
def auth_link_telegram(body: LinkTelegramBody, user: dict = Depends(get_current_user)):
    """Email account → link Telegram (merges into telegram_id)."""
    from .auth.email_auth import link_telegram_to_email
    from .auth.jwt_auth import verify_telegram_login

    if body.hash:
        payload = {
            "id": int(body.telegram_id),
            "first_name": body.first_name,
            "username": body.username,
            "auth_date": body.auth_date,
            "hash": body.hash,
        }
        if not verify_telegram_login(payload):
            raise HTTPException(401, "Invalid Telegram auth")
        ensure_user(body.telegram_id, body.username, body.first_name)
    return link_telegram_to_email(str(user["telegram_id"]), body.telegram_id)


# Dev/helper: login by telegram_id only when ADMIN creates a session (optional)
class DevLogin(BaseModel):
    telegram_id: str
    secret: str = ""


@app.post("/api/auth/dev-login")
def auth_dev_login(body: DevLogin):
    """Only works if DEV_LOGIN_SECRET is set — for testing without widget."""
    expected = os.getenv("DEV_LOGIN_SECRET", "").strip()
    if not expected or body.secret != expected:
        raise HTTPException(403, "Disabled")
    user = ensure_user(body.telegram_id)
    if is_banned(body.telegram_id):
        raise HTTPException(403, "Banned")
    token = create_access_token(str(body.telegram_id))
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "telegram_id": user.get("telegram_id"),
            "username": user.get("username"),
            "first_name": user.get("first_name"),
            "balance": float(user.get("balance") or 0),
        },
    }


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------
@app.get("/api/user/me")
def user_me(user: dict = Depends(get_current_user)):
    from .auth.email_auth import enrich_user_for_api

    stats = user_stats(user["telegram_id"])
    base = {
        "telegram_id": user["telegram_id"],
        "username": user.get("username"),
        "first_name": user.get("first_name"),
        "balance": float(user.get("balance") or 0),
        "joined_at": user.get("joined_at"),
        "banned": bool(int(user.get("banned") or 0)),
        **stats,
    }
    return enrich_user_for_api(base)


@app.get("/api/user/balance")
def user_balance(user: dict = Depends(get_current_user)):
    return {"balance": float(get_balance(user["telegram_id"]))}


@app.get("/api/user/transactions")
def user_tx(user: dict = Depends(get_current_user), limit: int = Query(20, le=50)):
    rows = get_transactions(user["telegram_id"], limit)
    return {"transactions": rows}


@app.get("/api/user/orders")
def user_orders(user: dict = Depends(get_current_user), limit: int = Query(20, le=50)):
    c = db()
    rows = c.execute(
        "SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
        (str(user["telegram_id"]), limit),
    ).fetchall()
    c.close()
    out = []
    for r in rows:
        d = row_to_dict(r)
        d["product_name"] = public_product_name(d.get("product_name"))
        if d.get("status") == "COMPLETED" and d.get("delivery_payload"):
            try:
                d["delivery"] = json.loads(d["delivery_payload"])
            except Exception:
                d["delivery"] = [d["delivery_payload"]]
        else:
            d["delivery"] = None
        # hide internal fields for non-admin
        d.pop("supplier_price", None)
        out.append(d)
    return {"orders": out}


@app.get("/api/user/orders/{order_ref}")
def user_order_detail(order_ref: str, user: dict = Depends(get_current_user)):
    c = db()
    r = c.execute(
        "SELECT * FROM orders WHERE order_ref=? AND telegram_id=?",
        (order_ref.upper(), str(user["telegram_id"])),
    ).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "Order not found")
    d = row_to_dict(r)
    d["product_name"] = public_product_name(d.get("product_name"))
    if d.get("status") == "COMPLETED" and d.get("delivery_payload"):
        try:
            d["delivery"] = json.loads(d["delivery_payload"])
        except Exception:
            d["delivery"] = [d["delivery_payload"]]
    else:
        d["delivery"] = None
    return d


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------
@app.get("/api/products")
def list_products(force: bool = False, user: Optional[dict] = Depends(get_optional_user)):
    try:
        ss = customer_catalog(services(force=force))
        ss = sorted(ss, key=product_display_priority)
        return {"products": [product_public(x) for x in ss], "total": len(ss)}
    except Exception as e:
        raise HTTPException(502, f"Product load failed: {e}")


@app.get("/api/products/{product_key}")
def product_detail(product_key: str):
    x = service_by_key(product_key, force=True)
    if not x:
        raise HTTPException(404, "Product not found")
    return product_public(x)


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
@app.post("/api/orders")
def create_order(body: OrderCreate, user: dict = Depends(get_current_user)):
    x = service_by_key(body.product_key, force=True)
    if not x or int(x.get("stock", 0) or 0) <= 0:
        raise HTTPException(400, "Product unavailable or out of stock")
    method = body.payment_method.upper()
    if method == "BALANCE":
        try:
            ref, cp, after = create_balance_order_web(user["telegram_id"], x, body.quantity)
        except ValueError as e:
            if str(e) == "INSUFFICIENT_BALANCE":
                raise HTTPException(400, "Insufficient balance")
            if str(e) == "OUT_OF_STOCK":
                raise HTTPException(400, "Out of stock")
            raise
        # OWN → deliver immediately
        delivery = None
        status = "PAID"
        if str(x.get("supplier")).upper() == "OWN":
            try:
                delivery = finish_own_delivery(ref, body.quantity)
                status = "COMPLETED"
            except Exception as e:
                print("OWN delivery error:", e)
        else:
            # Mark for bot delivery worker / set DELIVERING — supplier orders
            # need the bot's deliver_order. We leave status PAID; bot poller
            # or manual retry can pick up. Optionally call supplier here.
            status = "PAID"
        return {
            "order_ref": ref,
            "status": status,
            "amount": float(cp),
            "balance_after": float(after),
            "delivery": delivery,
            "message": "Order placed" + (" and delivered" if delivery else ". Delivery in progress."),
        }
    # DIRECT payment
    sid = str(x.get("product_id") or x.get("service_id"))
    supplier = str(x.get("supplier", "AIVERSE")).upper()
    product_key = str(x.get("product_key"))
    name = str(x.get("name", "Unknown"))
    qty = max(1, min(int(body.quantity), 100))
    cp = money(customer_price(x) * qty)
    sp = Decimal("0") if supplier == "OWN" else money(x.get("price", 0) * qty)
    ref = new_ref("ORD")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        if supplier == "OWN":
            _reserve_own_stock_tx(c, product_key, qty, ref)
        c.execute(
            """INSERT INTO orders(
                 order_ref,telegram_id,service_id,product_name,quantity,supplier_price,
                 customer_price,status,payment_method,supplier,product_key,source
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ref, str(user["telegram_id"]), sid, name, qty, float(sp), float(cp),
             "CREATING_INVOICE", "DIRECT", supplier, product_key, "web"),
        )
        c.commit()
    except ValueError as e:
        c.rollback()
        if str(e) == "OUT_OF_STOCK":
            raise HTTPException(400, "Out of stock")
        raise
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    try:
        iid, uid = create_invoice(user["telegram_id"], cp)
    except Exception as e:
        c = db()
        c.execute(
            "UPDATE orders SET status='INVOICE_FAILED',delivery_error=?,updated_at=CURRENT_TIMESTAMP WHERE order_ref=?",
            (str(e)[:500], ref),
        )
        c.execute(
            """UPDATE custom_stock SET status='AVAILABLE',order_ref=NULL,reserved_at=NULL
               WHERE order_ref=? AND status='RESERVED'""",
            (ref,),
        )
        c.commit()
        c.close()
        raise HTTPException(502, f"Invoice failed: {e}")
    c = db()
    c.execute(
        """UPDATE orders SET invoice_id=?,payment_uid=?,status='PENDING_PAYMENT',
           updated_at=CURRENT_TIMESTAMP WHERE order_ref=?""",
        (iid, uid, ref),
    )
    c.commit()
    c.close()
    return {
        "order_ref": ref,
        "status": "PENDING_PAYMENT",
        "amount": float(cp),
        "invoice_id": iid,
        "payment_uid": uid,
        "message": "Pay exact amount to the UID, then submit TX ID.",
    }


@app.post("/api/orders/verify")
def verify_order_payment(body: VerifyPayment, user: dict = Depends(get_current_user)):
    c = db()
    row = c.execute(
        "SELECT * FROM orders WHERE order_ref=? AND telegram_id=?",
        (body.reference.upper(), str(user["telegram_id"])),
    ).fetchone()
    c.close()
    if not row:
        raise HTTPException(404, "Order not found")
    if row["status"] != "PENDING_PAYMENT":
        return {"status": row["status"], "message": "Already processed"}
    ok, data = verify_payhub(row["invoice_id"], body.txid)
    if not ok:
        raise HTTPException(400, "Payment not verified yet")
    # mark paid
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        r = c.execute("SELECT * FROM orders WHERE order_ref=?", (body.reference.upper(),)).fetchone()
        if r["status"] != "PENDING_PAYMENT":
            c.commit()
            return {"status": r["status"], "message": "Already processed"}
        if not _claim_payment(c, body.txid, r["invoice_id"], "ORDER", r["order_ref"]):
            c.rollback()
            raise HTTPException(400, "Payment already used")
        c.execute(
            """UPDATE orders SET txid=?,status='PAID',paid_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE order_ref=?""",
            (body.txid, body.reference.upper()),
        )
        c.commit()
    finally:
        c.close()
    delivery = None
    if str(row["supplier"] or "").upper() == "OWN":
        try:
            delivery = finish_own_delivery(body.reference.upper(), int(row["quantity"] or 1))
        except Exception as e:
            print("OWN delivery after verify:", e)
    return {
        "status": "COMPLETED" if delivery else "PAID",
        "delivery": delivery,
        "message": "Payment verified",
    }


# ---------------------------------------------------------------------------
# Topup / Add Funds
# ---------------------------------------------------------------------------
@app.post("/api/topup")
def create_topup(body: TopupCreate, user: dict = Depends(get_current_user)):
    amount = money(body.amount)
    if amount < money("0.01"):
        raise HTTPException(400, "Minimum topup is 0.01 USDT")
    ref = new_ref("TOP")
    try:
        iid, uid = create_invoice(user["telegram_id"], amount)
    except Exception as e:
        raise HTTPException(502, f"Invoice failed: {e}")
    c = db()
    c.execute(
        """INSERT INTO topups(topup_ref,telegram_id,amount,invoice_id,payment_uid,status,source)
           VALUES(?,?,?,?,?,'PENDING','web')""",
        (ref, str(user["telegram_id"]), float(amount), iid, uid),
    )
    c.commit()
    c.close()
    return {
        "topup_ref": ref,
        "amount": float(amount),
        "invoice_id": iid,
        "payment_uid": uid,
        "status": "PENDING",
        "message": "Pay exact USDT amount, then submit transaction ID.",
    }


@app.post("/api/topup/verify")
def verify_topup_api(body: VerifyPayment, user: dict = Depends(get_current_user)):
    c = db()
    row = c.execute(
        "SELECT * FROM topups WHERE topup_ref=? AND telegram_id=?",
        (body.reference.upper(), str(user["telegram_id"])),
    ).fetchone()
    c.close()
    if not row:
        raise HTTPException(404, "Topup not found")
    if row["status"] == "PAID":
        return {"status": "PAID", "balance": float(get_balance(user["telegram_id"]))}
    ok, data = verify_payhub(row["invoice_id"], body.txid)
    if not ok:
        raise HTTPException(400, "Payment not verified yet")
    result, after = _topup_paid_once(body.reference.upper(), body.txid, data)
    if result not in {"ok", "duplicate"}:
        raise HTTPException(400, f"Verification failed: {result}")
    return {
        "status": "PAID",
        "balance": float(after or get_balance(user["telegram_id"])),
        "message": "Funds added",
    }


@app.get("/api/topup/pending")
def pending_topups(user: dict = Depends(get_current_user)):
    c = db()
    rows = c.execute(
        "SELECT * FROM topups WHERE telegram_id=? AND status='PENDING' ORDER BY id DESC LIMIT 10",
        (str(user["telegram_id"]),),
    ).fetchall()
    c.close()
    return {"topups": [row_to_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Payment webhook (shared with bot)
# ---------------------------------------------------------------------------
@app.post("/api/v1/payments/webhook")
@app.post("/webhook")
@app.post("/ipn")
async def payment_webhook(
    request: Request,
    x_webhook_secret: str = Header(default="", alias="X-Webhook-Secret"),
):
    if not hmac.compare_digest(x_webhook_secret, PAYMENT_WEBHOOK_SECRET):
        raise HTTPException(401, "invalid-secret")
    p = await request.json()
    event = str(p.get("event", "")).upper()
    status_ = str(p.get("status", "")).upper()
    if event and event not in {"PAYMENT_PAID", "PAYMENT_SUCCESS", "PAYMENT_COMPLETED"}:
        return {"result": "ignored"}
    if status_ and status_ not in _PAYMENT_OK:
        return {"result": "not-paid"}
    iid = p.get("invoice_id") or p.get("invoiceId") or p.get("invoice_no")
    if not iid:
        return {"result": "unknown-invoice"}
    iid = str(iid)
    payment_id = str(p.get("txid") or p.get("order_id") or p.get("tx_id") or f"WEBHOOK:{iid}")
    c = db()
    top = c.execute("SELECT * FROM topups WHERE invoice_id=?", (iid,)).fetchone()
    order_row = c.execute("SELECT * FROM orders WHERE invoice_id=?", (iid,)).fetchone()
    c.close()
    if top:
        result, after = _topup_paid_once(top["topup_ref"], payment_id, p)
        return {"result": result}
    if order_row and order_row["payment_method"] == "DIRECT":
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM orders WHERE order_ref=?", (order_row["order_ref"],)).fetchone()
            if row["status"] in {"PAID", "DELIVERING", "COMPLETED", "DELIVERY_REVIEW", "DELIVERY_FAILED"}:
                c.commit()
                return {"result": "duplicate"}
            if row["status"] != "PENDING_PAYMENT":
                c.rollback()
                return {"result": "invalid-status"}
            if not _claim_payment(c, payment_id, row["invoice_id"], "ORDER", row["order_ref"]):
                c.rollback()
                return {"result": "payment-already-used"}
            c.execute(
                """UPDATE orders SET txid=?,status='PAID',paid_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE order_ref=?""",
                (payment_id, order_row["order_ref"]),
            )
            c.commit()
        finally:
            c.close()
        if str(order_row["supplier"] or "").upper() == "OWN":
            try:
                finish_own_delivery(order_row["order_ref"], int(order_row["quantity"] or 1))
            except Exception as e:
                print("webhook OWN delivery:", e)
        return {"result": "ok"}
    return {"result": "unknown-invoice"}


# ---------------------------------------------------------------------------
# Admin APIs
# ---------------------------------------------------------------------------
@app.get("/api/admin/stats")
def admin_stats(admin: dict = Depends(require_admin)):
    c = db()
    users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    bal = c.execute("SELECT COALESCE(SUM(balance),0) s FROM users").fetchone()["s"]
    orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    completed = c.execute("SELECT COUNT(*) n FROM orders WHERE status='COMPLETED'").fetchone()["n"]
    pending = c.execute("SELECT COUNT(*) n FROM topups WHERE status='PENDING'").fetchone()["n"]
    direct_pending = c.execute("SELECT COUNT(*) n FROM orders WHERE status='PENDING_PAYMENT'").fetchone()["n"]
    review = c.execute(
        "SELECT COUNT(*) n FROM orders WHERE status IN ('DELIVERY_REVIEW','DELIVERY_FAILED')"
    ).fetchone()["n"]
    c.close()
    return {
        "users": users,
        "total_balance": float(bal),
        "orders": orders,
        "completed": completed,
        "pending_topups": pending,
        "pending_direct": direct_pending,
        "delivery_review": review,
    }


@app.get("/api/admin/users")
def admin_users_list(
    admin: dict = Depends(require_admin),
    q: str = "",
    limit: int = Query(40, le=100),
):
    from .auth.email_auth import list_web_users, get_web_user_by_key

    if q:
        users = search_users(q, limit)
    else:
        users = list_users(limit)
    # Attach email labels for web users
    for u in users:
        wu = get_web_user_by_key(str(u.get("telegram_id") or ""))
        if wu:
            u["email"] = wu.get("email")
            u["auth_type"] = "email"
        else:
            u["auth_type"] = u.get("auth_type") or "telegram"
    web = list_web_users(limit)
    return {"users": users, "web_users": web}


@app.get("/api/admin/users/{telegram_id}")
def admin_user_detail(telegram_id: str, admin: dict = Depends(require_admin)):
    user = get_user(telegram_id)
    if not user:
        raise HTTPException(404, "User not found")
    stats = user_stats(telegram_id)
    txs = get_transactions(telegram_id, 15)
    c = db()
    orders = c.execute(
        "SELECT * FROM orders WHERE telegram_id=? ORDER BY id DESC LIMIT 15",
        (str(telegram_id),),
    ).fetchall()
    c.close()
    return {
        "user": user,
        **stats,
        "transactions": txs,
        "orders": [row_to_dict(r) for r in orders],
    }


@app.post("/api/admin/balance/add")
def admin_bal_add(body: AdminBalanceBody, admin: dict = Depends(require_admin)):
    after = admin_add_balance(body.telegram_id, body.amount, body.note or "Admin web credit")
    return {"ok": True, "new_balance": float(after)}


@app.post("/api/admin/balance/remove")
def admin_bal_rm(body: AdminBalanceBody, admin: dict = Depends(require_admin)):
    try:
        after = admin_remove_balance(body.telegram_id, body.amount, body.note or "Admin web debit")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "new_balance": float(after)}


@app.post("/api/admin/users/ban")
def admin_ban(body: AdminBanBody, admin: dict = Depends(require_admin)):
    if body.telegram_id in ADMIN_IDS:
        raise HTTPException(400, "Cannot ban admin")
    ban_user(body.telegram_id, body.reason)
    return {"ok": True}


@app.post("/api/admin/users/unban")
def admin_unban(body: AdminBanBody, admin: dict = Depends(require_admin)):
    unban_user(body.telegram_id)
    return {"ok": True}


@app.get("/api/admin/orders")
def admin_orders_list(admin: dict = Depends(require_admin), limit: int = Query(40, le=100)):
    c = db()
    rows = c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return {"orders": [row_to_dict(r) for r in rows]}


@app.get("/api/admin/orders/{order_ref}")
def admin_order_detail(order_ref: str, admin: dict = Depends(require_admin)):
    c = db()
    r = c.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref.upper(),)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "Not found")
    d = row_to_dict(r)
    if d.get("delivery_payload"):
        try:
            d["delivery"] = json.loads(d["delivery_payload"])
        except Exception:
            d["delivery"] = [d["delivery_payload"]]
    return d


@app.post("/api/admin/orders/{order_ref}/retry-delivery")
def admin_retry_delivery(order_ref: str, admin: dict = Depends(require_admin)):
    c = db()
    r = c.execute("SELECT * FROM orders WHERE order_ref=?", (order_ref.upper(),)).fetchone()
    if not r:
        c.close()
        raise HTTPException(404, "Not found")
    if r["status"] not in {"PAID", "DELIVERY_FAILED", "DELIVERY_REVIEW"}:
        c.close()
        raise HTTPException(400, f"Cannot retry from {r['status']}")
    c.execute(
        "UPDATE orders SET status='PAID',delivery_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE order_ref=?",
        (order_ref.upper(),),
    )
    c.commit()
    c.close()
    if str(r["supplier"] or "").upper() == "OWN":
        try:
            delivery = finish_own_delivery(order_ref.upper(), int(r["quantity"] or 1))
            return {"ok": True, "status": "COMPLETED", "delivery": delivery}
        except Exception as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "status": "PAID", "message": "Reset to PAID — bot delivery worker will process"}


@app.get("/api/admin/products/own")
def admin_own_products(admin: dict = Depends(require_admin)):
    return {"products": own_services(include_disabled=True)}


@app.post("/api/admin/products/own")
def admin_create_own(body: AdminProductCreate, admin: dict = Depends(require_admin)):
    key = "OWN-" + uuid.uuid4().hex[:10].upper()
    c = db()
    c.execute(
        """INSERT INTO custom_products(product_key,name,price,validity,warranty,enabled)
           VALUES(?,?,?,?,?,1)""",
        (key, body.name.strip(), float(money(body.price)), body.validity.strip(), body.warranty.strip() or "No Warranty"),
    )
    c.commit()
    c.close()
    return {"product_key": key, "ok": True}


@app.post("/api/admin/products/own/stock")
def admin_add_stock(body: AdminStockAdd, admin: dict = Depends(require_admin)):
    clean = []
    seen = set()
    for item in body.payloads:
        v = str(item).strip()
        if v and v not in seen:
            clean.append(v)
            seen.add(v)
    added = skipped = 0
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        for payload in clean:
            existing = c.execute(
                "SELECT id,status FROM custom_stock WHERE product_key=? AND payload=?",
                (body.product_key, payload),
            ).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO custom_stock(product_key,payload,status) VALUES(?,?,'AVAILABLE')",
                    (body.product_key, payload),
                )
                added += 1
            elif str(existing["status"]).upper() in {"AVAILABLE", "RESERVED"}:
                skipped += 1
            else:
                c.execute(
                    """UPDATE custom_stock SET status='AVAILABLE',order_ref=NULL,
                       reserved_at=NULL,delivered_at=NULL WHERE id=?""",
                    (int(existing["id"]),),
                )
                added += 1
        c.commit()
    finally:
        c.close()
    return {"added": added, "skipped": skipped}


@app.get("/api/admin/export/users")
def admin_export_users(admin: dict = Depends(require_admin)):
    c = db()
    rows = c.execute(
        "SELECT telegram_id,username,first_name,balance,joined_at,last_seen FROM users ORDER BY joined_at"
    ).fetchall()
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["telegram_id", "username", "first_name", "balance", "joined_at", "last_seen"])
    for r in rows:
        w.writerow([r["telegram_id"], r["username"], r["first_name"], r["balance"], r["joined_at"], r["last_seen"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users_export.csv"},
    )


@app.get("/api/admin/export/orders")
def admin_export_orders(admin: dict = Depends(require_admin)):
    c = db()
    rows = c.execute(
        """SELECT order_ref,telegram_id,product_name,quantity,customer_price,status,
                  payment_method,supplier,created_at,paid_at,delivered_at
           FROM orders ORDER BY id DESC LIMIT 500"""
    ).fetchall()
    c.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "order_ref", "telegram_id", "product_name", "quantity", "customer_price",
        "status", "payment_method", "supplier", "created_at", "paid_at", "delivered_at",
    ])
    for r in rows:
        w.writerow([
            r["order_ref"], r["telegram_id"], r["product_name"], r["quantity"],
            r["customer_price"], r["status"], r["payment_method"], r["supplier"],
            r["created_at"], r["paid_at"], r["delivered_at"],
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=orders_export.csv"},
    )


@app.post("/api/admin/backup")
def admin_backup(admin: dict = Depends(require_admin)):
    ok, msg = backup_database("admin-web")
    return {"ok": ok, "message": msg}


@app.get("/api/admin/payments/pending")
def admin_pending_payments(admin: dict = Depends(require_admin)):
    c = db()
    tops = c.execute(
        "SELECT * FROM topups WHERE status='PENDING' ORDER BY id DESC LIMIT 50"
    ).fetchall()
    orders = c.execute(
        """SELECT * FROM orders WHERE payment_method='DIRECT' AND status='PENDING_PAYMENT'
           ORDER BY id DESC LIMIT 50"""
    ).fetchall()
    c.close()
    return {
        "topups": [row_to_dict(r) for r in tops],
        "orders": [row_to_dict(r) for r in orders],
    }


@app.post("/api/admin/orders/{order_ref}/cancel")
def admin_cancel_order(order_ref: str, admin: dict = Depends(require_admin)):
    """Cancel unpaid / pre-delivery order and release OWN stock."""
    ref = order_ref.upper().strip()
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM orders WHERE order_ref=?", (ref,)).fetchone()
        if not row:
            c.rollback()
            raise HTTPException(404, "Order not found")
        st = str(row["status"] or "")
        if st in {"COMPLETED", "REFUNDED"}:
            c.rollback()
            raise HTTPException(400, f"Cannot cancel {st}")
        c.execute(
            """UPDATE custom_stock SET status='AVAILABLE',order_ref=NULL,reserved_at=NULL
               WHERE order_ref=? AND status='RESERVED'""",
            (ref,),
        )
        c.execute(
            """UPDATE orders SET status='CANCELLED',delivery_error=?,updated_at=CURRENT_TIMESTAMP
               WHERE order_ref=?""",
            ("Cancelled by admin", ref),
        )
        c.commit()
        return {"ok": True, "order_ref": ref, "status": "CANCELLED"}
    except HTTPException:
        raise
    except Exception as e:
        c.rollback()
        raise HTTPException(500, str(e))
    finally:
        c.close()


@app.post("/api/admin/products/own/{product_key}/delete")
def admin_delete_own_product(product_key: str, admin: dict = Depends(require_admin)):
    """Soft-disable own product (does not delete delivered history)."""
    c = db()
    row = c.execute(
        "SELECT product_key FROM custom_products WHERE product_key=?", (product_key,)
    ).fetchone()
    if not row:
        c.close()
        raise HTTPException(404, "Product not found")
    c.execute(
        "UPDATE custom_products SET enabled=0,updated_at=CURRENT_TIMESTAMP WHERE product_key=?",
        (product_key,),
    )
    c.commit()
    c.close()
    return {"ok": True, "product_key": product_key, "enabled": False}


@app.get("/api/admin/db/integrity")
def admin_db_integrity(admin: dict = Depends(require_admin)):
    from .core.db import integrity_check

    ok, msg = integrity_check()
    return {"ok": ok, "message": msg, "db_path": os.path.abspath(DB)}


# ---------------------------------------------------------------------------
# Serve frontend static files (if present)
# ---------------------------------------------------------------------------
_frontend = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
_frontend = os.path.abspath(_frontend)
if os.path.isdir(_frontend):
    assets = os.path.join(_frontend, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

@app.get("/")
def serve_index():
    index = os.path.join(_frontend, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"message": "Premium Hub API", "docs": "/docs"}


@app.get("/{path:path}")
def spa_fallback(path: str):
    # Don't intercept API
    if path.startswith("api/") or path.startswith("docs") or path.startswith("openapi"):
        raise HTTPException(404)
    index = os.path.join(_frontend, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    raise HTTPException(404)
