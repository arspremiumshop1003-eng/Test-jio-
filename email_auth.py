"""
Email registration / login for website users.

Unified identity:
  - All balances/orders still live in `users` keyed by `telegram_id` column
  - Email-only users get a stable synthetic id: W + sha256(email)[:15]
  - Telegram users keep real numeric telegram_id
  - Optional link: same person can attach email to telegram account later

Migration-safe: only ADD tables/columns. Never deletes users or balances.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

from fastapi import HTTPException

from ..core.db import db, row_to_dict
from ..core.users import ensure_user, get_user, is_banned
from .jwt_auth import create_access_token

# PBKDF2 — no extra dependency required
_HASH_ITERS = 120_000
_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def normalize_email(email: str) -> str:
    return str(email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(normalize_email(email)))


def user_key_for_email(email: str) -> str:
    """Stable synthetic telegram_id for pure web users (fits TEXT PK)."""
    e = normalize_email(email)
    h = hashlib.sha256(e.encode("utf-8")).hexdigest()[:15]
    return f"W{h}"


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), _HASH_ITERS
    )
    return f"pbkdf2_sha256${_HASH_ITERS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt, hexhash = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters_s)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt.encode("utf-8"), iters
        )
        return hmac.compare_digest(dk.hex(), hexhash)
    except Exception:
        return False


def init_email_tables() -> None:
    c = db()
    c.execute(
        """CREATE TABLE IF NOT EXISTS web_users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            user_key TEXT NOT NULL,
            telegram_id TEXT,
            email_verified INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS email_tokens(
            token TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            purpose TEXT NOT NULL,
            expires_at REAL NOT NULL,
            used INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_web_users_key ON web_users(user_key)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_web_users_email ON web_users(email)")
    # Optional email on core users row (migration-safe)
    try:
        cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
        if "email" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
        if "auth_type" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN auth_type TEXT DEFAULT 'telegram'")
    except Exception:
        pass
    c.commit()
    c.close()
    init_identity_map()


def _make_token(email: str, purpose: str, hours: float = 24) -> str:
    token = secrets.token_urlsafe(32)
    exp = time.time() + hours * 3600
    c = db()
    c.execute(
        "INSERT INTO email_tokens(token,email,purpose,expires_at) VALUES(?,?,?,?)",
        (token, normalize_email(email), purpose, exp),
    )
    c.commit()
    c.close()
    return token


def _consume_token(token: str, purpose: str) -> str:
    """Returns email if valid; marks used."""
    c = db()
    row = c.execute(
        "SELECT * FROM email_tokens WHERE token=? AND purpose=?",
        (token, purpose),
    ).fetchone()
    if not row:
        c.close()
        raise HTTPException(400, "Invalid or expired token")
    if int(row["used"] or 0) == 1:
        c.close()
        raise HTTPException(400, "Token already used")
    if float(row["expires_at"]) < time.time():
        c.close()
        raise HTTPException(400, "Token expired")
    c.execute("UPDATE email_tokens SET used=1 WHERE token=?", (token,))
    c.commit()
    email = str(row["email"])
    c.close()
    return email


def get_web_user_by_email(email: str) -> Optional[dict]:
    c = db()
    r = c.execute(
        "SELECT * FROM web_users WHERE email=?", (normalize_email(email),)
    ).fetchone()
    c.close()
    return row_to_dict(r)


def get_web_user_by_key(user_key: str) -> Optional[dict]:
    c = db()
    r = c.execute(
        "SELECT * FROM web_users WHERE user_key=?", (str(user_key),)
    ).fetchone()
    c.close()
    return row_to_dict(r)


def register_email(
    email: str,
    password: str,
    name: str = "",
    phone: str = "",
) -> dict:
    email = normalize_email(email)
    if not valid_email(email):
        raise HTTPException(400, "Invalid email address")
    if len(password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if get_web_user_by_email(email):
        raise HTTPException(400, "Email already registered")

    key = user_key_for_email(email)
    # Ensure wallet row exists (same system as Telegram users)
    user = ensure_user(key, username="", first_name=name or email.split("@")[0])
    c = db()
    try:
        c.execute(
            """UPDATE users SET email=?, auth_type='email', first_name=COALESCE(NULLIF(?,''), first_name)
               WHERE telegram_id=?""",
            (email, name or "", key),
        )
        c.execute(
            """INSERT INTO web_users(email,password_hash,name,phone,user_key,email_verified,status)
               VALUES(?,?,?,?,?,0,'active')""",
            (email, hash_password(password), name.strip(), phone.strip(), key),
        )
        c.commit()
    except Exception as e:
        c.rollback()
        raise HTTPException(400, f"Registration failed: {e}")
    finally:
        c.close()

    # Email verification optional — auto-verify if SMTP not configured
    require_verify = os.getenv("REQUIRE_EMAIL_VERIFY", "0").strip() in {"1", "true", "yes"}
    verify_token = None
    if require_verify:
        verify_token = _make_token(email, "verify", hours=48)
        _try_send_email(
            email,
            "Verify your Premium Hub account",
            f"Your verification code/token:\n\n{verify_token}\n\nOr open: /verify-email?token={verify_token}",
        )
    else:
        c = db()
        c.execute("UPDATE web_users SET email_verified=1 WHERE email=?", (email,))
        c.commit()
        c.close()

    _set_identity("email", email, key)
    token = create_access_token(key, extra={"auth": "email", "email": email})
    profile = _public_profile(key, email=email, name=name)
    out = {
        "access_token": token,
        "token_type": "bearer",
        "user": profile,
        "message": "Account created",
    }
    if verify_token and os.getenv("DEV_SHOW_EMAIL_TOKEN", "").strip() in {"1", "true"}:
        out["verify_token"] = verify_token
    return out


def login_email(email: str, password: str) -> dict:
    email = normalize_email(email)
    wu = get_web_user_by_email(email)
    if not wu:
        raise HTTPException(401, "Invalid email or password")
    if str(wu.get("status") or "") == "banned":
        raise HTTPException(403, "Account banned")
    if not verify_password(password, wu["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    require_verify = os.getenv("REQUIRE_EMAIL_VERIFY", "0").strip() in {"1", "true", "yes"}
    if require_verify and not int(wu.get("email_verified") or 0):
        raise HTTPException(403, "Email not verified")

    key = str(wu["user_key"])
    if is_banned(key):
        raise HTTPException(403, "Account banned")
    ensure_user(key, first_name=wu.get("name") or "")
    token = create_access_token(key, extra={"auth": "email", "email": email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _public_profile(key, email=email, name=wu.get("name") or ""),
    }


def verify_email_token(token: str) -> dict:
    email = _consume_token(token, "verify")
    c = db()
    c.execute("UPDATE web_users SET email_verified=1,updated_at=CURRENT_TIMESTAMP WHERE email=?", (email,))
    c.commit()
    c.close()
    return {"ok": True, "email": email, "message": "Email verified"}


def request_password_reset(email: str) -> dict:
    email = normalize_email(email)
    wu = get_web_user_by_email(email)
    # Always same response (no email enumeration)
    msg = "If that email exists, a reset token was generated."
    if not wu:
        return {"ok": True, "message": msg}
    token = _make_token(email, "reset", hours=2)
    _try_send_email(
        email,
        "Password reset — Premium Hub",
        f"Reset token (valid 2 hours):\n\n{token}\n\nUse it on the website Forgot Password form.",
    )
    out = {"ok": True, "message": msg}
    if os.getenv("DEV_SHOW_EMAIL_TOKEN", "").strip() in {"1", "true"}:
        out["reset_token"] = token
    return out


def reset_password(token: str, new_password: str) -> dict:
    if len(new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    email = _consume_token(token, "reset")
    c = db()
    c.execute(
        """UPDATE web_users SET password_hash=?,updated_at=CURRENT_TIMESTAMP WHERE email=?""",
        (hash_password(new_password), email),
    )
    c.commit()
    c.close()
    return {"ok": True, "message": "Password updated. You can login now."}


def _public_profile(user_key: str, email: str = "", name: str = "") -> dict:
    u = get_user(user_key) or {}
    return {
        "telegram_id": user_key,
        "user_key": user_key,
        "email": email or u.get("email") or "",
        "username": u.get("username") or "",
        "first_name": name or u.get("first_name") or "",
        "balance": float(u.get("balance") or 0),
        "auth_type": u.get("auth_type") or ("email" if str(user_key).startswith("W") else "telegram"),
    }


def _try_send_email(to: str, subject: str, body: str) -> bool:
    """Optional SMTP. If not configured, logs only."""
    host = os.getenv("SMTP_HOST", "").strip()
    if not host:
        print(f"[EMAIL] (no SMTP) to={to} subject={subject}")
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText

        port = int(os.getenv("SMTP_PORT", "587"))
        user = os.getenv("SMTP_USER", "")
        password = os.getenv("SMTP_PASSWORD", "")
        from_addr = os.getenv("SMTP_FROM", user or "noreply@premiumhub.local")
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            if user:
                s.login(user, password)
            s.sendmail(from_addr, [to], msg.as_string())
        return True
    except Exception as e:
        print(f"[EMAIL] send failed: {e}")
        return False


def enrich_user_for_api(user: dict) -> dict:
    """Add email/auth_type for /me responses."""
    if not user:
        return user
    key = str(user.get("telegram_id") or "")
    wu = get_web_user_by_key(key)
    out = dict(user)
    if wu:
        out["email"] = wu.get("email")
        out["auth_type"] = "email" if str(key).startswith("W") else "telegram+email"
        out["email_verified"] = bool(int(wu.get("email_verified") or 0))
        out["name"] = wu.get("name") or user.get("first_name")
    else:
        out["auth_type"] = user.get("auth_type") or "telegram"
        out["email"] = user.get("email") or ""
    out["balance"] = float(user.get("balance") or 0)
    return out


def list_web_users(limit: int = 50) -> list:
    c = db()
    rows = c.execute(
        "SELECT email,name,user_key,telegram_id,email_verified,status,created_at FROM web_users ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    c.close()
    return [row_to_dict(r) for r in rows]


def init_identity_map() -> None:
    """user_identity: maps any login identity → canonical user_key (users.telegram_id)."""
    c = db()
    c.execute(
        """CREATE TABLE IF NOT EXISTS user_identity(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_type TEXT NOT NULL,
            identity_value TEXT NOT NULL,
            user_key TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(identity_type, identity_value)
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_identity_key ON user_identity(user_key)")
    c.commit()
    c.close()


def _set_identity(identity_type: str, identity_value: str, user_key: str) -> None:
    c = db()
    c.execute(
        """INSERT INTO user_identity(identity_type,identity_value,user_key)
           VALUES(?,?,?)
           ON CONFLICT(identity_type,identity_value) DO UPDATE SET user_key=excluded.user_key""",
        (identity_type, str(identity_value).lower() if identity_type == "email" else str(identity_value), user_key),
    )
    c.commit()
    c.close()


def resolve_user_key(identity_type: str, identity_value: str) -> Optional[str]:
    c = db()
    r = c.execute(
        "SELECT user_key FROM user_identity WHERE identity_type=? AND identity_value=?",
        (identity_type, str(identity_value).lower() if identity_type == "email" else str(identity_value)),
    ).fetchone()
    c.close()
    return str(r["user_key"]) if r else None


def _merge_user_rows(from_key: str, to_key: str) -> dict:
    """
    Move balance + re-point orders/topups/transactions from_key → to_key.
    Does NOT delete from_key row (sets balance 0, marks merged) — no history loss.
    """
    from decimal import Decimal
    from ..core.helpers import money, new_ref

    if from_key == to_key:
        return {"ok": True, "message": "already same"}
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        src = c.execute("SELECT * FROM users WHERE telegram_id=?", (from_key,)).fetchone()
        dst = c.execute("SELECT * FROM users WHERE telegram_id=?", (to_key,)).fetchone()
        if not src:
            c.rollback()
            raise HTTPException(400, "Source account not found")
        if not dst:
            c.execute(
                "INSERT INTO users(telegram_id,username,first_name,balance) VALUES(?,?,?,0)",
                (to_key, "", ""),
            )
            before_dst = Decimal("0")
        else:
            before_dst = money(dst["balance"])
        move = money(src["balance"] or 0)
        after_dst = money(before_dst + move)
        c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (float(after_dst), to_key))
        c.execute("UPDATE users SET balance=0 WHERE telegram_id=?", (from_key,))
        if move > 0:
            c.execute(
                """INSERT INTO transactions(
                     telegram_id,kind,amount,balance_before,balance_after,reference,status,note
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    to_key,
                    "ACCOUNT_MERGE",
                    float(move),
                    float(before_dst),
                    float(after_dst),
                    new_ref("MERGE"),
                    "COMPLETED",
                    f"Merged from {from_key}",
                ),
            )
        # Re-point history (keep rows, change owner)
        for table in ("orders", "topups", "transactions"):
            try:
                c.execute(
                    f"UPDATE {table} SET telegram_id=? WHERE telegram_id=?",
                    (to_key, from_key),
                )
            except Exception:
                pass
        c.execute(
            """UPDATE web_users SET user_key=?, telegram_id=?, updated_at=CURRENT_TIMESTAMP
               WHERE user_key=?""",
            (to_key, to_key if to_key.isdigit() else None, from_key),
        )
        c.execute(
            "UPDATE user_identity SET user_key=? WHERE user_key=?",
            (to_key, from_key),
        )
        c.commit()
        return {
            "ok": True,
            "from_key": from_key,
            "to_key": to_key,
            "balance_moved": float(move),
            "new_balance": float(after_dst),
        }
    except HTTPException:
        raise
    except Exception as e:
        c.rollback()
        raise HTTPException(500, f"Merge failed: {e}")
    finally:
        c.close()


def link_email_to_telegram(current_user_key: str, email: str, password: str) -> dict:
    """
    Logged-in Telegram user adds email password login (no second wallet).
    If email already has a W* account, merge that wallet into telegram.
    """
    email = normalize_email(email)
    if not valid_email(email):
        raise HTTPException(400, "Invalid email")
    if len(password) < 6:
        raise HTTPException(400, "Password min 6 chars")
    if str(current_user_key).startswith("W"):
        raise HTTPException(400, "Already an email account — use link Telegram instead")

    ensure_user(current_user_key)
    wu = get_web_user_by_email(email)
    if wu:
        if not verify_password(password, wu["password_hash"]):
            raise HTTPException(401, "Email password incorrect")
        old_key = str(wu["user_key"])
        if old_key != current_user_key:
            result = _merge_user_rows(old_key, current_user_key)
        else:
            result = {"ok": True, "balance_moved": 0}
        c = db()
        c.execute(
            """UPDATE web_users SET user_key=?, telegram_id=?, updated_at=CURRENT_TIMESTAMP WHERE email=?""",
            (current_user_key, current_user_key, email),
        )
        c.execute(
            "UPDATE users SET email=?, auth_type='telegram+email' WHERE telegram_id=?",
            (email, current_user_key),
        )
        c.commit()
        c.close()
        _set_identity("email", email, current_user_key)
        _set_identity("telegram", current_user_key, current_user_key)
        token = create_access_token(current_user_key, extra={"auth": "telegram+email", "email": email})
        return {
            "access_token": token,
            "token_type": "bearer",
            "user": _public_profile(current_user_key, email=email),
            "merge": result,
            "message": "Email linked to Telegram account",
        }

    # New email credentials on existing telegram account
    c = db()
    c.execute(
        """INSERT INTO web_users(email,password_hash,name,phone,user_key,telegram_id,email_verified,status)
           VALUES(?,?,?,?,?,?,1,'active')""",
        (email, hash_password(password), "", "", current_user_key, current_user_key),
    )
    c.execute(
        "UPDATE users SET email=?, auth_type='telegram+email' WHERE telegram_id=?",
        (email, current_user_key),
    )
    c.commit()
    c.close()
    _set_identity("email", email, current_user_key)
    _set_identity("telegram", current_user_key, current_user_key)
    token = create_access_token(current_user_key, extra={"auth": "telegram+email", "email": email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _public_profile(current_user_key, email=email),
        "message": "Email added — you can login with email or Telegram",
    }


def link_telegram_to_email(current_user_key: str, telegram_id: str) -> dict:
    """
    Logged-in email (W*) user claims a Telegram ID (after widget auth on client).
    Merges W* wallet into real telegram_id so one balance remains.
    """
    tid = str(telegram_id).strip()
    if not tid.isdigit():
        raise HTTPException(400, "Invalid telegram_id")
    if not str(current_user_key).startswith("W"):
        # already telegram-native
        _set_identity("telegram", tid, current_user_key)
        return {
            "ok": True,
            "user": _public_profile(current_user_key),
            "message": "Telegram already primary",
        }
    ensure_user(tid)
    result = _merge_user_rows(current_user_key, tid)
    wu = get_web_user_by_key(tid) or get_web_user_by_email(
        (get_web_user_by_key(current_user_key) or {}).get("email") or ""
    )
    email = ""
    if wu:
        email = str(wu.get("email") or "")
        c = db()
        c.execute(
            """UPDATE web_users SET user_key=?, telegram_id=?, updated_at=CURRENT_TIMESTAMP WHERE email=?""",
            (tid, tid, email),
        )
        c.execute(
            "UPDATE users SET email=?, auth_type='telegram+email' WHERE telegram_id=?",
            (email, tid),
        )
        c.commit()
        c.close()
        _set_identity("email", email, tid)
    _set_identity("telegram", tid, tid)
    token = create_access_token(tid, extra={"auth": "telegram+email", "email": email})
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": _public_profile(tid, email=email),
        "merge": result,
        "message": "Telegram linked — single wallet now",
    }
