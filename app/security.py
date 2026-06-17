from __future__ import annotations

import time
from typing import Optional

from fastapi import HTTPException, Request, status
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
_current_user_cache: dict[int, tuple[float, User]] = {}


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


async def authenticate_user(session: AsyncSession, email: str, password: str) -> Optional[User]:
    user = await session.scalar(select(User).where(User.email == email.strip().lower(), User.is_active.is_(True)))
    if not user or not verify_password(password.strip(), user.password_hash):
        return None
    return user


async def current_user(request: Request, session: AsyncSession) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    user_id_int = int(user_id)
    cached = _current_user_cache.get(user_id_int)
    if cached and cached[0] > time.monotonic() and cached[1].is_active:
        return cached[1]
    user = await session.get(User, user_id_int)
    if not user or not user.is_active:
        request.session.clear()
        _current_user_cache.pop(user_id_int, None)
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    _current_user_cache[user_id_int] = (time.monotonic() + 60, user)
    return user
