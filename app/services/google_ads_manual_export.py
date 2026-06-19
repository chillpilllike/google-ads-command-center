from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AdDraft, GoogleAdsAccount
from app.services.google_ads_landing_page_bank import canonical_landing_page_url, usable_landing_page_url


MANUAL_EXPORT_STATUSES = {
    "ready_for_review",
    "publish_ready",
    "publish_blocked",
    "needs_review",
    "approved",
    "published_enabled",
    "published_paused",
    "blocked_pmax_conversion_gate",
    "skipped_live_gate",
}


@dataclass(frozen=True)
class ManualExportFile:
    filename: str
    content: bytes
    content_type: str = "text/csv; charset=utf-8"


def _assets(draft: AdDraft) -> dict[str, Any]:
    return draft.generated_assets if isinstance(draft.generated_assets, dict) else {}


def _automation_source_key(draft: AdDraft) -> str:
    assets = _assets(draft)
    automation = assets.get("automation") if isinstance(assets.get("automation"), dict) else {}
    return str(automation.get("source_key") or "").strip()


def _identity(draft: AdDraft) -> dict[str, Any]:
    assets = _assets(draft)
    identity = assets.get("campaign_identity") if isinstance(assets.get("campaign_identity"), dict) else {}
    return identity


def _campaign_name(draft: AdDraft) -> str:
    assets = _assets(draft)
    identity = _identity(draft)
    return str(identity.get("campaign_name") or assets.get("campaign_name") or "").strip()


def _campaign_code(draft: AdDraft) -> str:
    assets = _assets(draft)
    identity = _identity(draft)
    return str(identity.get("campaign_code") or assets.get("campaign_code") or "").strip()


def _campaign_lane(draft: AdDraft) -> str:
    name = _campaign_name(draft)
    parts = [part.strip() for part in str(name or "").split("|")]
    if len(parts) >= 4 and parts[0].upper() == "AUTO":
        return parts[2]
    identity = _identity(draft)
    return str(identity.get("campaign_lane") or identity.get("lane") or "").strip()


def _category(draft: AdDraft) -> str:
    identity = _identity(draft)
    name = _campaign_name(draft)
    return str(identity.get("category") or _category_from_campaign_name(name) or "").strip()


def _category_from_campaign_name(name: str) -> str:
    text = str(name or "")
    parts = [part.strip() for part in text.split("|")]
    if len(parts) >= 3 and parts[0].upper() == "AUTO":
        return parts[1]
    return ""


def _ad_group_name(draft: AdDraft) -> str:
    assets = _assets(draft)
    clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
    for cluster in clusters:
        if isinstance(cluster, dict) and str(cluster.get("ad_group_name") or "").strip():
            return str(cluster.get("ad_group_name") or "").strip()
    identity = _identity(draft)
    ad_group_name = str(identity.get("ad_group_name") or assets.get("ad_group_name") or "").strip()
    if ad_group_name:
        return ad_group_name
    code = _campaign_code(draft) or f"DRAFT-{draft.id}"
    if str(draft.ad_type or "").lower() == "dsa":
        return f"AUTO | {_category(draft) or 'Testing / Discovery'} | DSA Pages | {code}"[:255]
    if str(draft.ad_type or "").lower() == "rsa":
        return f"AUTO | {_category(draft) or 'Testing / Discovery'} | RSA Keywords | {code}"[:255]
    return ""


def _asset_group_name(draft: AdDraft, group: dict[str, Any]) -> str:
    code = _campaign_code(draft) or f"DRAFT-{draft.id}"
    group_code = str(group.get("asset_group_code") or f"AG{int(group.get('asset_group_index') or 1):03d}").strip()
    category = _category(draft) or "PMax"
    return f"AUTO | {category} | PMax Asset Group | {code} | {group_code}"[:255]


def _status(draft: AdDraft) -> str:
    value = getattr(draft, "status", "")
    return str(getattr(value, "value", value) or "").strip()


