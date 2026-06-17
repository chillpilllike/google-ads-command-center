#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socketserver
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from google_auth_oauthlib.flow import Flow
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AppSetting, GoogleSearchConsoleConnection
from app.services.google_search_console import (
    GOOGLE_SEARCH_CONSOLE_SCOPES,
    discover_search_console_connection,
    fetch_google_email,
    sync_search_console_search_analytics,
)


REDIRECT_URI = "http://localhost:8080/"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: "OAuthCallbackServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        params = parse_qs(urlparse(self.path).query)
        self.server.oauth_result = {
            "code": params.get("code", [""])[0],
            "state": params.get("state", [""])[0],
            "error": params.get("error", [""])[0],
        }
        status = 400 if self.server.oauth_result.get("error") else 200
        body = (
            "<html><body style='font-family: sans-serif; padding: 32px'>"
            "<h2>Google Search Console connection received</h2>"
            "<p>You can return to Codex now. This window can be closed.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class OAuthCallbackServer(socketserver.TCPServer):
    allow_reuse_address = True
    oauth_result: dict[str, str] | None = None


def oauth_client_config(client_id: str, client_secret: str) -> dict[str, dict[str, str]]:
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def google_oauth_settings(session: Session) -> tuple[str, str]:
    rows = session.scalars(
        select(AppSetting).where(AppSetting.key.in_(("google_ads.client_id", "google_ads.client_secret")))
    ).all()
    values = {row.key: str(row.value or "") for row in rows}
    client_id = values.get("google_ads.client_id", "")
    client_secret = values.get("google_ads.client_secret", "")
    if not client_id or not client_secret:
        raise RuntimeError("Google OAuth client ID/secret are missing in Postgres Settings.")
    return client_id, client_secret


def upsert_connection(
    session: Session,
    *,
    email: str,
    client_id: str,
    client_secret: str,
    refresh_token: str,
) -> GoogleSearchConsoleConnection:
    connection = None
    if email:
        connection = session.scalar(
            select(GoogleSearchConsoleConnection)
            .where(GoogleSearchConsoleConnection.email == email)
            .limit(1)
        )
    if connection is None:
        connection = GoogleSearchConsoleConnection(name=email or "Firefox Search Console")
        session.add(connection)
        session.flush()
    connection.name = connection.name or email or "Firefox Search Console"
    connection.email = email or connection.email or ""
    connection.client_id = client_id
    connection.client_secret = client_secret
    connection.refresh_token = refresh_token
    connection.is_active = True
    connection.last_oauth_at = datetime.now(timezone.utc)
    session.commit()
    return connection


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect Google Search Console through Firefox and save the refresh token in Postgres."
    )
    parser.add_argument("--timeout", type=int, default=600, help="Seconds to wait for the OAuth callback.")
    parser.add_argument("--pull-days", type=int, default=28, help="Recent Search Console pull window after discovery.")
    parser.add_argument("--max-rows", type=int, default=25_000, help="Max Search Analytics rows per mapped site.")
    parser.add_argument("--skip-pull", action="store_true", help="Only connect and discover Search Console sites.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = get_settings()
    engine = create_engine(settings.sqlalchemy_sync_url)
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    with Session(engine) as session:
        client_id, client_secret = google_oauth_settings(session)

    flow = Flow.from_client_config(
        oauth_client_config(client_id, client_secret),
        scopes=list(GOOGLE_SEARCH_CONSOLE_SCOPES),
        redirect_uri=REDIRECT_URI,
    )
    authorization_url, expected_state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="false",
        prompt="consent",
    )

    with OAuthCallbackServer(("localhost", 8080), OAuthCallbackHandler) as httpd:
        httpd.timeout = max(int(args.timeout), 30)
        subprocess.run(["open", "-a", "Firefox", authorization_url], check=False)
        print(
            json.dumps(
                {
                    "status": "waiting_for_firefox_oauth",
                    "redirect_uri": REDIRECT_URI,
                    "timeout_seconds": httpd.timeout,
                    "note": "Complete Google sign-in/passkey/consent in Firefox. Tokens will not be printed.",
                },
                indent=2,
            ),
            flush=True,
        )
        httpd.handle_request()
        result = httpd.oauth_result or {}

    if result.get("error"):
        raise RuntimeError(f"Google OAuth returned an error: {result['error']}")
    if not result.get("code") or result.get("state") != expected_state:
        raise RuntimeError("OAuth callback was not received or state did not match.")

    flow.fetch_token(code=result["code"])
    credentials = flow.credentials
    if not credentials or not credentials.refresh_token:
        raise RuntimeError("Google did not return a refresh token. Re-run and approve offline access.")

    email = fetch_google_email(credentials.token or "")
    with Session(engine) as session:
        connection = upsert_connection(
            session,
            email=email,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=credentials.refresh_token,
        )
        discovery = discover_search_console_connection(session, connection.id)
        pull_result = None
        if not args.skip_pull:
            pull_result = sync_search_console_search_analytics(
                session,
                mode="recent",
                days=args.pull_days,
                max_rows=args.max_rows,
                force=True,
                connection_ids=[connection.id],
            )
        print(
            json.dumps(
                {
                    "status": "connected",
                    "email": email,
                    "connection_id": connection.id,
                    "discovery": discovery,
                    "pull_result": pull_result,
                },
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
