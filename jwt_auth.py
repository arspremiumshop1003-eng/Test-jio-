"""JWT auth for web users. Identity = telegram_id."""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

from ..core.config import JWT_SECRET, JWT_EXPIRE_HOURS, BOT_TOKEN, ADMIN_IDS
from ..core.users import get_user, is_banned, ensure_user

security = HTTPBearer(auto_error=False)
ALGORITHM = "HS256"


def create_access_token(telegram_id: str, extra: Optional[dict] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {
        "sub": str(telegram_id),
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {e}",
        )


def verify_telegram_login(data: dict) -> bool:
    """
    Verify Telegram Login Widget hash.
    https://core.telegram.org/widgets/login#checking-authorization
    """
    if not BOT_TOKEN:
        return False
    check_hash = data.get("hash", "")
    auth_data = {k: v for k, v in data.items() if k != "hash"}
    data_check_arr = [f"{k}={v}" for k, v in sorted(auth_data.items())]
    data_check_string = "\n".join(data_check_arr)
    secret_key = hashlib.sha256(BOT_TOKEN.encode()).digest()
    computed = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, check_hash):
        return False
    # Auth data should not be older than 1 day
    auth_date = int(data.get("auth_date", 0) or 0)
    if time.time() - auth_date > 86400:
        return False
    return True


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(creds.credentials)
    uid = str(payload.get("sub") or "")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = get_user(uid)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if is_banned(uid):
        raise HTTPException(status_code=403, detail="Account banned")
    return user


async def get_optional_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[dict]:
    if not creds or not creds.credentials:
        return None
    try:
        return await get_current_user(creds)
    except HTTPException:
        return None


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    uid = str(user.get("telegram_id") or "")
    if uid in ADMIN_IDS:
        return user
    # Optional: ADMIN_EMAILS=admin@x.com,other@y.com
    import os

    admin_emails = {
        x.strip().lower()
        for x in os.getenv("ADMIN_EMAILS", "").split(",")
        if x.strip()
    }
    if admin_emails:
        email = str(user.get("email") or "").lower()
        if not email:
            try:
                from .email_auth import get_web_user_by_key

                wu = get_web_user_by_key(uid)
                email = str((wu or {}).get("email") or "").lower()
            except Exception:
                email = ""
        if email in admin_emails:
            return user
    raise HTTPException(status_code=403, detail="Admin only")


def login_from_telegram_widget(data: dict) -> dict:
    """Validate widget data, ensure user, return token + profile."""
    if not verify_telegram_login(data):
        raise HTTPException(status_code=401, detail="Invalid Telegram auth data")
    tid = str(data.get("id"))
    user = ensure_user(
        tid,
        username=str(data.get("username") or ""),
        first_name=str(data.get("first_name") or ""),
    )
    if is_banned(tid):
        raise HTTPException(status_code=403, detail="Account banned")
    token = create_access_token(tid)
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