def _clean_text(value: Any, *, max_len: Optional[int] = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if max_len is not None:
        text = text[:max_len].strip()
    return text


def _normalize_keyword(value: Any) -> str:
    return _clean_text(value, max_len=80).lower()


def _canonical_url(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    url = canonical_landing_page_url(text)
    if not url.startswith(("http://", "https://")) or not usable_landing_page_url(url):
        return ""
    return url.rstrip("/")


def _draft_sort_key(draft: AdDraft) -> tuple[str, int]:
    return (_campaign_name(draft).lower(), int(draft.id or 0))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-") or "google-ads-account"


def _campaign_file_label(draft: AdDraft) -> str:
    category = _slug(_category(draft) or "uncategorized")
    ad_type = _slug(str(draft.ad_type or "ad").upper())
    lane = _slug(_campaign_lane(draft) or "campaign")
    code = _slug(_campaign_code(draft) or f"draft-{draft.id}")
    return "-".join(part for part in [category, ad_type, lane, code] if part)[:120]


def automation_drafts_for_manual_export(session: Session, account: GoogleAdsAccount) -> list[AdDraft]:
    drafts = list(
        session.scalars(
            select(AdDraft)
            .where(AdDraft.account_id == account.id, AdDraft.generated_assets != {})
            .order_by(AdDraft.created_at.desc(), AdDraft.id.desc())
        ).all()
    )
    latest_by_key: dict[tuple[str, str], AdDraft] = {}
    fallback: list[AdDraft] = []
    for draft in drafts:
        source_key = _automation_source_key(draft)
        if not source_key.startswith("automation:"):
            continue
        if _status(draft) and _status(draft) not in MANUAL_EXPORT_STATUSES:
            continue
        key = (str(draft.ad_type or "").lower(), source_key)
        if key not in latest_by_key:
            latest_by_key[key] = draft
        elif int(draft.id or 0) > int(latest_by_key[key].id or 0):
            latest_by_key[key] = draft
    if not latest_by_key:
        for draft in drafts:
            if _automation_source_key(draft).startswith("automation:"):
                fallback.append(draft)
    return sorted(latest_by_key.values() or fallback, key=_draft_sort_key)


def _base_row(account: GoogleAdsAccount, draft: AdDraft) -> dict[str, Any]:
    return {
        "Account": account.name or "",
        "Customer ID": account.customer_id or "",
        "Campaign": _campaign_name(draft),
        "Ad group": _ad_group_name(draft),
        "Asset group": "",
        "Category": _category(draft),
        "Campaign lane": _campaign_lane(draft),
        "Ad type": str(draft.ad_type or "").upper(),
        "Campaign code": _campaign_code(draft),
        "Draft ID": draft.id,
        "Draft status": _status(draft),
        "Automation source": _automation_source_key(draft),
        "Manual file group": _campaign_file_label(draft),
    }


def _keyword_rows(account: GoogleAdsAccount, drafts: Iterable[AdDraft]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for draft in drafts:
        assets = _assets(draft)
        clusters = assets.get("keyword_clusters") if isinstance(assets.get("keyword_clusters"), list) else []
        for cluster in clusters:
            if not isinstance(cluster, dict):
                continue
            ad_group_name = str(cluster.get("ad_group_name") or _ad_group_name(draft)).strip()
            for item in cluster.get("exact_terms") or []:
                keyword = item.get("keyword") if isinstance(item, dict) else item
                text = _normalize_keyword(keyword)
                if not text:
                    continue
                key = (_campaign_name(draft), ad_group_name, text)
                if key in seen:
                    continue
                seen.add(key)
                row = _base_row(account, draft)
                row.update(
                    {
                        "Ad group": ad_group_name,
                        "Action": "Add keyword",
                        "Manual upload level": "Ad group",
                        "Manual target": "RSA exact keyword",
                        "Editor entity": "Keyword",
                        "Keyword": text,
                        "Match type": "Exact",
                        "Google Ads Editor match type": "Exact",
                        "Status": "Enabled",
                    }
                )
                rows.append(row)
        for group in assets.get("pmax_asset_groups") or []:
            if not isinstance(group, dict):
                continue
            asset_group_name = _asset_group_name(draft, group)
            for item in group.get("search_themes") or []:
                keyword = item.get("text") or item.get("keyword") if isinstance(item, dict) else item
                text = _normalize_keyword(keyword)
                if not text:
                    continue
                key = (_campaign_name(draft), asset_group_name, text)
                if key in seen:
                    continue
                seen.add(key)
                row = _base_row(account, draft)
                row.update(
                    {
                        "Ad group": "",
                        "Asset group": asset_group_name,
                        "Action": "Add search theme",
                        "Manual upload level": "Asset group",
                        "Manual target": "PMax search theme",
                        "Editor entity": "Search theme",
                        "Keyword": text,
                        "Match type": "Search theme",
                        "Google Ads Editor match type": "Search theme",
                        "Status": "Enabled",
                    }
                )
                rows.append(row)
    return rows


def _negative_keyword_rows(account: GoogleAdsAccount, drafts: Iterable[AdDraft]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for draft in drafts:
        assets = _assets(draft)
        items = assets.get("negative_keywords") if isinstance(assets.get("negative_keywords"), list) else []
        for item in items:
            keyword = item.get("keyword") if isinstance(item, dict) else item
            text = _normalize_keyword(keyword)
            if not text:
                continue
            match_type = str((item.get("match_type") if isinstance(item, dict) else "") or "exact").strip().title()
            key = (_campaign_name(draft), text)
            if key in seen:
                continue
            seen.add(key)
            row = _base_row(account, draft)
            row.update(
                {
                    "Action": "Add campaign negative keyword",
                    "Manual upload level": "Campaign",
                    "Manual target": "Campaign negative keyword",
                    "Editor entity": "Campaign negative keyword",
                    "Keyword": text,
                    "Match type": match_type,
                    "Google Ads Editor match type": f"Campaign Negative {match_type}",
                    "Status": "Enabled",
                    "Reason": item.get("reason") if isinstance(item, dict) else "",
                }
            )
            rows.append(row)
    return rows


def _url_inclusion_rows(account: GoogleAdsAccount, drafts: Iterable[AdDraft]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for draft in drafts:
        assets = _assets(draft)
        for item in assets.get("url_inclusion_targets") or []:
            raw_url = item.get("url") or item.get("final_url") if isinstance(item, dict) else item
            url = _canonical_url(raw_url)
            if not url:
                continue
            ad_group_name = _ad_group_name(draft)
            key = (_campaign_name(draft), ad_group_name, url)
            if key in seen:
                continue
            seen.add(key)
            row = _base_row(account, draft)
            row.update(
                {
                    "Action": "Add URL inclusion",
                    "Manual upload level": "Ad group",
                    "Manual target": "Dynamic/AI Max exact URL inclusion",
                    "Editor entity": "Dynamic ad target",
                    "URL": url,
                    "URL rule condition": "URL",
                    "URL rule match": "Exact URL",
                    "Status": "Enabled",
                    "Source label": item.get("source_label") if isinstance(item, dict) else "",
                    "Score": item.get("score") if isinstance(item, dict) else "",
                    "Conversions": item.get("conversions") if isinstance(item, dict) else "",
                    "Clicks": item.get("clicks") if isinstance(item, dict) else "",
                }
            )
            rows.append(row)
    return rows


def _url_exclusion_rows(account: GoogleAdsAccount, drafts: Iterable[AdDraft]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for draft in drafts:
        assets = _assets(draft)
        for item in assets.get("negative_page_targets") or []:
            raw_url = item.get("url") if isinstance(item, dict) else item
            url = _canonical_url(raw_url)
            if not url:
                continue
            key = (_campaign_name(draft), url)
            if key in seen:
                continue
            seen.add(key)
            row = _base_row(account, draft)
            row.update(
                {
                    "Action": "Add URL exclusion",
                    "Manual upload level": "Campaign",
                    "Manual target": "Campaign URL exclusion",
                    "Editor entity": "Campaign URL exclusion",
                    "URL": url,
                    "URL rule condition": "URL",
                    "URL rule match": "Exact URL",
                    "Status": "Enabled",
                    "Reason": item.get("reason") if isinstance(item, dict) else "",
                }
            )
            rows.append(row)
    return rows


def manual_export_rows(account: GoogleAdsAccount, drafts: list[AdDraft], kind: str) -> list[dict[str, Any]]:
    normalized = str(kind or "keywords").strip().lower().replace("-", "_")
    if normalized in {"keyword", "keywords", "positive_keywords"}:
        return _sort_manual_rows(_keyword_rows(account, drafts))
    if normalized in {"negative_keyword", "negative_keywords", "keyword_exclusions"}:
        return _sort_manual_rows(_negative_keyword_rows(account, drafts))
    if normalized in {"url", "urls", "url_inclusion", "url_inclusions", "inclusions"}:
        return _sort_manual_rows(_url_inclusion_rows(account, drafts))
    if normalized in {"url_exclusion", "url_exclusions", "exclusions", "negative_urls"}:
        return _sort_manual_rows(_url_exclusion_rows(account, drafts))
    raise ValueError(f"Unsupported manual export kind: {kind}")


def _sort_manual_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    target_rank = {
        "rsa exact keyword": 10,
        "dynamic/ai max exact url inclusion": 20,
        "pmax search theme": 30,
        "campaign negative keyword": 40,
        "campaign url exclusion": 50,
    }
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("Category") or "").lower(),
            str(row.get("Ad type") or "").lower(),
            str(row.get("Campaign lane") or "").lower(),
            str(row.get("Campaign") or "").lower(),
            target_rank.get(str(row.get("Manual target") or "").lower(), 99),
            str(row.get("Ad group") or row.get("Asset group") or "").lower(),
            str(row.get("Keyword") or row.get("URL") or "").lower(),
        ),
    )


def _manual_csv_fields(rows: list[dict[str, Any]]) -> list[str]:
    preferred = [
        "Account",
        "Customer ID",
        "Campaign",
        "Ad group",
        "Asset group",
        "Category",
        "Campaign lane",
        "Ad type",
        "Campaign code",
        "Manual file group",
        "Manual upload level",
        "Manual target",
        "Editor entity",
        "Action",
        "Keyword",
        "Match type",
        "Google Ads Editor match type",
        "URL",
        "URL rule condition",
        "URL rule match",
        "Status",
        "Source label",
        "Score",
        "Conversions",
        "Clicks",
        "Reason",
        "Draft ID",
        "Draft status",
        "Automation source",
    ]
    keys: set[str] = set()
    for row in rows:
        keys.update(row.keys())
    if not keys:
        return preferred
    return [field for field in preferred if field in keys] + sorted(keys.difference(preferred))


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    output = io.StringIO()
    fields = _manual_csv_fields(rows)
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def _campaign_wise_zip_bytes(rows: list[dict[str, Any]], *, export_label: str, account_slug: str, date_label: str) -> bytes:
    payload = io.BytesIO()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        group = str(row.get("Manual file group") or _slug(str(row.get("Campaign") or "campaign"))).strip()
        grouped.setdefault(group, []).append(row)
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"00-master-{export_label}-{account_slug}-{date_label}.csv", _csv_bytes(rows))
        manifest_rows = [
            {
                "Manual file group": group,
                "Campaign": group_rows[0].get("Campaign") or "",
                "Category": group_rows[0].get("Category") or "",
                "Campaign lane": group_rows[0].get("Campaign lane") or "",
                "Ad type": group_rows[0].get("Ad type") or "",
                "Row count": len(group_rows),
            }
            for group, group_rows in sorted(grouped.items())
        ]
        archive.writestr("00-manifest.csv", _csv_bytes(manifest_rows))
        for index, (group, group_rows) in enumerate(sorted(grouped.items()), start=1):
            archive.writestr(f"{index:02d}-{group}/{export_label}.csv", _csv_bytes(group_rows))
    return payload.getvalue()


def build_manual_automation_export(
    session: Session,
    account: GoogleAdsAccount,
    *,
    kind: str,
    grouped: bool = False,
) -> ManualExportFile:
    normalized = str(kind or "keywords").strip().lower().replace("-", "_")
    label_by_kind = {
        "keywords": "keywords",
        "positive_keywords": "keywords",
        "keyword": "keywords",
        "negative_keywords": "negative-keywords",
        "negative_keyword": "negative-keywords",
        "keyword_exclusions": "negative-keywords",
        "url_inclusions": "url-inclusions",
        "url_inclusion": "url-inclusions",
        "inclusions": "url-inclusions",
        "urls": "url-inclusions",
        "url_exclusions": "url-exclusions",
        "url_exclusion": "url-exclusions",
        "exclusions": "url-exclusions",
        "negative_urls": "url-exclusions",
    }
    export_label = label_by_kind.get(normalized)
    if export_label is None:
        raise ValueError(f"Unsupported manual export kind: {kind}")
    drafts = automation_drafts_for_manual_export(session, account)
    rows = manual_export_rows(account, drafts, normalized)
    date_label = datetime.utcnow().strftime("%Y-%m-%d")
    account_slug = _slug(account.name)
    if grouped:
        filename = f"automation-{export_label}-by-campaign-{account_slug}-{account.customer_id or account.id}-{date_label}.zip"
        return ManualExportFile(
            filename=filename,
            content=_campaign_wise_zip_bytes(rows, export_label=export_label, account_slug=account_slug, date_label=date_label),
            content_type="application/zip",
        )
    filename = f"automation-{export_label}-{account_slug}-{account.customer_id or account.id}-{date_label}.csv"
    return ManualExportFile(filename=filename, content=_csv_bytes(rows))


def confirm_manual_automation_upload(
    session: Session,
    account: GoogleAdsAccount,
    *,
    user_id: Optional[int] = None,
    note: str = "",
) -> dict[str, Any]:
    drafts = automation_drafts_for_manual_export(session, account)
    confirmed_at = datetime.utcnow().isoformat() + "Z"
    counts_by_type: dict[str, int] = {}
    for draft in drafts:
        assets = dict(_assets(draft))
        ad_type = str(draft.ad_type or "unknown").lower()
        counts_by_type[ad_type] = counts_by_type.get(ad_type, 0) + 1
        identity = _identity(draft)
        campaign_name = _campaign_name(draft)
        campaign_code = _campaign_code(draft)
        confirmation = {
            "status": "confirmed",
            "confirmed_at_utc": confirmed_at,
            "confirmed_by_user_id": user_id,
            "source": "google_ads_editor_manual_upload",
            "note": _clean_text(note, max_len=500),
            "campaign_name": campaign_name,
            "campaign_code": campaign_code,
            "ad_type": ad_type,
            "automation_source": _automation_source_key(draft),
        }
        assets["manual_upload"] = confirmation
        assets["live_publish"] = {
            "status": "manual_uploaded_confirmed",
            "reason": "User confirmed the latest automation export was uploaded manually through Google Ads Editor.",
            "confirmed_at_utc": confirmed_at,
            "confirmed_by_user_id": user_id,
            "campaign_name": campaign_name,
            "campaign_code": campaign_code,
            "ad_type": ad_type,
            "automation_source": _automation_source_key(draft),
            "campaign_identity": identity,
        }
        draft.generated_assets = assets
        draft.status = "published_enabled"
        session.add(draft)
    session.flush()
    return {
        "status": "confirmed",
        "account_id": account.id,
        "customer_id": account.customer_id,
        "confirmed_at_utc": confirmed_at,
        "confirmed_by_user_id": user_id,
        "draft_count": len(drafts),
        "counts_by_type": counts_by_type,
    }
