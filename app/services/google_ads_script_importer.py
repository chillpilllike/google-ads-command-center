from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import ROOT_DIR
from app.models import GoogleAdsAccount, GoogleAdsConnection
from app.services.google_ads_connection import clean_customer_id, clear_google_ads_connection_status_cache


GOOGLE_ADS_ENV_KEYS = {
    "developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
    "client_id": "GOOGLE_ADS_CLIENT_ID",
    "client_secret": "GOOGLE_ADS_CLIENT_SECRET",
    "refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
    "api_version": "GOOGLE_ADS_API_VERSION",
}
REQUIRED_CREDENTIAL_FIELDS = ("developer_token", "client_id", "client_secret", "refresh_token")


@dataclass(frozen=True)
class GoogleAdsScriptSource:
    label: str
    path: Path
    credentials: dict[str, str]
    login_customer_id: str = ""
    customer_id: str = ""
    account_config_path: Path | None = None
    connection_label: str = ""
    account_name: str = ""


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _source_label(path: Path) -> str:
    if path.name == ".env.manager":
        return "Manager automation"
    if path.name.startswith(".env"):
        return _env_account_name(path) or f"Imported {path.name}"
    return f"Imported {path.stem}"


def _env_account_name(path: Path) -> str:
    return ""


def _complete_credentials(credentials: dict[str, str]) -> bool:
    return all(credentials.get(field) for field in REQUIRED_CREDENTIAL_FIELDS)


def _source_from_env(path: Path) -> GoogleAdsScriptSource | None:
    values = parse_env_file(path)
    credentials = {
        field: values.get(env_key, "")
        for field, env_key in GOOGLE_ADS_ENV_KEYS.items()
    }
    inherited_source: GoogleAdsScriptSource | None = None
    config_py = values.get("GOOGLE_ADS_CONFIG_PY")
    if not _complete_credentials(credentials) and config_py:
        config_path = Path(config_py).expanduser()
        if not config_path.is_absolute():
            config_path = (path.parent / config_path).resolve()
        inherited_source = _source_from_python(config_path)
        if inherited_source:
            credentials = {
                field: credentials.get(field) or inherited_source.credentials.get(field, "")
                for field in GOOGLE_ADS_ENV_KEYS
            }
    if not _complete_credentials(credentials):
        return None
    account_config_path = ROOT_DIR / "config" / "google_ads_accounts.json" if path.name == ".env.manager" else None
    customer_id = clean_customer_id(values.get("GOOGLE_ADS_CUSTOMER_ID", ""))
    login_customer_id = clean_customer_id(values.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))
    account_name = _env_account_name(path)
    return GoogleAdsScriptSource(
        label=_source_label(path),
        path=path,
        credentials=credentials,
        login_customer_id=login_customer_id or (inherited_source.login_customer_id if inherited_source else ""),
        customer_id=customer_id or (inherited_source.customer_id if inherited_source else ""),
        account_config_path=account_config_path if account_config_path and account_config_path.exists() else None,
        connection_label="Legacy Google Ads automations" if inherited_source else "",
        account_name=account_name,
    )


