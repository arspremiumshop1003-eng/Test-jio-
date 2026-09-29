"""
Database layer — identical schema to the original bot.
Migration-safe: only ADD columns, never DROP.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Optional, Set, Tuple

from .config import DB, BACKUP_DIR, BACKUP_KEEP


def db() -> sqlite3.Connection:
    """
    Production-safe SQLite connection.
    - WAL: concurrent readers + one writer
    - busy_timeout: wait on lock instead of failing immediately
    - synchronous=NORMAL: safe with WAL, better perf than FULL
    - foreign_keys ON
    Never use this connection across threads without your own lock.
    """
    c = sqlite3.connect(DB, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=60000")
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute("PRAGMA wal_autocheckpoint=1000")
    except Exception:
        pass
    return c


def db_transaction():
    """
    Context manager for atomic writes (balance, orders, stock).
    Usage:
        with db_transaction() as c:
            c.execute(...)
    Commits on success, rolls back on error.
    """
    from contextlib import contextmanager

    @contextmanager
    def _tx():
        c = db()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except Exception:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            c.close()

    return _tx()


def integrity_check() -> Tuple[bool, str]:
    """Quick corruption check. Does not modify data."""
    try:
        c = db()
        row = c.execute("PRAGMA integrity_check").fetchone()
        c.close()
        msg = str(row[0] if row else "unknown")
        return msg.lower() == "ok", msg
    except Exception as e:
        return False, str(e)


def _columns(c: sqlite3.Connection, table: str) -> Set[str]:
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(c: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(c, table):
        c.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db() -> None:
    c = db()
    c.execute("PRAGMA journal_mode=WAL")

    c.execute(
        """CREATE TABLE IF NOT EXISTS users(
            telegram_id TEXT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance REAL DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_ref TEXT UNIQUE,
            telegram_id TEXT,
            service_id TEXT,
            product_name TEXT,
            quantity INTEGER DEFAULT 1,
            supplier_price REAL,
            customer_price REAL,
            status TEXT,
            invoice_id TEXT UNIQUE,
            payment_uid TEXT,
            txid TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT,
            kind TEXT,
            amount REAL,
            balance_before REAL,
            balance_after REAL,
            reference TEXT UNIQUE,
            status TEXT,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS topups(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topup_ref TEXT UNIQUE,
            telegram_id TEXT,
            amount REAL,
            invoice_id TEXT UNIQUE,
            payment_uid TEXT,
            txid TEXT,
            status TEXT DEFAULT 'PENDING',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_states(
            telegram_id TEXT PRIMARY KEY,
            state TEXT,
            payload TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS payment_claims(
            payment_id TEXT PRIMARY KEY,
            invoice_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS supplier_catalog(
            product_key TEXT PRIMARY KEY,
            supplier TEXT NOT NULL,
            product_id TEXT NOT NULL,
            name TEXT,
            price REAL,
            stock INTEGER,
            raw_json TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(supplier, product_id)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS custom_products(
            product_key TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            validity TEXT DEFAULT '',
            warranty TEXT DEFAULT 'No Warranty',
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS custom_stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_key TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT DEFAULT 'AVAILABLE',
            order_ref TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reserved_at TEXT,
            delivered_at TEXT,
            UNIQUE(product_key, payload),
            FOREIGN KEY(product_key) REFERENCES custom_products(product_key)
        )"""
    )
    # Web sessions (optional, for JWT refresh tracking)
    c.execute(
        """CREATE TABLE IF NOT EXISTS web_sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT NOT NULL,
            token_jti TEXT UNIQUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT,
            user_agent TEXT,
            ip TEXT
        )"""
    )

    _add_column(c, "orders", "payment_method TEXT DEFAULT 'BALANCE'")
    _add_column(c, "orders", "delivery_payload TEXT")
    _add_column(c, "orders", "delivery_error TEXT")
    _add_column(c, "orders", "delivery_attempts INTEGER DEFAULT 0")
    _add_column(c, "orders", "paid_at TEXT")
    _add_column(c, "orders", "delivered_at TEXT")
    _add_column(c, "orders", "supplier TEXT DEFAULT 'AIVERSE'")
    _add_column(c, "orders", "product_key TEXT")
    _add_column(c, "orders", "verify_attempts INTEGER DEFAULT 0")
    _add_column(c, "orders", "source TEXT DEFAULT 'telegram'")  # telegram | web
    _add_column(c, "topups", "verify_attempts INTEGER DEFAULT 0")
    _add_column(c, "topups", "source TEXT DEFAULT 'telegram'")
    _add_column(c, "users", "banned INTEGER DEFAULT 0")
    _add_column(c, "users", "ban_reason TEXT DEFAULT ''")
    _add_column(c, "users", "banned_at TEXT")

    c.execute("UPDATE orders SET supplier='AIVERSE' WHERE supplier IS NULL OR TRIM(supplier)=''")
    c.execute(
        """UPDATE orders SET payment_method='DIRECT'
           WHERE invoice_id IS NOT NULL AND TRIM(invoice_id)<>''
             AND (payment_method IS NULL OR payment_method='BALANCE')"""
    )

    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(telegram_id, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_topups_user ON topups(telegram_id, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_topups_status ON topups(status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(telegram_id, id DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_orders_supplier ON orders(supplier, service_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_catalog_supplier ON supplier_catalog(supplier, product_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_stock_product_status ON custom_stock(product_key,status,id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_custom_stock_order ON custom_stock(order_ref,status)")
    c.commit()
    c.close()
    try:
        from ..auth.email_auth import init_email_tables

        init_email_tables()
    except Exception as e:
        print(f"Email tables init: {e}")
    print(f"🗄 Database ready at: {os.path.abspath(DB)}")


def log_error(tag: str, err: Any) -> None:
    try:
        print(f"[ERROR] {tag} | {err}")
    except Exception:
        pass


def backup_database(reason: str = "manual") -> Tuple[bool, str]:
    if not BACKUP_DIR:
        return False, "BACKUP_DIR is not set"
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception as e:
        return False, f"Cannot create BACKUP_DIR: {e}"

    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, f"ars_bot_{stamp}.db")
    latest = os.path.join(BACKUP_DIR, "ars_bot.latest.db")
    src = dst = None
    try:
        src = sqlite3.connect(DB, timeout=30)
        dst = sqlite3.connect(dest, timeout=30)
        src.backup(dst)
        dst.close()
        dst = None
        src.close()
        src = None
        try:
            import shutil
            shutil.copy2(dest, latest)
        except Exception as e:
            log_error("backup_latest_copy", e)
        try:
            files = sorted(
                (
                    os.path.join(BACKUP_DIR, f)
                    for f in os.listdir(BACKUP_DIR)
                    if f.startswith("ars_bot_") and f.endswith(".db")
                ),
                key=lambda p: os.path.getmtime(p),
                reverse=True,
            )
            for old in files[BACKUP_KEEP:]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception as e:
            log_error("backup_prune", e)
        size_kb = os.path.getsize(dest) // 1024
        msg = f"Backup OK ({reason}) → {dest} ({size_kb} KB)"
        print(f"[BACKUP] {msg}")
        return True, msg
    except Exception as e:
        log_error("backup_database", e)
        return False, f"Backup failed: {e}"
    finally:
        try:
            if dst is not None:
                dst.close()
        except Exception:
            pass
        try:
            if src is not None:
                src.close()
        except Exception:
            pass


def get_setting(key: str, default: str = "") -> str:
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (str(key),)).fetchone()
    c.close()
    return str(r["value"]) if r else str(default)


def set_setting_value(key: str, value: Any) -> None:
    c = db()
    c.execute(
        """INSERT INTO settings(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (str(key), str(value)),
    )
    c.commit()
    c.close()


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}
