from __future__ import annotations

from contextlib import asynccontextmanager
import os
import time

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.config import ROOT_DIR, get_settings
from app.database import AsyncSessionLocal, Base, engine, get_session
from app.routers import ad_factory, analytics, assets, auth, automation, browser_automation, customer_match, dashboard, editor_exports, ga_search_terms, keywords, landing_pages, negative_keywords, optimize_campaigns, page_feeds, settings as settings_router, stores
from app.seed import seed_database
from app.services.dashboard import get_dashboard_static_data, get_recent_runs
from app.services.google_ads_api_errors import recent_unacknowledged_google_ads_api_errors
from app.services.google_ads_connection import get_google_ads_connection_status


settings = get_settings()
_google_ads_alert_cache: dict[str, object] = {"expires_at": 0.0, "data": {"count": 0, "errors": []}}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.auto_init_db:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await seed_database(session, settings)
    if os.getenv("STARTUP_WARMUP_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                await get_dashboard_static_data(session)
                await get_recent_runs(session)
                await get_google_ads_connection_status(session, include_accounts=False)
        except Exception as exc:  # noqa: BLE001 - keep web alive during temporary database saturation.
            app.state.startup_warmup_error = str(exc)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.session_cookie_secure,
)
app.mount("/static", StaticFiles(directory=ROOT_DIR / "app" / "static"), name="static")
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(ad_factory.router)
app.include_router(optimize_campaigns.router)
app.include_router(assets.router)
app.include_router(page_feeds.router)
app.include_router(keywords.router)
app.include_router(ga_search_terms.router)
app.include_router(landing_pages.router)
app.include_router(negative_keywords.router)
app.include_router(automation.router)
app.include_router(browser_automation.router)
app.include_router(editor_exports.router)
app.include_router(customer_match.router)
app.include_router(stores.router)
app.include_router(analytics.router)
app.include_router(settings_router.router)


@app.middleware("http")
async def attach_google_ads_api_error_alert(request: Request, call_next):
    request.state.google_ads_api_error_alert = {"count": 0, "errors": []}
    skip_prefixes = ("/api/", "/static/", "/feeds/", "/healthz", "/login", "/logout")
    should_check = not request.url.path.startswith(skip_prefixes)
    if should_check:
        try:
            cached = _google_ads_alert_cache.get("data")
            if float(_google_ads_alert_cache.get("expires_at", 0.0)) > time.monotonic() and isinstance(cached, dict):
                request.state.google_ads_api_error_alert = cached
            else:
                async with AsyncSessionLocal() as session:
                    data = await recent_unacknowledged_google_ads_api_errors(session)
                _google_ads_alert_cache.update({"expires_at": time.monotonic() + 15, "data": data})
                request.state.google_ads_api_error_alert = data
        except Exception as exc:  # noqa: BLE001 - never let alert lookup block the app shell.
            request.state.google_ads_api_error_alert = {"count": 0, "errors": [], "lookup_error": str(exc)}
    response = await call_next(request)
    return response


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    response = await call_next(request)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(select(1))
    return {"status": "ok"}
