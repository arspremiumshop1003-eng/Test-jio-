"""
Shared configuration for Telegram Bot + Web API.
All env vars match the original bot.
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Set

from dotenv import load_dotenv

load_dotenv()


def _dec_env(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)).strip() or default)
    except Exception:
        return Decimal(default)


def _resolve_db_path() -> str:
    raw = (os.getenv("DB_FILE") or "ars_bot.db").strip() or "ars_bot.db"
    if os.path.isabs(raw):
        return raw
    volume = (os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR") or "").strip()
    if volume:
        try:
            os.makedirs(volume, exist_ok=True)
        except Exception:
            pass
        return os.path.join(volume, raw)
    return raw


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
BASE_URL = os.getenv("BASE_URL", "https://aiversehub.store").rstrip("/")

ELITE_BASE_URL = os.getenv("ELITE_BASE_URL", "https://elite-tools-store.up.railway.app").rstrip("/")
ELITE_API_KEY = os.getenv("ELITE_API_KEY", "").strip()
ELITE_PRODUCTS_PATH = os.getenv("ELITE_PRODUCTS_PATH", "/api/reseller/products").strip()
ELITE_BALANCE_PATH = os.getenv("ELITE_BALANCE_PATH", "/api/reseller/balance").strip()
ELITE_ORDER_PATH = os.getenv("ELITE_ORDER_PATH", "/api/reseller/buy").strip()

SHOPBOT_BASE_URL = os.getenv("SHOPBOT_BASE_URL", "https://shopbot.00969600.xyz/shop-api/v1").rstrip("/")
SHOPBOT_API_KEY = os.getenv("SHOPBOT_API_KEY", "").strip()

PAYMENT_BASE_URL = os.getenv("PAYMENT_BASE_URL", "https://payhub-railway-production.up.railway.app").rstrip("/")
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY", "").strip()
PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "").strip()

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", "8080"))

FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "@free_internet_config_bd").strip()
FORCE_GROUP = os.getenv("FORCE_GROUP", "@gemini_vr_Chat").strip()
FORCE_CHANNEL_URL = os.getenv("FORCE_CHANNEL_URL", "https://t.me/free_internet_config_bd").strip()
FORCE_GROUP_URL = os.getenv("FORCE_GROUP_URL", "https://t.me/gemini_vr_Chat").strip()
LOG_CHAT_ID = os.getenv("LOG_CHAT_ID", FORCE_GROUP).strip()

ADMIN_IDS: Set[str] = {
    x.strip()
    for x in os.getenv("ADMIN_IDS", "8908955171,5446536002").split(",")
    if x.strip()
}

MIN_TOPUP = Decimal("0.01")

MARKUP_USDT = _dec_env("MARKUP_USDT", "0.20")
MARKUP_PERCENT = _dec_env("MARKUP_PERCENT", "0")
AIVERSE_MARKUP_USDT = _dec_env("AIVERSE_MARKUP_USDT", str(MARKUP_USDT))
ELITE_MARKUP_USDT = _dec_env("ELITE_MARKUP_USDT", str(MARKUP_USDT))
SHOPBOT_MARKUP_USDT = _dec_env("SHOPBOT_MARKUP_USDT", "0")
AIVERSE_MARKUP_PERCENT = _dec_env("AIVERSE_MARKUP_PERCENT", str(MARKUP_PERCENT))
ELITE_MARKUP_PERCENT = _dec_env("ELITE_MARKUP_PERCENT", str(MARKUP_PERCENT))
_shopbot_pct_default = str(MARKUP_PERCENT) if MARKUP_PERCENT > 0 else "20"
SHOPBOT_MARKUP_PERCENT = _dec_env("SHOPBOT_MARKUP_PERCENT", _shopbot_pct_default)

DB = _resolve_db_path()
PRODUCT_CACHE_SECONDS = max(0, int(os.getenv("PRODUCT_CACHE_SECONDS", "8")))
PRODUCTS_PER_PAGE = max(5, min(40, int(os.getenv("PRODUCTS_PER_PAGE", "20"))))
FIRST_PAGE_PRODUCTS = max(4, min(12, int(os.getenv("FIRST_PAGE_PRODUCTS", "8"))))

BACKUP_DIR = (os.getenv("BACKUP_DIR") or os.getenv("DB_BACKUP_DIR") or "").strip()
BACKUP_INTERVAL_MINUTES = max(5, int(os.getenv("BACKUP_INTERVAL_MINUTES", "30")))
BACKUP_KEEP = max(1, min(48, int(os.getenv("BACKUP_KEEP", "12"))))

STOCK_RESTOCK_NOTIFY = (
    os.getenv("STOCK_RESTOCK_NOTIFY", "1").strip().lower() not in {"0", "false", "off", "no"}
)
STOCK_RESTOCK_COOLDOWN_SECONDS = max(60, int(os.getenv("STOCK_RESTOCK_COOLDOWN_SECONDS", "300")))

PAYMENT_MAX_VERIFY_ATTEMPTS = max(1, int(os.getenv("PAYMENT_MAX_VERIFY_ATTEMPTS", "8")))
PAYMENT_PENDING_EXPIRE_MINUTES = max(5, int(os.getenv("PAYMENT_PENDING_EXPIRE_MINUTES", "45")))
PAYMENT_POLL_INTERVAL_SECONDS = max(20, int(os.getenv("PAYMENT_POLL_INTERVAL_SECONDS", "60")))

SHOP_NAME = os.getenv("SHOP_NAME", "Premium Hub").strip() or "Premium Hub"
SUPPORT_URL = os.getenv("SUPPORT_URL", "").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "lostdopay").strip() or "lostdopay"
SUPPORT_TEXT = os.getenv(
    "SUPPORT_TEXT",
    "Need Help?\n\nContact Support and share your User ID for faster help.",
).strip()

FEATURED_PRODUCT_KEYWORDS = tuple(
    k.strip().casefold()
    for k in os.getenv("FEATURED_PRODUCT_KEYWORDS", "gemini,jio").split(",")
    if k.strip()
)
MAIN_PRODUCT_KEYWORDS = tuple(
    k.strip().casefold()
    for k in os.getenv(
        "MAIN_PRODUCT_KEYWORDS",
        "gemini jio,jio 18,gemini 18m jio,gemini jio 18",
    ).split(",")
    if k.strip()
)

# Web / JWT
JWT_SECRET = os.getenv("JWT_SECRET", BOT_TOKEN or "change-me-in-production").strip()
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "168"))  # 7 days
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "*").split(",")
    if o.strip()
]

# Headers for suppliers / payment
AHEAD = {"X-API-Key": API_KEY}
EHEAD = {"X-API-Key": ELITE_API_KEY}
SHEAD = {
    "X-Shop-API-Key": SHOPBOT_API_KEY,
    "Authorization": f"Bearer {SHOPBOT_API_KEY}",
    "Content-Type": "application/json",
}
PHEAD = {"X-API-Key": PAYMENT_API_KEY, "Content-Type": "application/json"}
_PAYMENT_OK = {"PAID", "SUCCESS", "COMPLETED", "CONFIRMED"}
_UPSTREAM_SUPPLIERS = ("AIVERSE", "ELITE", "SHOPBOT")
TG = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
