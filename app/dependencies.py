from __future__ import annotations

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ROOT_DIR, get_settings
from app.database import get_session
from app.models import User
from app.security import current_user


settings = get_settings()
templates = Jinja2Templates(directory=ROOT_DIR / "app" / "templates")


async def require_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    return await current_user(request, session)
