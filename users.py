"""User management — shared by bot + web."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from .db import db, row_to_dict
from .helpers import money, new_ref, fmoney


def upsert_user_from_user(u: Dict[str, Any]) -> None:
    uid = u.get("id")
    if uid is None:
        return
    c = db()
    c.execute(
        """INSERT INTO users(telegram_id,username,first_name)
           VALUES(?,?,?)
           ON CONFLICT(telegram_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name,
             last_seen=CURRENT_TIMESTAMP""",
        (str(uid), u.get("username") or "", u.get("first_name") or ""),
    )
    c.commit()
    c.close()


def get_user(telegram_id: Any) -> Optional[dict]:
    c = db()
    r = c.execute("SELECT * FROM users WHERE telegram_id=?", (str(telegram_id),)).fetchone()
    c.close()
    return row_to_dict(r)


def ensure_user(telegram_id: Any, username: str = "", first_name: str = "") -> dict:
    c = db()
    c.execute(
        """INSERT INTO users(telegram_id,username,first_name)
           VALUES(?,?,?)
           ON CONFLICT(telegram_id) DO UPDATE SET
             username=COALESCE(NULLIF(excluded.username,''), users.username),
             first_name=COALESCE(NULLIF(excluded.first_name,''), users.first_name),
             last_seen=CURRENT_TIMESTAMP""",
        (str(telegram_id), username or "", first_name or ""),
    )
    c.commit()
    r = c.execute("SELECT * FROM users WHERE telegram_id=?", (str(telegram_id),)).fetchone()
    c.close()
    return row_to_dict(r) or {}


def get_balance(uid: Any) -> Decimal:
    c = db()
    r = c.execute("SELECT balance FROM users WHERE telegram_id=?", (str(uid),)).fetchone()
    c.close()
    return money(r["balance"] if r else 0)


def is_banned(uid: Any) -> bool:
    try:
        c = db()
        r = c.execute("SELECT banned FROM users WHERE telegram_id=?", (str(uid),)).fetchone()
        c.close()
        return bool(r and int(r["banned"] or 0) == 1)
    except Exception:
        return False


def ban_user(uid: str, reason: str = "") -> None:
    c = db()
    c.execute(
        """INSERT INTO users(telegram_id,banned,ban_reason,banned_at)
           VALUES(?,1,?,CURRENT_TIMESTAMP)
           ON CONFLICT(telegram_id) DO UPDATE SET
             banned=1, ban_reason=excluded.ban_reason, banned_at=CURRENT_TIMESTAMP""",
        (str(uid), str(reason or "")[:500]),
    )
    c.commit()
    c.close()


def unban_user(uid: str) -> None:
    c = db()
    c.execute(
        """UPDATE users SET banned=0,ban_reason='',banned_at=NULL WHERE telegram_id=?""",
        (str(uid),),
    )
    c.commit()
    c.close()


def admin_add_balance(uid: str, amount: Any, note: str = "Admin manual credit") -> Decimal:
    amount = money(amount)
    if amount <= 0:
        raise ValueError("Invalid amount")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute("SELECT balance FROM users WHERE telegram_id=?", (str(uid),)).fetchone()
        if not user:
            c.execute("INSERT INTO users(telegram_id,balance) VALUES(?,0)", (str(uid),))
            before = Decimal("0")
        else:
            before = money(user["balance"])
        after = money(before + amount)
        c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (float(after), str(uid)))
        c.execute(
            """INSERT INTO transactions(
                telegram_id,kind,amount,balance_before,balance_after,reference,status,note
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uid), "ADMIN_CREDIT", float(amount), float(before), float(after),
                new_ref("ADMIN"), "COMPLETED", note,
            ),
        )
        c.commit()
        return after
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def admin_remove_balance(uid: str, amount: Any, note: str = "Admin manual debit") -> Decimal:
    amount = money(amount)
    if amount <= 0:
        raise ValueError("Invalid amount")
    c = db()
    try:
        c.execute("BEGIN IMMEDIATE")
        user = c.execute("SELECT balance FROM users WHERE telegram_id=?", (str(uid),)).fetchone()
        if not user:
            c.rollback()
            raise ValueError("User not found")
        before = money(user["balance"])
        if before < amount:
            c.rollback()
            raise ValueError(f"Insufficient balance. Current: ${fmoney(before)}")
        after = money(before - amount)
        c.execute("UPDATE users SET balance=? WHERE telegram_id=?", (float(after), str(uid)))
        c.execute(
            """INSERT INTO transactions(
                telegram_id,kind,amount,balance_before,balance_after,reference,status,note
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(uid), "ADMIN_DEBIT", -float(amount), float(before), float(after),
                new_ref("ADMIN"), "COMPLETED", note,
            ),
        )
        c.commit()
        return after
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def search_users(query: str, limit: int = 30) -> List[dict]:
    q = str(query or "").strip().lstrip("@")
    c = db()
    if q.isdigit():
        rows = c.execute(
            "SELECT * FROM users WHERE telegram_id=? OR lower(username) LIKE ? LIMIT ?",
            (q, f"%{q.lower()}%", limit),
        ).fetchall()
    else:
        rows = c.execute(
            """SELECT * FROM users WHERE lower(username) LIKE ? OR lower(first_name) LIKE ?
               ORDER BY last_seen DESC LIMIT ?""",
            (f"%{q.lower()}%", f"%{q.lower()}%", limit),
        ).fetchall()
    c.close()
    return [row_to_dict(r) for r in rows]


def list_users(limit: int = 50, offset: int = 0) -> List[dict]:
    c = db()
    rows = c.execute(
        "SELECT * FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    c.close()
    return [row_to_dict(r) for r in rows]


def user_stats(telegram_id: Any) -> dict:
    c = db()
    uid = str(telegram_id)
    orders_n = c.execute(
        "SELECT COUNT(*) n FROM orders WHERE telegram_id=?", (uid,)
    ).fetchone()["n"]
    completed = c.execute(
        "SELECT COUNT(*) n FROM orders WHERE telegram_id=? AND status='COMPLETED'", (uid,)
    ).fetchone()["n"]
    spent = c.execute(
        """SELECT COALESCE(SUM(customer_price),0) s FROM orders
           WHERE telegram_id=? AND status='COMPLETED'""",
        (uid,),
    ).fetchone()["s"]
    c.close()
    return {
        "total_orders": orders_n,
        "completed_orders": completed,
        "total_spent": float(money(spent)),
    }


def get_transactions(telegram_id: Any, limit: int = 20) -> List[dict]:
    c = db()
    rows = c.execute(
        "SELECT * FROM transactions WHERE telegram_id=? ORDER BY id DESC LIMIT ?",
        (str(telegram_id), limit),
    ).fetchall()
    c.close()
    return [row_to_dict(r) for r in rows]