def _literal_assignments(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except Exception:
        return {}
    values: dict[str, Any] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value_node = node.value
        else:
            continue
        if not targets:
            continue
        try:
            value = ast.literal_eval(value_node)
        except Exception:
            continue
        for name in targets:
            values[name] = value
    return values


def _source_from_python(path: Path) -> GoogleAdsScriptSource | None:
    values = _literal_assignments(path)
    config = values.get("GOOGLE_ADS_CONFIG")
    if not isinstance(config, dict):
        return None
    credentials = {
        "developer_token": str(config.get("developer_token") or ""),
        "client_id": str(config.get("client_id") or ""),
        "client_secret": str(config.get("client_secret") or ""),
        "refresh_token": str(config.get("refresh_token") or ""),
        "api_version": str(config.get("api_version") or ""),
    }
    if not _complete_credentials(credentials):
        return None
    return GoogleAdsScriptSource(
        label=_source_label(path),
        path=path,
        credentials=credentials,
        login_customer_id=clean_customer_id(str(config.get("login_customer_id") or values.get("LOGIN_CUSTOMER_ID") or "")),
        customer_id=clean_customer_id(str(values.get("CUSTOMER_ID") or values.get("GOOGLE_ADS_CUSTOMER_ID") or "")),
    )


def source_from_path(path: Path) -> GoogleAdsScriptSource | None:
    path = path.expanduser().resolve()
    if not path.exists() or not path.is_file():
        return None
    if path.name.startswith(".env") or path.suffix.lower() in {".env", ".dotenv"}:
        return _source_from_env(path)
    if path.suffix.lower() == ".py":
        return _source_from_python(path)
    return None


def default_import_paths() -> list[Path]:
    paths = [
        ROOT_DIR / ".env.gofinch",
        ROOT_DIR / ".env",
        ROOT_DIR / "scripts" / "google_ads_optimizer.py",
        ROOT_DIR / "scripts" / "gofinch_google_ads_optimizer.py",
    ]
    values = parse_env_file(ROOT_DIR / ".env.gofinch")
    config_py = values.get("GOOGLE_ADS_CONFIG_PY")
    if config_py:
        paths.append(Path(config_py))
    return paths


def discover_import_paths(roots: Iterable[Path]) -> list[Path]:
    paths: list[Path] = []
    for root in roots:
        root = root.expanduser()
        if root.is_file():
            paths.append(root)
            continue
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.startswith(".env") or ("google_ads" in name and path.suffix.lower() == ".py"):
                paths.append(path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _account_rows_from_config(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(errors="ignore"))
    groups = data.get("manager_groups") or [{"manager": data["manager"], "sub_accounts": data["sub_accounts"]}]
    rows: list[dict[str, str]] = []
    for group in groups:
        manager = group["manager"]
        for account in group.get("sub_accounts", []):
            rows.append(
                {
                    "manager_name": str(manager["name"]),
                    "manager_customer_id": clean_customer_id(str(manager["customer_id"])),
                    "name": str(account["name"]),
                    "customer_id": clean_customer_id(str(account["customer_id"])),
                    "currency_code": str(account.get("currency_code") or ""),
                }
            )
    return rows


async def _connection_for_source(session: AsyncSession, source: GoogleAdsScriptSource) -> GoogleAdsConnection:
    connection_name = source.connection_label or source.label
    existing = await session.scalar(
        select(GoogleAdsConnection).where(GoogleAdsConnection.refresh_token == source.credentials["refresh_token"]).limit(1)
    )
    if existing is None:
        existing = await session.scalar(select(GoogleAdsConnection).where(GoogleAdsConnection.name == connection_name).limit(1))
    connection = existing or GoogleAdsConnection(name=connection_name)
    if existing is None:
        session.add(connection)
        await session.flush()
    connection.name = connection_name
    connection.developer_token = source.credentials.get("developer_token", "")
    connection.client_id = source.credentials.get("client_id", "")
    connection.client_secret = source.credentials.get("client_secret", "")
    connection.refresh_token = source.credentials.get("refresh_token", "")
    connection.api_version = source.credentials.get("api_version", "")
    connection.is_active = True
    return connection


async def _upsert_account(
    session: AsyncSession,
    *,
    connection: GoogleAdsConnection,
    manager_name: str,
    manager_customer_id: str,
    account_name: str,
    customer_id: str,
    currency_code: str = "",
    source_label: str,
) -> None:
    stmt = insert(GoogleAdsAccount).values(
        connection_id=connection.id,
        manager_name=manager_name or "Imported manager",
        manager_customer_id=clean_customer_id(manager_customer_id or customer_id),
        name=account_name or f"Google Ads {customer_id}",
        customer_id=clean_customer_id(customer_id),
        currency_code=currency_code or "",
        source_label=source_label,
        is_active=True,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[GoogleAdsAccount.customer_id],
        set_={
            "connection_id": stmt.excluded.connection_id,
            "manager_name": func.coalesce(func.nullif(GoogleAdsAccount.manager_name, ""), stmt.excluded.manager_name),
            "manager_customer_id": stmt.excluded.manager_customer_id,
            "name": func.coalesce(func.nullif(GoogleAdsAccount.name, ""), stmt.excluded.name),
            "currency_code": func.coalesce(func.nullif(GoogleAdsAccount.currency_code, ""), stmt.excluded.currency_code),
            "source_label": stmt.excluded.source_label,
            "is_active": True,
        },
    )
    await session.execute(stmt)


async def import_google_ads_script_sources(
    session: AsyncSession,
    paths: Sequence[Path],
) -> dict[str, Any]:
    imported_connection_ids: set[int] = set()
    linked_accounts = 0
    scanned = 0
    skipped: list[str] = []
    imported_labels: list[str] = []

    for path in paths:
        scanned += 1
        source = source_from_path(path)
        if source is None:
            skipped.append(str(path))
            continue
        connection = await _connection_for_source(session, source)
        imported_connection_ids.add(int(connection.id))
        imported_labels.append(connection.name)

        if source.account_config_path:
            for account in _account_rows_from_config(source.account_config_path):
                await _upsert_account(
                    session,
                    connection=connection,
                    manager_name=account["manager_name"],
                    manager_customer_id=account["manager_customer_id"],
                    account_name=account["name"],
                    customer_id=account["customer_id"],
                    currency_code=account.get("currency_code", ""),
                    source_label=f"Imported from {source.path.name}",
                )
                linked_accounts += 1
        elif source.customer_id:
            manager_name = (
                f"Manager {source.login_customer_id}"
                if source.login_customer_id and source.login_customer_id != source.customer_id
                else source.label
            )
            await _upsert_account(
                session,
                connection=connection,
                manager_name=manager_name,
                manager_customer_id=source.login_customer_id or source.customer_id,
                account_name=source.account_name or source.label,
                customer_id=source.customer_id,
                source_label=f"Imported from {source.path.name}",
            )
            linked_accounts += 1

    clear_google_ads_connection_status_cache()
    return {
        "scanned": scanned,
        "imported_connections": len(imported_connection_ids),
        "linked_accounts": linked_accounts,
        "labels": sorted(set(imported_labels)),
        "skipped": skipped,
    }


async def import_default_google_ads_script_sources(session: AsyncSession) -> dict[str, Any]:
    return await import_google_ads_script_sources(session, default_import_paths())
