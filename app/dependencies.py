from __future__ import annotations

from typing import Any

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ROOT_DIR, get_settings
from app.database import get_session
from app.models import User
from app.security import current_user


settings = get_settings()


class AppTemplates(Jinja2Templates):
    def TemplateResponse(self, *args: Any, **kwargs: Any):  # noqa: N802 - Starlette API name.
        if args and isinstance(args[0], str):
            name = args[0]
            context = args[1] if len(args) > 1 else kwargs.pop("context", {})
            request = context.get("request") if isinstance(context, dict) else None
            if request is None:
                return super().TemplateResponse(*args, **kwargs)
            status_code = args[2] if len(args) > 2 else kwargs.pop("status_code", 200)
            headers = args[3] if len(args) > 3 else kwargs.pop("headers", None)
            media_type = args[4] if len(args) > 4 else kwargs.pop("media_type", None)
            background = args[5] if len(args) > 5 else kwargs.pop("background", None)
            return super().TemplateResponse(
                request,
                name,
                context,
                status_code=status_code,
                headers=headers,
                media_type=media_type,
                background=background,
            )
        return super().TemplateResponse(*args, **kwargs)


templates = AppTemplates(directory=ROOT_DIR / "app" / "templates")
templates.env.cache = None


async def require_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    return await current_user(request, session)
