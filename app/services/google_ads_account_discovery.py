from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import GoogleAdsAccount, GoogleAdsConnection
from app.services.google_ads_connection import clean_customer_id
from app.services.google_ads_sync import enum_name


def _is_inactive_customer_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "CUSTOMER_NOT_ENABLED" in message
        or "not yet enabled or has been deactivated" in message
    )


def _client_for_connection(
    connection: GoogleAdsConnection,
    *,
    login_customer_id: str | None = None,
) -> GoogleAdsClient:
    config: dict[str, Any] = {
        "developer_token": connection.developer_token,
        "client_id": connection.client_id,
        "client_secret": connection.client_secret,
        "refresh_token": connection.refresh_token,
        "use_proto_plus": True,
    }
    if login_customer_id:
        config["login_customer_id"] = clean_customer_id(login_customer_id)
    missing = [key for key, value in config.items() if key != "use_proto_plus" and not value]
    if missing:
        raise RuntimeError(f"Missing Google Ads connection fields: {', '.join(missing)}")
    if connection.api_version:
        return GoogleAdsClient.load_from_dict(config, version=connection.api_version)
    return GoogleAdsClient.load_from_dict(config)


def _customer_info(client: GoogleAdsClient, customer_id: str) -> dict[str, Any]:
    ga_service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer.id,
          customer.descriptive_name,
          customer.currency_code,
          customer.manager
        FROM customer
        LIMIT 1
    """
    for row in ga_service.search(customer_id=customer_id, query=query):
        return {
            "id": clean_customer_id(str(row.customer.id)),
            "name": row.customer.descriptive_name or f"Google Ads {customer_id}",
            "currency_code": row.customer.currency_code or "",
            "manager": bool(row.customer.manager),
        }
    return {
        "id": clean_customer_id(customer_id),
        "name": f"Google Ads {customer_id}",
        "currency_code": "",
        "manager": False,
    }


def _upsert_account(
    session: Session,
    *,
    connection: GoogleAdsConnection,
    manager_id: str,
    manager_name: str,
    customer_id: str,
    account_name: str,
    currency_code: str,
) -> None:
    stmt = insert(GoogleAdsAccount).values(
        connection_id=connection.id,
        manager_name=manager_name,
        manager_customer_id=clean_customer_id(manager_id),
        name=account_name or f"Google Ads {customer_id}",
        customer_id=clean_customer_id(customer_id),
        currency_code=currency_code or "",
        source_label=f"Discovered from {connection.email or connection.name}",
        is_active=True,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[GoogleAdsAccount.customer_id],
        set_={
            "connection_id": stmt.excluded.connection_id,
            "manager_name": stmt.excluded.manager_name,
            "manager_customer_id": stmt.excluded.manager_customer_id,
            "name": stmt.excluded.name,
            "currency_code": func.coalesce(func.nullif(stmt.excluded.currency_code, ""), GoogleAdsAccount.currency_code),
            "source_label": stmt.excluded.source_label,
            "is_active": True,
        },
    )
    session.execute(stmt)


def discover_accounts_for_connection(session: Session, connection_id: int) -> dict[str, Any]:
    connection = session.get(GoogleAdsConnection, connection_id)
    if connection is None:
        raise RuntimeError("Google Ads connection not found.")

    discovered_accounts = 0
    discovered_managers: set[str] = set()
    seen_accounts: set[str] = set()
    visited_managers: set[str] = set()
    queued_managers: set[str] = set()
    errors: list[str] = []
    skipped: list[str] = []
    root_client = _client_for_connection(connection)
    customer_service = root_client.get_service("CustomerService")
    accessible = customer_service.list_accessible_customers()
    resource_names = list(accessible.resource_names)
    manager_queue: list[dict[str, Any]] = []

    for resource_name in resource_names:
        accessible_customer_id = clean_customer_id(resource_name.split("/")[-1])
        if not accessible_customer_id:
            continue
        try:
            direct_client = _client_for_connection(connection, login_customer_id=accessible_customer_id)
            info = _customer_info(direct_client, accessible_customer_id)
        except Exception as exc:  # noqa: BLE001 - keep discovery moving across accessible customers.
            if _is_inactive_customer_error(exc):
                skipped.append(f"{accessible_customer_id}: inactive or deactivated")
                continue
            errors.append(f"{accessible_customer_id}: {exc}")
            continue

        if not info["manager"]:
            _upsert_account(
                session,
                connection=connection,
                manager_id=info["id"],
                manager_name=info["name"],
                customer_id=info["id"],
                account_name=info["name"],
                currency_code=info["currency_code"],
            )
            if info["id"] not in seen_accounts:
                discovered_accounts += 1
                seen_accounts.add(info["id"])
            continue

        discovered_managers.add(info["id"])
        if info["id"] not in queued_managers:
            manager_queue.append(info)
            queued_managers.add(info["id"])

    while manager_queue:
        info = manager_queue.pop(0)
        if info["id"] in visited_managers:
            continue
        visited_managers.add(info["id"])
        manager_client = _client_for_connection(connection, login_customer_id=info["id"])
        ga_service = manager_client.get_service("GoogleAdsService")
        query = """
            SELECT
              customer_client.id,
              customer_client.descriptive_name,
              customer_client.currency_code,
              customer_client.manager,
              customer_client.hidden,
              customer_client.status
            FROM customer_client
        """
        try:
            rows = ga_service.search(customer_id=info["id"], query=query)
            for row in rows:
                child = row.customer_client
                child_id = clean_customer_id(str(child.id))
                if not child_id or child_id == info["id"]:
                    continue
                if bool(child.hidden) or enum_name(child.status) == "REMOVED":
                    continue
                if bool(child.manager):
                    discovered_managers.add(child_id)
                    if child_id not in queued_managers and child_id not in visited_managers:
                        manager_queue.append(
                            {
                                "id": child_id,
                                "name": child.descriptive_name or f"Google Ads Manager {child_id}",
                                "currency_code": child.currency_code or "",
                                "manager": True,
                            }
                        )
                        queued_managers.add(child_id)
                    continue
                _upsert_account(
                    session,
                    connection=connection,
                    manager_id=info["id"],
                    manager_name=info["name"],
                    customer_id=child_id,
                    account_name=child.descriptive_name or f"Google Ads {child_id}",
                    currency_code=child.currency_code or "",
                )
                if child_id not in seen_accounts:
                    discovered_accounts += 1
                    seen_accounts.add(child_id)
        except Exception as exc:  # noqa: BLE001
            if _is_inactive_customer_error(exc):
                skipped.append(f"{info['id']}: inactive or deactivated")
                continue
            errors.append(f"{info['id']}: {exc}")

    connection.last_discovery_at = datetime.now(timezone.utc)
    connection.last_discovery_error = "\n".join(errors[-10:]) if errors else None
    connection.discovered_manager_count = len(discovered_managers)
    connection.discovered_account_count = discovered_accounts
    session.commit()
    return {
        "connection_id": connection.id,
        "connection_name": connection.name,
        "accessible_customers": len(resource_names),
        "manager_count": len(discovered_managers),
        "account_count": discovered_accounts,
        "errors": errors,
        "skipped": skipped,
    }
