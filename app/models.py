from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class RunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class AccountStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class BackgroundJobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancel_requested = "cancel_requested"
    canceled = "canceled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    __tablename__ = "app_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_app_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(160), index=True)
    value: Mapped[Any] = mapped_column(JSONB)
    category: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str] = mapped_column(String(160))
    help_text: Mapped[str] = mapped_column(Text)
    input_type: Mapped[str] = mapped_column(String(40), default="text")
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class GoogleAdsConnection(Base):
    __tablename__ = "google_ads_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    developer_token: Mapped[str] = mapped_column(Text, default="")
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    api_version: Mapped[str] = mapped_column(String(40), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_oauth_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovered_manager_count: Mapped[int] = mapped_column(Integer, default=0)
    discovered_account_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    accounts: Mapped[List["GoogleAdsAccount"]] = relationship(back_populates="connection", lazy="selectin")


class GoogleAnalyticsConnection(Base):
    __tablename__ = "google_analytics_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_oauth_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovered_account_count: Mapped[int] = mapped_column(Integer, default=0)
    discovered_property_count: Mapped[int] = mapped_column(Integer, default=0)
    discovered_stream_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    properties: Mapped[List["GoogleAnalyticsProperty"]] = relationship(back_populates="connection", lazy="selectin")


class GoogleAnalyticsProperty(Base):
    __tablename__ = "google_analytics_properties"
    __table_args__ = (UniqueConstraint("property_resource_name", name="uq_google_analytics_property_resource"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("google_analytics_connections.id"), index=True)
    account_resource_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    account_display_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    property_resource_name: Mapped[str] = mapped_column(String(120), index=True)
    property_id: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    property_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    industry_category: Mapped[str] = mapped_column(String(120), default="", index=True)
    time_zone: Mapped[str] = mapped_column(String(120), default="", index=True)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_pull_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    connection: Mapped[GoogleAnalyticsConnection] = relationship(back_populates="properties", lazy="joined")
    streams: Mapped[List["GoogleAnalyticsWebStream"]] = relationship(back_populates="property", lazy="selectin")


class GoogleAnalyticsWebStream(Base):
    __tablename__ = "google_analytics_web_streams"
    __table_args__ = (UniqueConstraint("stream_resource_name", name="uq_google_analytics_web_stream_resource"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analytics_property_id: Mapped[int] = mapped_column(ForeignKey("google_analytics_properties.id"), index=True)
    stream_resource_name: Mapped[str] = mapped_column(String(160), index=True)
    stream_id: Mapped[str] = mapped_column(String(80), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    default_uri: Mapped[str] = mapped_column(String(1000), default="", index=True)
    measurement_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    property: Mapped[GoogleAnalyticsProperty] = relationship(back_populates="streams", lazy="joined")


class GoogleAnalyticsWebsiteMapping(Base):
    __tablename__ = "google_analytics_website_mappings"
    __table_args__ = (
        UniqueConstraint(
            "analytics_property_id",
            "analytics_stream_id",
            "store_id",
            "website_id",
            "account_id",
            name="uq_google_analytics_website_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analytics_property_id: Mapped[int] = mapped_column(ForeignKey("google_analytics_properties.id"), index=True)
    analytics_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_analytics_web_streams.id"),
        nullable=True,
        index=True,
    )
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("odoo_stores.id"), nullable=True, index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    match_source: Mapped[str] = mapped_column(String(80), default="manual", index=True)
    match_confidence: Mapped[float] = mapped_column(Float, default=1.0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    property: Mapped[GoogleAnalyticsProperty] = relationship(lazy="joined")
    stream: Mapped[Optional[GoogleAnalyticsWebStream]] = relationship(lazy="joined")
    store: Mapped[Optional["OdooStore"]] = relationship(lazy="joined")
    account: Mapped[Optional["GoogleAdsAccount"]] = relationship(lazy="joined")


class GoogleAnalyticsDataSnapshot(Base):
    __tablename__ = "google_analytics_data_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "target_key",
            "dataset_key",
            "scope_key",
            "schema_version",
            "query_hash",
            name="uq_google_analytics_data_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_key: Mapped[str] = mapped_column(String(200), index=True)
    analytics_property_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_analytics_properties.id"),
        nullable=True,
        index=True,
    )
    analytics_stream_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_analytics_web_streams.id"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    dataset_key: Mapped[str] = mapped_column(String(100), index=True)
    scope_key: Mapped[str] = mapped_column(String(180), default="", index=True)
    query_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    property: Mapped[Optional[GoogleAnalyticsProperty]] = relationship(lazy="joined")
    stream: Mapped[Optional[GoogleAnalyticsWebStream]] = relationship(lazy="joined")
    account: Mapped[Optional["GoogleAdsAccount"]] = relationship(lazy="joined")
    source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsAccount(Base):
    __tablename__ = "google_ads_accounts"
    __table_args__ = (UniqueConstraint("customer_id", name="uq_google_ads_accounts_customer_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_ads_connections.id"),
        nullable=True,
        index=True,
    )
    manager_name: Mapped[str] = mapped_column(String(255), index=True)
    manager_customer_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    customer_id: Mapped[str] = mapped_column(String(32), index=True)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    source_label: Mapped[str] = mapped_column(String(120), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    connection: Mapped[Optional[GoogleAdsConnection]] = relationship(back_populates="accounts", lazy="joined")


class GoogleAdsCampaignMetric(Base):
    __tablename__ = "google_ads_campaign_metrics"
    __table_args__ = (
        UniqueConstraint("account_id", "metric_date", "campaign_id", name="uq_google_ads_campaign_metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), index=True)
    campaign_status: Mapped[str] = mapped_column(String(80), default="UNKNOWN", index=True)
    channel_type: Mapped[str] = mapped_column(String(80), index=True)
    bidding_strategy_type: Mapped[str] = mapped_column(String(80), index=True)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    conversions: Mapped[float] = mapped_column(Float, default=0)
    conversions_value: Mapped[float] = mapped_column(Float, default=0)
    all_conversions: Mapped[float] = mapped_column(Float, default=0)
    all_conversions_value: Mapped[float] = mapped_column(Float, default=0)
    target_roas: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    budget_amount_micros: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class GoogleAdsAccountDailyMetric(Base):
    __tablename__ = "google_ads_account_daily_metrics"
    __table_args__ = (
        UniqueConstraint("account_id", "metric_date", name="uq_google_ads_account_daily_metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    conversions: Mapped[float] = mapped_column(Float, default=0)
    conversions_value: Mapped[float] = mapped_column(Float, default=0)
    all_conversions: Mapped[float] = mapped_column(Float, default=0)
    all_conversions_value: Mapped[float] = mapped_column(Float, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class GoogleAdsConversionGoalSnapshot(Base):
    __tablename__ = "google_ads_conversion_goal_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "level",
            "campaign_id",
            "category",
            "origin",
            name="uq_google_ads_conversion_goal_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    level: Mapped[str] = mapped_column(String(40), index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    campaign_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(120), index=True)
    origin: Mapped[str] = mapped_column(String(120), index=True)
    biddable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, index=True)
    goal_config_level: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    custom_conversion_goal: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class GoogleAdsDataSnapshot(Base):
    __tablename__ = "google_ads_data_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "target_key",
            "dataset_key",
            "scope_key",
            "schema_version",
            "query_hash",
            name="uq_google_ads_data_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_key: Mapped[str] = mapped_column(String(160), index=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    connection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_ads_connections.id"),
        nullable=True,
        index=True,
    )
    source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    dataset_key: Mapped[str] = mapped_column(String(80), index=True)
    scope_key: Mapped[str] = mapped_column(String(160), default="", index=True)
    query_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    account: Mapped[Optional[GoogleAdsAccount]] = relationship(lazy="joined")
    connection: Mapped[Optional[GoogleAdsConnection]] = relationship(lazy="joined")
    source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsKeywordCandidate(Base):
    __tablename__ = "google_ads_keyword_candidates"
    __table_args__ = (
        UniqueConstraint("account_id", "normalized_keyword", name="uq_google_ads_keyword_candidate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    keyword: Mapped[str] = mapped_column(String(160), index=True)
    normalized_keyword: Mapped[str] = mapped_column(String(160), index=True)
    quality_label: Mapped[str] = mapped_column(String(40), default="clicked", index=True)
    review_status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    match_type: Mapped[str] = mapped_column(String(40), default="exact", index=True)
    source_dataset_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_scope_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    ad_group_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    conversion_value: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions_value: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsLandingPageCandidate(Base):
    __tablename__ = "google_ads_landing_page_candidates"
    __table_args__ = (
        UniqueConstraint("account_id", "normalized_url_hash", name="uq_google_ads_landing_page_candidate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    normalized_url: Mapped[str] = mapped_column(Text)
    normalized_url_hash: Mapped[str] = mapped_column(String(64), index=True)
    quality_label: Mapped[str] = mapped_column(String(40), default="clicked", index=True)
    review_status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    source_dataset_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_scope_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    channel_types: Mapped[List[str]] = mapped_column(JSONB, default=list)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    conversion_value: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions_value: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAnalyticsSearchTermCandidate(Base):
    __tablename__ = "google_analytics_search_term_candidates"
    __table_args__ = (
        UniqueConstraint("account_id", "normalized_search_term", name="uq_google_analytics_search_term_candidate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    analytics_property_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_analytics_properties.id"), nullable=True, index=True)
    analytics_stream_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_analytics_web_streams.id"), nullable=True, index=True)
    search_term: Mapped[str] = mapped_column(String(240), index=True)
    normalized_search_term: Mapped[str] = mapped_column(String(240), index=True)
    keyword: Mapped[str] = mapped_column(String(240), default="", index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    quality_label: Mapped[str] = mapped_column(String(40), default="clicked", index=True)
    review_status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    source_dataset_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_scope_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    engaged_sessions: Mapped[int] = mapped_column(BigInteger, default=0)
    purchases: Mapped[float] = mapped_column(Float, default=0.0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    property: Mapped[Optional[GoogleAnalyticsProperty]] = relationship(lazy="joined")
    stream: Mapped[Optional[GoogleAnalyticsWebStream]] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleSearchConsoleConnection(Base):
    __tablename__ = "google_search_console_connections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    email: Mapped[str] = mapped_column(String(255), default="", index=True)
    client_id: Mapped[str] = mapped_column(Text, default="")
    client_secret: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_oauth_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_discovery_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    discovered_site_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    sites: Mapped[List["GoogleSearchConsoleSite"]] = relationship(back_populates="connection", lazy="selectin")


class GoogleSearchConsoleSite(Base):
    __tablename__ = "google_search_console_sites"
    __table_args__ = (UniqueConstraint("site_url", name="uq_google_search_console_site_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("google_search_console_connections.id"), index=True)
    site_url: Mapped[str] = mapped_column(String(1000), index=True)
    permission_level: Mapped[str] = mapped_column(String(80), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_discovery_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_pull_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    connection: Mapped[GoogleSearchConsoleConnection] = relationship(back_populates="sites", lazy="joined")


class GoogleSearchConsoleWebsiteMapping(Base):
    __tablename__ = "google_search_console_website_mappings"
    __table_args__ = (
        UniqueConstraint(
            "search_console_site_id",
            "store_id",
            "website_id",
            "account_id",
            name="uq_google_search_console_website_mapping",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    search_console_site_id: Mapped[int] = mapped_column(ForeignKey("google_search_console_sites.id"), index=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("odoo_stores.id"), nullable=True, index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    match_source: Mapped[str] = mapped_column(String(80), default="manual", index=True)
    match_confidence: Mapped[float] = mapped_column(Float, default=1.0, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    site: Mapped[GoogleSearchConsoleSite] = relationship(lazy="joined")
    store: Mapped[Optional["OdooStore"]] = relationship(lazy="joined")
    account: Mapped[Optional["GoogleAdsAccount"]] = relationship(lazy="joined")


class GoogleSearchConsoleDataSnapshot(Base):
    __tablename__ = "google_search_console_data_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "target_key",
            "dataset_key",
            "scope_key",
            "schema_version",
            "query_hash",
            name="uq_google_search_console_data_snapshot",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target_key: Mapped[str] = mapped_column(String(220), index=True)
    search_console_site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_search_console_sites.id"),
        nullable=True,
        index=True,
    )
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    dataset_key: Mapped[str] = mapped_column(String(100), index=True)
    scope_key: Mapped[str] = mapped_column(String(180), default="", index=True)
    query_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    schema_version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    site: Mapped[Optional[GoogleSearchConsoleSite]] = relationship(lazy="joined")
    account: Mapped[Optional["GoogleAdsAccount"]] = relationship(lazy="joined")
    source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleSearchConsoleQueryCandidate(Base):
    __tablename__ = "google_search_console_query_candidates"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "normalized_query",
            "page_url_hash",
            "country",
            "device",
            name="uq_google_search_console_query_candidate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    search_console_site_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_search_console_sites.id"),
        nullable=True,
        index=True,
    )
    query: Mapped[str] = mapped_column(String(240), index=True)
    normalized_query: Mapped[str] = mapped_column(String(240), index=True)
    page_url: Mapped[str] = mapped_column(Text)
    page_url_hash: Mapped[str] = mapped_column(String(64), index=True)
    country: Mapped[str] = mapped_column(String(16), default="", index=True)
    device: Mapped[str] = mapped_column(String(40), default="", index=True)
    quality_label: Mapped[str] = mapped_column(String(40), default="watch", index=True)
    review_status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    source_dataset_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_scope_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    site: Mapped[Optional[GoogleSearchConsoleSite]] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsNegativeKeywordCandidate(Base):
    __tablename__ = "google_ads_negative_keyword_candidates"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "scope_level",
            "campaign_id",
            "ad_group_id",
            "normalized_keyword",
            "match_type",
            name="uq_google_ads_negative_keyword_candidate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    scope_level: Mapped[str] = mapped_column(String(40), default="campaign", index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    ad_group_id: Mapped[int] = mapped_column(BigInteger, default=0, index=True)
    ad_group_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    keyword: Mapped[str] = mapped_column(String(160), index=True)
    normalized_keyword: Mapped[str] = mapped_column(String(160), index=True)
    match_type: Mapped[str] = mapped_column(String(40), default="exact", index=True)
    reason_label: Mapped[str] = mapped_column(String(80), default="review", index=True)
    review_status: Mapped[str] = mapped_column(String(40), default="new", index=True)
    guard_status: Mapped[str] = mapped_column(String(40), default="candidate", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    guard_reasons: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_dataset_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_scope_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    campaign_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    ad_group_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    ad_group_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    impressions: Mapped[int] = mapped_column(BigInteger, default=0)
    clicks: Mapped[int] = mapped_column(BigInteger, default=0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    conversions: Mapped[float] = mapped_column(Float, default=0.0)
    conversion_value: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    all_conversions_value: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsPolicyDisapprovalTerm(Base):
    __tablename__ = "google_ads_policy_disapproval_terms"
    __table_args__ = (
        UniqueConstraint("account_id", "normalized_term", "policy_topic", name="uq_google_ads_policy_disapproval_term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    term: Mapped[str] = mapped_column(String(160), index=True)
    normalized_term: Mapped[str] = mapped_column(String(160), index=True)
    policy_topic: Mapped[str] = mapped_column(String(160), default="Unapproved substances", index=True)
    approval_status: Mapped[str] = mapped_column(String(40), default="DISAPPROVED", index=True)
    guard_status: Mapped[str] = mapped_column(String(40), default="active", index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    google_shared_set_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    google_shared_criterion_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    negative_candidate_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_ads_negative_keyword_candidates.id"),
        nullable=True,
        index=True,
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_pulled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_source_job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    negative_candidate: Mapped[Optional[GoogleAdsNegativeKeywordCandidate]] = relationship(lazy="joined")
    last_source_job: Mapped[Optional["BackgroundJob"]] = relationship(lazy="joined")


class GoogleAdsAutomationPreference(Base):
    __tablename__ = "google_ads_automation_preferences"
    __table_args__ = (UniqueConstraint("account_id", name="uq_google_ads_automation_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    automation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    monitor_only: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    keyword_discovery_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    negative_keyword_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    audience_signal_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    landing_page_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auction_monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    odoo_sales_guard_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_apply_keywords_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_apply_negatives_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_create_campaigns_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_pause_campaigns_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_peak_budget_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    testing_bootstrap_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    testing_bootstrap_days: Mapped[int] = mapped_column(Integer, default=15)
    pmax_min_7d_conversions: Mapped[float] = mapped_column(Float, default=15.0)
    testing_sales_budget_ratio: Mapped[float] = mapped_column(Float, default=0.05)
    testing_keyword_limit: Mapped[int] = mapped_column(Integer, default=0)
    testing_landing_page_limit: Mapped[int] = mapped_column(Integer, default=0)
    peak_budget_increase_pct: Mapped[float] = mapped_column(Float, default=0.5)
    peak_budget_warmup_minutes: Mapped[int] = mapped_column(Integer, default=60)
    peak_budget_restore_delay_minutes: Mapped[int] = mapped_column(Integer, default=0)
    peak_budget_decision_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_peak_budget_decision_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    daily_keyword_lookback_days: Mapped[int] = mapped_column(Integer, default=120)
    all_time_refresh_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    api_call_budget_per_day: Mapped[int] = mapped_column(Integer, default=750)
    max_daily_api_rows: Mapped[int] = mapped_column(Integer, default=10000)
    mutation_cooldown_days: Mapped[int] = mapped_column(Integer, default=3)
    schedule_mode: Mapped[str] = mapped_column(String(40), default="dynamic_low_traffic", index=True)
    scheduled_hour: Mapped[int] = mapped_column(Integer, default=4)
    scheduled_minute: Mapped[int] = mapped_column(Integer, default=20)
    schedule_timezone: Mapped[str] = mapped_column(String(80), default="UTC")
    schedule_source: Mapped[str] = mapped_column(String(160), default="", index=True)
    schedule_decision_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_schedule_decision_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    odoo_sales_max_spend_ratio: Mapped[float] = mapped_column(Float, default=0.15)
    odoo_sales_guard_window_days: Mapped[int] = mapped_column(Integer, default=7)
    budget_guard_check_interval_hours: Mapped[int] = mapped_column(Integer, default=6)
    minimum_daily_budget_amount: Mapped[float] = mapped_column(Float, default=1.0)
    underperforming_budget_reduce_pct: Mapped[float] = mapped_column(Float, default=0.20)
    peak_budget_extra_spend_ratio: Mapped[float] = mapped_column(Float, default=0.05)
    peak_budget_check_interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    target_cost_per_conversion: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_roas: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    last_keyword_pull_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_all_time_pull_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_budget_guard_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_peak_budget_check_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_analysis_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    strategy_summary_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class OdooStore(Base):
    __tablename__ = "odoo_stores"
    __table_args__ = (UniqueConstraint("name", name="uq_odoo_stores_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    base_url: Mapped[str] = mapped_column(String(500))
    database: Mapped[str] = mapped_column(String(160))
    username: Mapped[str] = mapped_column(String(255))
    api_key: Mapped[str] = mapped_column(Text, default="")
    website_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    is_multisite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    mappings: Mapped[List["OdooStoreGoogleAdsMapping"]] = relationship(
        back_populates="store",
        lazy="selectin",
        cascade="all, delete-orphan",
    )
    websites: Mapped[List["OdooWebsite"]] = relationship(
        back_populates="store",
        lazy="selectin",
        cascade="all, delete-orphan",
    )


class OdooWebsite(Base):
    __tablename__ = "odoo_websites"
    __table_args__ = (
        UniqueConstraint("store_id", "website_id", name="uq_odoo_website_store_website"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    domain: Mapped[str] = mapped_column(String(500), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    store: Mapped[OdooStore] = relationship(back_populates="websites", lazy="joined")


class OdooStoreGoogleAdsMapping(Base):
    __tablename__ = "odoo_store_google_ads_mappings"
    __table_args__ = (
        UniqueConstraint("store_id", "website_id", "account_id", name="uq_odoo_store_google_ads_mapping"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    revenue_weight: Mapped[float] = mapped_column(Float, default=1.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    store: Mapped[OdooStore] = relationship(back_populates="mappings", lazy="joined")
    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class OdooSaleOrder(Base):
    __tablename__ = "odoo_sale_orders"
    __table_args__ = (
        UniqueConstraint("store_id", "odoo_order_id", name="uq_odoo_sale_order_store_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    odoo_order_id: Mapped[int] = mapped_column(Integer, index=True)
    order_name: Mapped[str] = mapped_column(String(160), index=True)
    order_datetime: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    state: Mapped[str] = mapped_column(String(40), index=True)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    amount_total: Mapped[float] = mapped_column(Float, default=0.0)
    website_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    margin_amount: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    margin_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    store: Mapped[OdooStore] = relationship(lazy="joined")


class GoogleAdsCustomerMatchPublication(Base):
    __tablename__ = "google_ads_customer_match_publications"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "store_id",
            "website_id",
            "list_kind",
            name="uq_google_ads_customer_match_publication",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    list_kind: Mapped[str] = mapped_column(String(40), default="purchasers", index=True)
    list_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    access_username: Mapped[str] = mapped_column(String(120), default="")
    access_password: Mapped[str] = mapped_column(String(120), default="")
    customer_match_policy_accepted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    data_manager_api_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    data_manager_user_list_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    data_manager_user_list_resource: Mapped[str] = mapped_column(String(255), default="")
    data_manager_user_list_name: Mapped[str] = mapped_column(String(255), default="")
    data_manager_status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    data_manager_last_request_id: Mapped[str] = mapped_column(String(120), default="")
    data_manager_last_error: Mapped[str] = mapped_column(Text, default="")
    data_manager_last_response_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    data_manager_last_pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_sync_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    store: Mapped[OdooStore] = relationship(lazy="joined")
    outbox_rows: Mapped[List["GoogleAdsCustomerMatchOutbox"]] = relationship(back_populates="publication", lazy="selectin")


class OdooCustomerMatchMember(Base):
    __tablename__ = "odoo_customer_match_members"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "store_id",
            "website_id",
            "customer_key",
            name="uq_odoo_customer_match_member",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    odoo_partner_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    customer_key: Mapped[str] = mapped_column(String(64), index=True)
    hashed_email: Mapped[str] = mapped_column(String(64), default="", index=True)
    hashed_phone: Mapped[str] = mapped_column(String(64), default="", index=True)
    hashed_first_name: Mapped[str] = mapped_column(String(64), default="")
    hashed_last_name: Mapped[str] = mapped_column(String(64), default="")
    country_code: Mapped[str] = mapped_column(String(3), default="", index=True)
    zip_code: Mapped[str] = mapped_column(String(40), default="")
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    total_value: Mapped[float] = mapped_column(Float, default=0.0)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    first_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    source_order_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    store: Mapped[OdooStore] = relationship(lazy="joined")
    outbox_rows: Mapped[List["GoogleAdsCustomerMatchOutbox"]] = relationship(back_populates="member", lazy="selectin")


class GoogleAdsCustomerMatchOutbox(Base):
    __tablename__ = "google_ads_customer_match_outbox"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "member_id",
            "action",
            name="uq_google_ads_customer_match_outbox",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("google_ads_customer_match_publications.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("odoo_customer_match_members.id"), index=True)
    action: Mapped[str] = mapped_column(String(20), default="add", index=True)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_request_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    publication: Mapped[GoogleAdsCustomerMatchPublication] = relationship(back_populates="outbox_rows", lazy="joined")
    member: Mapped[OdooCustomerMatchMember] = relationship(back_populates="outbox_rows", lazy="joined")


class OdooProductPageSignal(Base):
    __tablename__ = "odoo_product_page_signals"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "website_id",
            "account_id",
            "product_code",
            "product_url_hash",
            name="uq_odoo_product_page_signal",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    domain: Mapped[str] = mapped_column(String(500), default="", index=True)
    product_code: Mapped[str] = mapped_column(String(160), default="", index=True)
    product_name: Mapped[str] = mapped_column(String(500), default="", index=True)
    product_url: Mapped[str] = mapped_column(String(1000), default="")
    product_url_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    currency_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    sales_amount: Mapped[float] = mapped_column(Float, default=0.0)
    margin_amount: Mapped[float] = mapped_column(Float, default=0.0)
    margin_percent: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    google_cost: Mapped[float] = mapped_column(Float, default=0.0)
    google_clicks: Mapped[int] = mapped_column(Integer, default=0)
    google_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    google_conversion_value: Mapped[float] = mapped_column(Float, default=0.0)
    zero_conversion_cost: Mapped[float] = mapped_column(Float, default=0.0)
    label: Mapped[str] = mapped_column(String(40), default="watch", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    store: Mapped[OdooStore] = relationship(lazy="joined")
    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class GoogleAdsPageFeedPublication(Base):
    __tablename__ = "google_ads_page_feed_publications"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "store_id",
            "website_id",
            "feed_kind",
            name="uq_google_ads_page_feed_publication",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("odoo_stores.id"), index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    feed_kind: Mapped[str] = mapped_column(String(40), default="best", index=True)
    asset_set_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    asset_set_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    last_publish_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    store: Mapped[OdooStore] = relationship(lazy="joined")


class GoogleAdsPageFeedAsset(Base):
    __tablename__ = "google_ads_page_feed_assets"
    __table_args__ = (
        UniqueConstraint("publication_id", "page_url_hash", name="uq_google_ads_page_feed_asset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("google_ads_page_feed_publications.id"), index=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("odoo_product_page_signals.id"), nullable=True, index=True)
    page_url: Mapped[str] = mapped_column(String(1000), default="")
    page_url_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    labels: Mapped[List[str]] = mapped_column(JSONB, default=list)
    asset_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    asset_set_asset_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    publication: Mapped[GoogleAdsPageFeedPublication] = relationship(lazy="joined")
    signal: Mapped[Optional[OdooProductPageSignal]] = relationship(lazy="joined")


class GoogleAdsPageFeedCampaignLink(Base):
    __tablename__ = "google_ads_page_feed_campaign_links"
    __table_args__ = (
        UniqueConstraint("publication_id", "campaign_id", name="uq_google_ads_page_feed_campaign_link"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("google_ads_page_feed_publications.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(BigInteger, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    campaign_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    channel_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    campaign_asset_set_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    dsa_criterion_resource_names: Mapped[List[str]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    publication: Mapped[GoogleAdsPageFeedPublication] = relationship(lazy="joined")


class SpendGuardSnapshot(Base):
    __tablename__ = "spend_guard_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(ForeignKey("google_ads_accounts.id"), nullable=True, index=True)
    guard_window_hours: Mapped[int] = mapped_column(Integer, default=12, index=True)
    status: Mapped[str] = mapped_column(String(40), index=True)
    sales_inr: Mapped[float] = mapped_column(Float, default=0.0)
    margin_inr: Mapped[float] = mapped_column(Float, default=0.0)
    ad_cost_inr: Mapped[float] = mapped_column(Float, default=0.0)
    spend_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    target_ratio: Mapped[float] = mapped_column(Float, default=0.15)
    max_ratio: Mapped[float] = mapped_column(Float, default=0.20)
    details: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[Optional[GoogleAdsAccount]] = relationship(lazy="joined")


class RazorpayDailyReceipt(Base):
    __tablename__ = "razorpay_daily_receipts"
    __table_args__ = (
        UniqueConstraint("receipt_date", "currency", name="uq_razorpay_daily_receipt"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_date: Mapped[date] = mapped_column(Date, index=True)
    currency: Mapped[str] = mapped_column(String(12), index=True)
    captured_amount_subunits: Mapped[int] = mapped_column(BigInteger, default=0)
    refunded_amount_subunits: Mapped[int] = mapped_column(BigInteger, default=0)
    fee_subunits: Mapped[int] = mapped_column(BigInteger, default=0)
    tax_subunits: Mapped[int] = mapped_column(BigInteger, default=0)
    captured_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    authorized_count: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CurrencyRateSnapshot(Base):
    __tablename__ = "currency_rate_snapshots"
    __table_args__ = (
        UniqueConstraint("rate_date", "base_currency", "source", name="uq_currency_rate_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rate_date: Mapped[date] = mapped_column(Date, index=True)
    base_currency: Mapped[str] = mapped_column(String(12), default="USD", index=True)
    source: Mapped[str] = mapped_column(String(80), default="openexchangerates", index=True)
    rates: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class CostDashboardSnapshot(Base):
    __tablename__ = "cost_dashboard_snapshots"
    __table_args__ = (UniqueConstraint("days", name="uq_cost_dashboard_snapshots_days"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    days: Mapped[int] = mapped_column(Integer, index=True)
    data: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )


class BackgroundJob(Base):
    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(120), index=True)
    label: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[BackgroundJobStatus] = mapped_column(
        Enum(BackgroundJobStatus),
        default=BackgroundJobStatus.queued,
        index=True,
    )
    requested_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    message_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    requested_by: Mapped[Optional[User]] = relationship(lazy="joined")


class GoogleAdsApiError(Base):
    __tablename__ = "google_ads_api_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_ads_accounts.id"),
        nullable=True,
        index=True,
    )
    connection_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("google_ads_connections.id"),
        nullable=True,
        index=True,
    )
    job_id: Mapped[Optional[int]] = mapped_column(ForeignKey("background_jobs.id"), nullable=True, index=True)
    context: Mapped[str] = mapped_column(String(120), default="google_ads", index=True)
    severity: Mapped[str] = mapped_column(String(40), default="error", index=True)
    error_code: Mapped[str] = mapped_column(String(180), default="", index=True)
    request_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[Optional[GoogleAdsAccount]] = relationship(lazy="joined")
    connection: Mapped[Optional[GoogleAdsConnection]] = relationship(lazy="joined")
    job: Mapped[Optional[BackgroundJob]] = relationship(lazy="joined")


class AutoPilotEvent(Base):
    __tablename__ = "autopilot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    campaign_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    action_type: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", index=True)
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    result_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class AdDraft(Base):
    __tablename__ = "ad_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    ad_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    website_url: Mapped[str] = mapped_column(String(500))
    final_url: Mapped[str] = mapped_column(String(500))
    business_name: Mapped[str] = mapped_column(String(120), default="")
    prompt: Mapped[str] = mapped_column(Text)
    generated_assets: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    validation_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    created_by: Mapped[Optional[User]] = relationship(lazy="joined")


class BrowserAutomationTask(Base):
    __tablename__ = "browser_automation_tasks"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_key", name="uq_browser_automation_task_account_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    draft_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ad_drafts.id"), nullable=True, index=True)
    action_type: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    ad_group_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    asset_group_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    dedupe_key: Mapped[str] = mapped_column(String(80), index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    step_order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    result_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    claimed_by: Mapped[str] = mapped_column(String(160), default="", index=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    draft: Mapped[Optional[AdDraft]] = relationship(lazy="joined")


class CampaignOptimizationRun(Base):
    __tablename__ = "campaign_optimization_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    requested_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    campaign_type: Mapped[str] = mapped_column(String(40), default="all", index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    days: Mapped[int] = mapped_column(Integer, default=30)
    max_rows: Mapped[int] = mapped_column(Integer, default=3000)
    force_refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    use_openai: Mapped[bool] = mapped_column(Boolean, default=True)
    apply_local_feed_labels: Mapped[bool] = mapped_column(Boolean, default=True)
    summary_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    actions_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_snapshot_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    prompt: Mapped[str] = mapped_column(Text, default="")
    openai_response_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    requested_by: Mapped[Optional[User]] = relationship(lazy="joined")


class GoogleAdsAssetAutomationPreference(Base):
    __tablename__ = "google_ads_asset_automation_preferences"
    __table_args__ = (UniqueConstraint("account_id", name="uq_google_ads_asset_automation_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    auto_discount_promotions_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_coupon_promotions_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_sitelinks_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_structured_snippets_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_pmax_search_themes_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_callouts_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_price_assets_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_business_messages_enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    auto_campaign_asset_mapping_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    auto_ad_group_asset_mapping_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    whatsapp_country_code: Mapped[str] = mapped_column(String(8), default="+1")
    whatsapp_phone_number: Mapped[str] = mapped_column(String(40), default="+14434007587")
    whatsapp_starter_message: Mapped[str] = mapped_column(String(140), default="Can I get help choosing a product?")
    whatsapp_call_to_action: Mapped[str] = mapped_column(String(40), default="MESSAGE")
    whatsapp_call_to_action_description: Mapped[str] = mapped_column(String(30), default="Chat with us")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class GoogleAdsGeneratedAsset(Base):
    __tablename__ = "google_ads_generated_assets"
    __table_args__ = (
        UniqueConstraint("account_id", "asset_type", "source_key", name="uq_google_ads_generated_asset_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    created_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    asset_type: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(255), default="", index=True)
    source_type: Mapped[str] = mapped_column(String(80), default="", index=True)
    source_key: Mapped[str] = mapped_column(String(160), index=True)
    status: Mapped[str] = mapped_column(String(40), default="draft", index=True)
    google_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    payload_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )
    last_generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    created_by: Mapped[Optional[User]] = relationship(lazy="joined")


class GoogleAdsBrandListCandidate(Base):
    __tablename__ = "google_ads_brand_list_candidates"
    __table_args__ = (
        UniqueConstraint("account_id", "website_id", "normalized_brand", name="uq_google_ads_brand_list_candidate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("odoo_stores.id"), nullable=True, index=True)
    website_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    website_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    website_domain: Mapped[str] = mapped_column(String(500), default="", index=True)
    country_code: Mapped[str] = mapped_column(String(12), default="", index=True)
    brand_name: Mapped[str] = mapped_column(String(255), index=True)
    normalized_brand: Mapped[str] = mapped_column(String(160), index=True)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    sales_amount: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_brand_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    suggested_brand_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    suggested_primary_url: Mapped[str] = mapped_column(String(1000), default="", index=True)
    suggested_urls: Mapped[List[str]] = mapped_column(JSONB, default=list)
    google_brand_state: Mapped[str] = mapped_column(String(80), default="", index=True)
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    match_status: Mapped[str] = mapped_column(String(40), default="needs_review", index=True)
    source_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    candidate_json: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    google_shared_set_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    google_shared_criterion_resource_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        index=True,
    )

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
    store: Mapped[Optional["OdooStore"]] = relationship(lazy="joined")


class StrategyRecommendation(Base):
    __tablename__ = "strategy_recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    campaign_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    campaign_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recommendation_type: Mapped[str] = mapped_column(String(120), index=True)
    severity: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    summary: Mapped[str] = mapped_column(Text)
    evidence: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="proposed", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    mode: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    details: Mapped[Dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_destructive: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.queued, index=True)
    requested_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
    strategy_keys: Mapped[List[str]] = mapped_column(JSONB, default=list)
    account_ids: Mapped[List[int]] = mapped_column(JSONB, default=list)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    accounts: Mapped[List["StrategyRunAccount"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class StrategyRunAccount(Base):
    __tablename__ = "strategy_run_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_ads_accounts.id"), index=True)
    strategy_key: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.queued, index=True)
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    optimizer_state_json: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped[StrategyRun] = relationship(back_populates="accounts")
    account: Mapped[GoogleAdsAccount] = relationship(lazy="joined")
