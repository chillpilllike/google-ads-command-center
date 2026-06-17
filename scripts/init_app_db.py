#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_settings
from app.database import AsyncSessionLocal, Base, engine
from app.seed import seed_database
from sqlalchemy import text


async def init_app_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS category VARCHAR(120) NOT NULL DEFAULT 'General'")
        )
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS label VARCHAR(160) NOT NULL DEFAULT ''")
        )
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS help_text TEXT NOT NULL DEFAULT ''")
        )
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS input_type VARCHAR(40) NOT NULL DEFAULT 'text'")
        )
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS sensitive BOOLEAN NOT NULL DEFAULT false")
        )
        await conn.execute(
            text("ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_app_settings_category ON app_settings (category)")
        )
        await conn.execute(
            text("ALTER TABLE strategy_run_accounts ADD COLUMN IF NOT EXISTS report_json JSONB")
        )
        await conn.execute(
            text("ALTER TABLE strategy_run_accounts ADD COLUMN IF NOT EXISTS optimizer_state_json JSONB")
        )
        await conn.execute(
            text("ALTER TABLE strategies ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_accounts ADD COLUMN IF NOT EXISTS currency_code VARCHAR(12) DEFAULT ''")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_accounts ADD COLUMN IF NOT EXISTS connection_id INTEGER")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_accounts ADD COLUMN IF NOT EXISTS source_label VARCHAR(120) DEFAULT ''")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_accounts_connection_id ON google_ads_accounts (connection_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_accounts_source_label ON google_ads_accounts (source_label)")
        )
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint WHERE conname = 'fk_google_ads_accounts_connection_id'"
                ") THEN "
                "ALTER TABLE google_ads_accounts "
                "ADD CONSTRAINT fk_google_ads_accounts_connection_id "
                "FOREIGN KEY (connection_id) REFERENCES google_ads_connections(id); "
                "END IF; END $$;"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_accounts_currency_code ON google_ads_accounts (currency_code)")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_campaign_metrics ADD COLUMN IF NOT EXISTS campaign_status VARCHAR(80) DEFAULT 'UNKNOWN'")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_campaign_metrics_campaign_status ON google_ads_campaign_metrics (campaign_status)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_account_daily_metrics ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "metric_date DATE NOT NULL, "
                "currency_code VARCHAR(12) NOT NULL DEFAULT '', "
                "cost_micros BIGINT NOT NULL DEFAULT 0, "
                "impressions BIGINT NOT NULL DEFAULT 0, "
                "clicks BIGINT NOT NULL DEFAULT 0, "
                "conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_account_daily_metric UNIQUE (account_id, metric_date)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_account_daily_metrics_account_id ON google_ads_account_daily_metrics (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_account_daily_metrics_metric_date ON google_ads_account_daily_metrics (metric_date)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_account_daily_metrics_currency_code ON google_ads_account_daily_metrics (currency_code)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_account_daily_metrics_synced_at ON google_ads_account_daily_metrics (synced_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_api_errors ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER REFERENCES google_ads_accounts(id), "
                "connection_id INTEGER REFERENCES google_ads_connections(id), "
                "job_id INTEGER REFERENCES background_jobs(id), "
                "context VARCHAR(120) NOT NULL DEFAULT 'google_ads', "
                "severity VARCHAR(40) NOT NULL DEFAULT 'error', "
                "error_code VARCHAR(180) NOT NULL DEFAULT '', "
                "request_id VARCHAR(120) NOT NULL DEFAULT '', "
                "message TEXT NOT NULL, "
                "details JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "acknowledged BOOLEAN NOT NULL DEFAULT false, "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now()"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_account_id ON google_ads_api_errors (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_connection_id ON google_ads_api_errors (connection_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_job_id ON google_ads_api_errors (job_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_context ON google_ads_api_errors (context)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_severity ON google_ads_api_errors (severity)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_error_code ON google_ads_api_errors (error_code)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_request_id ON google_ads_api_errors (request_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_acknowledged ON google_ads_api_errors (acknowledged)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_api_errors_created_at ON google_ads_api_errors (created_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS currency_rate_snapshots ("
                "id SERIAL PRIMARY KEY, "
                "rate_date DATE NOT NULL, "
                "base_currency VARCHAR(12) NOT NULL DEFAULT 'USD', "
                "source VARCHAR(80) NOT NULL DEFAULT 'openexchangerates', "
                "rates JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "error TEXT, "
                "CONSTRAINT uq_currency_rate_snapshot UNIQUE (rate_date, base_currency, source)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_currency_rate_snapshots_rate_date ON currency_rate_snapshots (rate_date)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_currency_rate_snapshots_base_currency ON currency_rate_snapshots (base_currency)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_currency_rate_snapshots_source ON currency_rate_snapshots (source)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_currency_rate_snapshots_fetched_at ON currency_rate_snapshots (fetched_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_data_snapshots ("
                "id SERIAL PRIMARY KEY, "
                "target_key VARCHAR(160) NOT NULL, "
                "account_id INTEGER REFERENCES google_ads_accounts(id), "
                "connection_id INTEGER REFERENCES google_ads_connections(id), "
                "source_job_id INTEGER REFERENCES background_jobs(id), "
                "dataset_key VARCHAR(80) NOT NULL, "
                "scope_key VARCHAR(160) NOT NULL DEFAULT '', "
                "query_hash VARCHAR(64) NOT NULL DEFAULT '', "
                "schema_version INTEGER NOT NULL DEFAULT 1, "
                "row_count INTEGER NOT NULL DEFAULT 0, "
                "payload_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "expires_at TIMESTAMP WITH TIME ZONE, "
                "CONSTRAINT uq_google_ads_data_snapshot UNIQUE "
                "(target_key, dataset_key, scope_key, schema_version, query_hash)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_target_key ON google_ads_data_snapshots (target_key)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_account_id ON google_ads_data_snapshots (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_connection_id ON google_ads_data_snapshots (connection_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_source_job_id ON google_ads_data_snapshots (source_job_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_dataset_key ON google_ads_data_snapshots (dataset_key)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_scope_key ON google_ads_data_snapshots (scope_key)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_query_hash ON google_ads_data_snapshots (query_hash)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_schema_version ON google_ads_data_snapshots (schema_version)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_fetched_at ON google_ads_data_snapshots (fetched_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_data_snapshots_expires_at ON google_ads_data_snapshots (expires_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_keyword_candidates ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "keyword VARCHAR(160) NOT NULL, "
                "normalized_keyword VARCHAR(160) NOT NULL, "
                "quality_label VARCHAR(40) NOT NULL DEFAULT 'clicked', "
                "review_status VARCHAR(40) NOT NULL DEFAULT 'new', "
                "match_type VARCHAR(40) NOT NULL DEFAULT 'exact', "
                "source_dataset_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_scope_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_snapshot_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "ad_group_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "impressions BIGINT NOT NULL DEFAULT 0, "
                "clicks BIGINT NOT NULL DEFAULT 0, "
                "cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversion_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "score DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_source_job_id INTEGER REFERENCES background_jobs(id), "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_keyword_candidate UNIQUE (account_id, normalized_keyword)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_account_id ON google_ads_keyword_candidates (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_keyword ON google_ads_keyword_candidates (keyword)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_normalized_keyword ON google_ads_keyword_candidates (normalized_keyword)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_quality_label ON google_ads_keyword_candidates (quality_label)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_review_status ON google_ads_keyword_candidates (review_status)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_match_type ON google_ads_keyword_candidates (match_type)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_score ON google_ads_keyword_candidates (score)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_first_seen_at ON google_ads_keyword_candidates (first_seen_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_last_seen_at ON google_ads_keyword_candidates (last_seen_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_last_pulled_at ON google_ads_keyword_candidates (last_pulled_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_last_source_job_id ON google_ads_keyword_candidates (last_source_job_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_created_at ON google_ads_keyword_candidates (created_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_keyword_candidates_updated_at ON google_ads_keyword_candidates (updated_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_landing_page_candidates ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "url TEXT NOT NULL, "
                "normalized_url TEXT NOT NULL, "
                "normalized_url_hash VARCHAR(64) NOT NULL, "
                "quality_label VARCHAR(40) NOT NULL DEFAULT 'clicked', "
                "review_status VARCHAR(40) NOT NULL DEFAULT 'new', "
                "source_dataset_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_scope_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_snapshot_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "channel_types JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "impressions BIGINT NOT NULL DEFAULT 0, "
                "clicks BIGINT NOT NULL DEFAULT 0, "
                "cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversion_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "score DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_source_job_id INTEGER REFERENCES background_jobs(id), "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_landing_page_candidate UNIQUE (account_id, normalized_url_hash)"
                ")"
            )
        )
        await conn.execute(
            text("ALTER TABLE google_ads_landing_page_candidates ADD COLUMN IF NOT EXISTS channel_types JSONB NOT NULL DEFAULT '[]'::jsonb")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_account_id ON google_ads_landing_page_candidates (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_normalized_url_hash ON google_ads_landing_page_candidates (normalized_url_hash)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_quality_label ON google_ads_landing_page_candidates (quality_label)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_review_status ON google_ads_landing_page_candidates (review_status)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_score ON google_ads_landing_page_candidates (score)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_first_seen_at ON google_ads_landing_page_candidates (first_seen_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_last_seen_at ON google_ads_landing_page_candidates (last_seen_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_last_pulled_at ON google_ads_landing_page_candidates (last_pulled_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_last_source_job_id ON google_ads_landing_page_candidates (last_source_job_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_created_at ON google_ads_landing_page_candidates (created_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_landing_page_candidates_updated_at ON google_ads_landing_page_candidates (updated_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_negative_keyword_candidates ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "scope_level VARCHAR(40) NOT NULL DEFAULT 'campaign', "
                "campaign_id BIGINT NOT NULL DEFAULT 0, "
                "campaign_name VARCHAR(255) NOT NULL DEFAULT '', "
                "ad_group_id BIGINT NOT NULL DEFAULT 0, "
                "ad_group_name VARCHAR(255) NOT NULL DEFAULT '', "
                "keyword VARCHAR(160) NOT NULL, "
                "normalized_keyword VARCHAR(160) NOT NULL, "
                "match_type VARCHAR(40) NOT NULL DEFAULT 'exact', "
                "reason_label VARCHAR(80) NOT NULL DEFAULT 'review', "
                "review_status VARCHAR(40) NOT NULL DEFAULT 'new', "
                "guard_status VARCHAR(40) NOT NULL DEFAULT 'candidate', "
                "confidence DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "guard_reasons JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_dataset_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_scope_keys JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "source_snapshot_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "campaign_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "ad_group_ids JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "ad_group_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "impressions BIGINT NOT NULL DEFAULT 0, "
                "clicks BIGINT NOT NULL DEFAULT 0, "
                "cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "conversion_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "all_conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "score DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_source_job_id INTEGER REFERENCES background_jobs(id), "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_negative_keyword_candidate UNIQUE (account_id, scope_level, campaign_id, ad_group_id, normalized_keyword, match_type)"
                ")"
            )
        )
        for column_sql in [
            "scope_level VARCHAR(40) NOT NULL DEFAULT 'campaign'",
            "campaign_id BIGINT NOT NULL DEFAULT 0",
            "campaign_name VARCHAR(255) NOT NULL DEFAULT ''",
            "ad_group_id BIGINT NOT NULL DEFAULT 0",
            "ad_group_name VARCHAR(255) NOT NULL DEFAULT ''",
            "keyword VARCHAR(160) NOT NULL DEFAULT ''",
            "normalized_keyword VARCHAR(160) NOT NULL DEFAULT ''",
            "match_type VARCHAR(40) NOT NULL DEFAULT 'exact'",
            "reason_label VARCHAR(80) NOT NULL DEFAULT 'review'",
            "review_status VARCHAR(40) NOT NULL DEFAULT 'new'",
            "guard_status VARCHAR(40) NOT NULL DEFAULT 'candidate'",
            "confidence DOUBLE PRECISION NOT NULL DEFAULT 0",
            "guard_reasons JSONB NOT NULL DEFAULT '[]'::jsonb",
            "source_dataset_keys JSONB NOT NULL DEFAULT '[]'::jsonb",
            "source_scope_keys JSONB NOT NULL DEFAULT '[]'::jsonb",
            "source_snapshot_ids JSONB NOT NULL DEFAULT '[]'::jsonb",
            "campaign_ids JSONB NOT NULL DEFAULT '[]'::jsonb",
            "campaign_names JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ad_group_ids JSONB NOT NULL DEFAULT '[]'::jsonb",
            "ad_group_names JSONB NOT NULL DEFAULT '[]'::jsonb",
            "impressions BIGINT NOT NULL DEFAULT 0",
            "clicks BIGINT NOT NULL DEFAULT 0",
            "cost DOUBLE PRECISION NOT NULL DEFAULT 0",
            "conversions DOUBLE PRECISION NOT NULL DEFAULT 0",
            "conversion_value DOUBLE PRECISION NOT NULL DEFAULT 0",
            "all_conversions DOUBLE PRECISION NOT NULL DEFAULT 0",
            "all_conversions_value DOUBLE PRECISION NOT NULL DEFAULT 0",
            "score DOUBLE PRECISION NOT NULL DEFAULT 0",
            "source_json JSONB NOT NULL DEFAULT '{}'::jsonb",
            "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
            "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
            "last_pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
            "last_source_job_id INTEGER REFERENCES background_jobs(id)",
            "created_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
            "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now()",
        ]:
            column_name = column_sql.split(" ", 1)[0]
            await conn.execute(text(f"ALTER TABLE google_ads_negative_keyword_candidates ADD COLUMN IF NOT EXISTS {column_sql}"))
            if column_name in {"keyword", "normalized_keyword"}:
                await conn.execute(text(f"UPDATE google_ads_negative_keyword_candidates SET {column_name} = '' WHERE {column_name} IS NULL"))
        for index_name, index_columns in [
            ("ix_google_ads_negative_keyword_candidates_account_id", "account_id"),
            ("ix_google_ads_negative_keyword_candidates_scope_level", "scope_level"),
            ("ix_google_ads_negative_keyword_candidates_campaign_id", "campaign_id"),
            ("ix_google_ads_negative_keyword_candidates_campaign_name", "campaign_name"),
            ("ix_google_ads_negative_keyword_candidates_ad_group_id", "ad_group_id"),
            ("ix_google_ads_negative_keyword_candidates_ad_group_name", "ad_group_name"),
            ("ix_google_ads_negative_keyword_candidates_keyword", "keyword"),
            ("ix_google_ads_negative_keyword_candidates_normalized_keyword", "normalized_keyword"),
            ("ix_google_ads_negative_keyword_candidates_match_type", "match_type"),
            ("ix_google_ads_negative_keyword_candidates_reason_label", "reason_label"),
            ("ix_google_ads_negative_keyword_candidates_review_status", "review_status"),
            ("ix_google_ads_negative_keyword_candidates_guard_status", "guard_status"),
            ("ix_google_ads_negative_keyword_candidates_confidence", "confidence"),
            ("ix_google_ads_negative_keyword_candidates_score", "score"),
            ("ix_google_ads_negative_keyword_candidates_first_seen_at", "first_seen_at"),
            ("ix_google_ads_negative_keyword_candidates_last_seen_at", "last_seen_at"),
            ("ix_google_ads_negative_keyword_candidates_last_pulled_at", "last_pulled_at"),
            ("ix_google_ads_negative_keyword_candidates_last_source_job_id", "last_source_job_id"),
            ("ix_google_ads_negative_keyword_candidates_created_at", "created_at"),
            ("ix_google_ads_negative_keyword_candidates_updated_at", "updated_at"),
        ]:
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON google_ads_negative_keyword_candidates ({index_columns})"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_policy_disapproval_terms ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "term VARCHAR(160) NOT NULL, "
                "normalized_term VARCHAR(160) NOT NULL, "
                "policy_topic VARCHAR(160) NOT NULL DEFAULT 'Unapproved substances', "
                "approval_status VARCHAR(40) NOT NULL DEFAULT 'DISAPPROVED', "
                "guard_status VARCHAR(40) NOT NULL DEFAULT 'active', "
                "occurrence_count INTEGER NOT NULL DEFAULT 0, "
                "evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "google_shared_set_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "google_shared_criterion_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "negative_candidate_id INTEGER REFERENCES google_ads_negative_keyword_candidates(id), "
                "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_pulled_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_source_job_id INTEGER REFERENCES background_jobs(id), "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_policy_disapproval_term UNIQUE (account_id, normalized_term, policy_topic)"
                ")"
            )
        )
        for index_name, index_columns in [
            ("ix_google_ads_policy_disapproval_terms_account_id", "account_id"),
            ("ix_google_ads_policy_disapproval_terms_term", "term"),
            ("ix_google_ads_policy_disapproval_terms_normalized_term", "normalized_term"),
            ("ix_google_ads_policy_disapproval_terms_policy_topic", "policy_topic"),
            ("ix_google_ads_policy_disapproval_terms_approval_status", "approval_status"),
            ("ix_google_ads_policy_disapproval_terms_guard_status", "guard_status"),
            ("ix_google_ads_policy_disapproval_terms_google_shared_set_resource_name", "google_shared_set_resource_name"),
            ("ix_google_ads_policy_disapproval_terms_google_shared_criterion_resource_name", "google_shared_criterion_resource_name"),
            ("ix_google_ads_policy_disapproval_terms_negative_candidate_id", "negative_candidate_id"),
            ("ix_google_ads_policy_disapproval_terms_first_seen_at", "first_seen_at"),
            ("ix_google_ads_policy_disapproval_terms_last_seen_at", "last_seen_at"),
            ("ix_google_ads_policy_disapproval_terms_last_pulled_at", "last_pulled_at"),
            ("ix_google_ads_policy_disapproval_terms_last_source_job_id", "last_source_job_id"),
            ("ix_google_ads_policy_disapproval_terms_created_at", "created_at"),
            ("ix_google_ads_policy_disapproval_terms_updated_at", "updated_at"),
        ]:
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON google_ads_policy_disapproval_terms ({index_columns})"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_automation_preferences ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "automation_enabled BOOLEAN NOT NULL DEFAULT false, "
                "monitor_only BOOLEAN NOT NULL DEFAULT true, "
                "keyword_discovery_enabled BOOLEAN NOT NULL DEFAULT true, "
                "negative_keyword_enabled BOOLEAN NOT NULL DEFAULT false, "
                "audience_signal_enabled BOOLEAN NOT NULL DEFAULT true, "
                "landing_page_enabled BOOLEAN NOT NULL DEFAULT true, "
                "auction_monitor_enabled BOOLEAN NOT NULL DEFAULT true, "
                "odoo_sales_guard_enabled BOOLEAN NOT NULL DEFAULT true, "
                "auto_apply_keywords_enabled BOOLEAN NOT NULL DEFAULT false, "
                "auto_apply_negatives_enabled BOOLEAN NOT NULL DEFAULT false, "
                "auto_create_campaigns_enabled BOOLEAN NOT NULL DEFAULT false, "
                "auto_pause_campaigns_enabled BOOLEAN NOT NULL DEFAULT false, "
                "auto_peak_budget_enabled BOOLEAN NOT NULL DEFAULT false, "
                "testing_bootstrap_enabled BOOLEAN NOT NULL DEFAULT true, "
                "testing_bootstrap_days INTEGER NOT NULL DEFAULT 14, "
                "pmax_min_7d_conversions DOUBLE PRECISION NOT NULL DEFAULT 5, "
                "testing_sales_budget_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.05, "
                "testing_keyword_limit INTEGER NOT NULL DEFAULT 30, "
                "testing_landing_page_limit INTEGER NOT NULL DEFAULT 25, "
                "peak_budget_increase_pct DOUBLE PRECISION NOT NULL DEFAULT 0.5, "
                "peak_budget_warmup_minutes INTEGER NOT NULL DEFAULT 60, "
                "peak_budget_restore_delay_minutes INTEGER NOT NULL DEFAULT 0, "
                "peak_budget_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "last_peak_budget_decision_at TIMESTAMP WITH TIME ZONE, "
                "daily_keyword_lookback_days INTEGER NOT NULL DEFAULT 60, "
                "all_time_refresh_interval_days INTEGER NOT NULL DEFAULT 7, "
                "api_call_budget_per_day INTEGER NOT NULL DEFAULT 750, "
                "max_daily_api_rows INTEGER NOT NULL DEFAULT 10000, "
                "mutation_cooldown_days INTEGER NOT NULL DEFAULT 3, "
                "schedule_mode VARCHAR(40) NOT NULL DEFAULT 'dynamic_low_traffic', "
                "scheduled_hour INTEGER NOT NULL DEFAULT 4, "
                "scheduled_minute INTEGER NOT NULL DEFAULT 20, "
                "schedule_timezone VARCHAR(80) NOT NULL DEFAULT 'UTC', "
                "schedule_source VARCHAR(160) NOT NULL DEFAULT '', "
                "schedule_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "last_schedule_decision_at TIMESTAMP WITH TIME ZONE, "
                "odoo_sales_max_spend_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.15, "
                "odoo_sales_guard_window_days INTEGER NOT NULL DEFAULT 7, "
                "budget_guard_check_interval_hours INTEGER NOT NULL DEFAULT 6, "
                "minimum_daily_budget_amount DOUBLE PRECISION NOT NULL DEFAULT 1, "
                "underperforming_budget_reduce_pct DOUBLE PRECISION NOT NULL DEFAULT 0.20, "
                "peak_budget_extra_spend_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.05, "
                "peak_budget_check_interval_minutes INTEGER NOT NULL DEFAULT 60, "
                "target_cost_per_conversion DOUBLE PRECISION, "
                "target_roas DOUBLE PRECISION, "
                "last_keyword_pull_at TIMESTAMP WITH TIME ZONE, "
                "last_all_time_pull_at TIMESTAMP WITH TIME ZONE, "
                "last_budget_guard_run_at TIMESTAMP WITH TIME ZONE, "
                "last_peak_budget_check_at TIMESTAMP WITH TIME ZONE, "
                "last_analysis_at TIMESTAMP WITH TIME ZONE, "
                "last_run_at TIMESTAMP WITH TIME ZONE, "
                "last_error TEXT, "
                "strategy_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_automation_account UNIQUE (account_id)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_account_id ON google_ads_automation_preferences (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_automation_enabled ON google_ads_automation_preferences (automation_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_monitor_only ON google_ads_automation_preferences (monitor_only)")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS schedule_mode VARCHAR(40) NOT NULL DEFAULT 'dynamic_low_traffic'")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS scheduled_hour INTEGER NOT NULL DEFAULT 4")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS scheduled_minute INTEGER NOT NULL DEFAULT 20")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS schedule_timezone VARCHAR(80) NOT NULL DEFAULT 'UTC'")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS schedule_source VARCHAR(160) NOT NULL DEFAULT ''")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS schedule_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS last_schedule_decision_at TIMESTAMP WITH TIME ZONE")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS odoo_sales_max_spend_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.15")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS odoo_sales_guard_window_days INTEGER NOT NULL DEFAULT 7")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS budget_guard_check_interval_hours INTEGER NOT NULL DEFAULT 6")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS minimum_daily_budget_amount DOUBLE PRECISION NOT NULL DEFAULT 1")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS underperforming_budget_reduce_pct DOUBLE PRECISION NOT NULL DEFAULT 0.20")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_extra_spend_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.05")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_check_interval_minutes INTEGER NOT NULL DEFAULT 60")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_schedule_mode ON google_ads_automation_preferences (schedule_mode)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_schedule_source ON google_ads_automation_preferences (schedule_source)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_schedule_decision_at ON google_ads_automation_preferences (last_schedule_decision_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_keyword_discovery_enabled ON google_ads_automation_preferences (keyword_discovery_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_negative_keyword_enabled ON google_ads_automation_preferences (negative_keyword_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_audience_signal_enabled ON google_ads_automation_preferences (audience_signal_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_landing_page_enabled ON google_ads_automation_preferences (landing_page_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auction_monitor_enabled ON google_ads_automation_preferences (auction_monitor_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_odoo_sales_guard_enabled ON google_ads_automation_preferences (odoo_sales_guard_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auto_apply_keywords_enabled ON google_ads_automation_preferences (auto_apply_keywords_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auto_apply_negatives_enabled ON google_ads_automation_preferences (auto_apply_negatives_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auto_create_campaigns_enabled ON google_ads_automation_preferences (auto_create_campaigns_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auto_pause_campaigns_enabled ON google_ads_automation_preferences (auto_pause_campaigns_enabled)")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS auto_peak_budget_enabled BOOLEAN NOT NULL DEFAULT false")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS testing_bootstrap_enabled BOOLEAN NOT NULL DEFAULT true")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS testing_bootstrap_days INTEGER NOT NULL DEFAULT 14")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS pmax_min_7d_conversions DOUBLE PRECISION NOT NULL DEFAULT 5")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS testing_sales_budget_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.05")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS testing_keyword_limit INTEGER NOT NULL DEFAULT 30")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS testing_landing_page_limit INTEGER NOT NULL DEFAULT 25")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_testing_bootstrap_enabled ON google_ads_automation_preferences (testing_bootstrap_enabled)")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_increase_pct DOUBLE PRECISION NOT NULL DEFAULT 0.5")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_warmup_minutes INTEGER NOT NULL DEFAULT 60")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_restore_delay_minutes INTEGER NOT NULL DEFAULT 0")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS peak_budget_decision_json JSONB NOT NULL DEFAULT '{}'::jsonb")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS last_peak_budget_decision_at TIMESTAMP WITH TIME ZONE")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_auto_peak_budget_enabled ON google_ads_automation_preferences (auto_peak_budget_enabled)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_peak_budget_decision_at ON google_ads_automation_preferences (last_peak_budget_decision_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_keyword_pull_at ON google_ads_automation_preferences (last_keyword_pull_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_all_time_pull_at ON google_ads_automation_preferences (last_all_time_pull_at)")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS last_budget_guard_run_at TIMESTAMP WITH TIME ZONE")
        )
        await conn.execute(
            text("ALTER TABLE google_ads_automation_preferences ADD COLUMN IF NOT EXISTS last_peak_budget_check_at TIMESTAMP WITH TIME ZONE")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_budget_guard_run_at ON google_ads_automation_preferences (last_budget_guard_run_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_peak_budget_check_at ON google_ads_automation_preferences (last_peak_budget_check_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_analysis_at ON google_ads_automation_preferences (last_analysis_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_automation_preferences_last_run_at ON google_ads_automation_preferences (last_run_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS odoo_websites ("
                "id SERIAL PRIMARY KEY, "
                "store_id INTEGER NOT NULL REFERENCES odoo_stores(id), "
                "website_id INTEGER NOT NULL, "
                "name VARCHAR(255) NOT NULL, "
                "domain VARCHAR(500) NOT NULL DEFAULT '', "
                "is_active BOOLEAN NOT NULL DEFAULT true, "
                "fetched_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_odoo_website_store_website UNIQUE (store_id, website_id)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_store_id ON odoo_websites (store_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_website_id ON odoo_websites (website_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_name ON odoo_websites (name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_domain ON odoo_websites (domain)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_is_active ON odoo_websites (is_active)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_websites_fetched_at ON odoo_websites (fetched_at)")
        )
        await conn.execute(
            text("ALTER TABLE odoo_store_google_ads_mappings ADD COLUMN IF NOT EXISTS website_id INTEGER NOT NULL DEFAULT 0")
        )
        await conn.execute(
            text("UPDATE odoo_store_google_ads_mappings SET website_id = 0 WHERE website_id IS NULL")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_store_google_ads_mappings_website_id ON odoo_store_google_ads_mappings (website_id)")
        )
        await conn.execute(
            text(
                "DELETE FROM odoo_store_google_ads_mappings loser "
                "USING odoo_store_google_ads_mappings keeper "
                "WHERE loser.id < keeper.id "
                "AND loser.store_id = keeper.store_id "
                "AND loser.website_id = keeper.website_id "
                "AND loser.account_id = keeper.account_id"
            )
        )
        await conn.execute(
            text(
                "DO $$ DECLARE constraint_matches BOOLEAN; BEGIN "
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_constraint c "
                "WHERE c.conname = 'uq_odoo_store_google_ads_mapping' "
                "AND c.conrelid = 'odoo_store_google_ads_mappings'::regclass "
                "AND c.contype = 'u' "
                "AND ("
                "SELECT array_agg(a.attname::text ORDER BY cols.ordinality) "
                "FROM unnest(c.conkey) WITH ORDINALITY AS cols(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = cols.attnum"
                ") = ARRAY['store_id', 'website_id', 'account_id']::text[]"
                ") INTO constraint_matches; "
                "IF EXISTS ("
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_odoo_store_google_ads_mapping' "
                "AND conrelid = 'odoo_store_google_ads_mappings'::regclass"
                ") AND NOT constraint_matches THEN "
                "ALTER TABLE odoo_store_google_ads_mappings DROP CONSTRAINT uq_odoo_store_google_ads_mapping; "
                "END IF; "
                "IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_odoo_store_google_ads_mapping' "
                "AND conrelid = 'odoo_store_google_ads_mappings'::regclass"
                ") THEN "
                "ALTER TABLE odoo_store_google_ads_mappings "
                "ADD CONSTRAINT uq_odoo_store_google_ads_mapping "
                "UNIQUE (store_id, website_id, account_id); "
                "END IF; END $$;"
            )
        )
        await conn.execute(
            text("ALTER TABLE odoo_sale_orders ADD COLUMN IF NOT EXISTS website_name VARCHAR(255) NOT NULL DEFAULT ''")
        )
        await conn.execute(
            text("ALTER TABLE odoo_sale_orders ADD COLUMN IF NOT EXISTS margin_amount DOUBLE PRECISION")
        )
        await conn.execute(
            text("ALTER TABLE odoo_sale_orders ADD COLUMN IF NOT EXISTS margin_percent DOUBLE PRECISION")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_sale_orders_website_name ON odoo_sale_orders (website_name)")
        )
        await conn.execute(
            text("ALTER TABLE spend_guard_snapshots ADD COLUMN IF NOT EXISTS margin_inr DOUBLE PRECISION NOT NULL DEFAULT 0")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS odoo_product_page_signals ("
                "id SERIAL PRIMARY KEY, "
                "store_id INTEGER NOT NULL REFERENCES odoo_stores(id), "
                "website_id INTEGER NOT NULL DEFAULT 0, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "website_name VARCHAR(255) NOT NULL DEFAULT '', "
                "domain VARCHAR(500) NOT NULL DEFAULT '', "
                "product_code VARCHAR(160) NOT NULL DEFAULT '', "
                "product_name VARCHAR(500) NOT NULL DEFAULT '', "
                "product_url VARCHAR(1000) NOT NULL DEFAULT '', "
                "product_url_hash VARCHAR(64) NOT NULL DEFAULT '', "
                "currency_code VARCHAR(12) NOT NULL DEFAULT '', "
                "order_count INTEGER NOT NULL DEFAULT 0, "
                "quantity DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "sales_amount DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "margin_amount DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "margin_percent DOUBLE PRECISION, "
                "google_cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "google_clicks INTEGER NOT NULL DEFAULT 0, "
                "google_conversions DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "google_conversion_value DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "zero_conversion_cost DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "label VARCHAR(40) NOT NULL DEFAULT 'watch', "
                "reason TEXT NOT NULL DEFAULT '', "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_odoo_product_page_signal UNIQUE "
                "(store_id, website_id, account_id, product_code, product_url_hash)"
                ")"
            )
        )
        await conn.execute(
            text("ALTER TABLE odoo_product_page_signals ADD COLUMN IF NOT EXISTS product_url_hash VARCHAR(64) NOT NULL DEFAULT ''")
        )
        await conn.execute(
            text("UPDATE odoo_product_page_signals SET product_url_hash = md5(product_url) WHERE product_url_hash = ''")
        )
        await conn.execute(
            text(
                "DO $$ DECLARE constraint_matches BOOLEAN; BEGIN "
                "SELECT EXISTS ("
                "SELECT 1 FROM pg_constraint c "
                "WHERE c.conname = 'uq_odoo_product_page_signal' "
                "AND c.conrelid = 'odoo_product_page_signals'::regclass "
                "AND c.contype = 'u' "
                "AND ("
                "SELECT array_agg(a.attname::text ORDER BY cols.ordinality) "
                "FROM unnest(c.conkey) WITH ORDINALITY AS cols(attnum, ordinality) "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = cols.attnum"
                ") = ARRAY['store_id', 'website_id', 'account_id', 'product_code', 'product_url_hash']::text[]"
                ") INTO constraint_matches; "
                "IF EXISTS ("
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_odoo_product_page_signal' "
                "AND conrelid = 'odoo_product_page_signals'::regclass"
                ") AND NOT constraint_matches THEN "
                "ALTER TABLE odoo_product_page_signals DROP CONSTRAINT uq_odoo_product_page_signal; "
                "END IF; "
                "IF NOT EXISTS ("
                "SELECT 1 FROM pg_constraint "
                "WHERE conname = 'uq_odoo_product_page_signal' "
                "AND conrelid = 'odoo_product_page_signals'::regclass"
                ") THEN "
                "ALTER TABLE odoo_product_page_signals "
                "ADD CONSTRAINT uq_odoo_product_page_signal "
                "UNIQUE (store_id, website_id, account_id, product_code, product_url_hash); "
                "END IF; END $$;"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_store_id ON odoo_product_page_signals (store_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_website_id ON odoo_product_page_signals (website_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_account_id ON odoo_product_page_signals (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_website_name ON odoo_product_page_signals (website_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_domain ON odoo_product_page_signals (domain)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_product_code ON odoo_product_page_signals (product_code)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_product_name ON odoo_product_page_signals (product_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_product_url_hash ON odoo_product_page_signals (product_url_hash)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_currency_code ON odoo_product_page_signals (currency_code)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_label ON odoo_product_page_signals (label)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_odoo_product_page_signals_synced_at ON odoo_product_page_signals (synced_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_page_feed_publications ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "store_id INTEGER NOT NULL REFERENCES odoo_stores(id), "
                "website_id INTEGER NOT NULL DEFAULT 0, "
                "website_name VARCHAR(255) NOT NULL DEFAULT '', "
                "feed_kind VARCHAR(40) NOT NULL DEFAULT 'best', "
                "asset_set_name VARCHAR(255) NOT NULL DEFAULT '', "
                "asset_set_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "status VARCHAR(40) NOT NULL DEFAULT 'planned', "
                "last_publish_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "last_error TEXT NOT NULL DEFAULT '', "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_page_feed_publication UNIQUE "
                "(account_id, store_id, website_id, feed_kind)"
                ")"
            )
        )
        await conn.execute(
            text("ALTER TABLE google_ads_page_feed_publications ADD COLUMN IF NOT EXISTS feed_kind VARCHAR(40) NOT NULL DEFAULT 'best'")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_feed_kind ON google_ads_page_feed_publications (feed_kind)")
        )
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_google_ads_page_feed_publication') THEN "
                "ALTER TABLE google_ads_page_feed_publications DROP CONSTRAINT uq_google_ads_page_feed_publication; "
                "END IF; END $$;"
            )
        )
        await conn.execute(
            text(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_google_ads_page_feed_publication') THEN "
                "ALTER TABLE google_ads_page_feed_publications "
                "ADD CONSTRAINT uq_google_ads_page_feed_publication "
                "UNIQUE (account_id, store_id, website_id, feed_kind); "
                "END IF; END $$;"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_account_id ON google_ads_page_feed_publications (account_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_store_id ON google_ads_page_feed_publications (store_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_website_id ON google_ads_page_feed_publications (website_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_website_name ON google_ads_page_feed_publications (website_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_asset_set_name ON google_ads_page_feed_publications (asset_set_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_asset_set_resource_name ON google_ads_page_feed_publications (asset_set_resource_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_status ON google_ads_page_feed_publications (status)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_created_at ON google_ads_page_feed_publications (created_at)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_publications_updated_at ON google_ads_page_feed_publications (updated_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_page_feed_assets ("
                "id SERIAL PRIMARY KEY, "
                "publication_id INTEGER NOT NULL REFERENCES google_ads_page_feed_publications(id), "
                "signal_id INTEGER REFERENCES odoo_product_page_signals(id), "
                "page_url VARCHAR(1000) NOT NULL DEFAULT '', "
                "page_url_hash VARCHAR(64) NOT NULL DEFAULT '', "
                "labels JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "asset_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "asset_set_asset_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "status VARCHAR(40) NOT NULL DEFAULT 'planned', "
                "last_error TEXT NOT NULL DEFAULT '', "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_page_feed_asset UNIQUE (publication_id, page_url_hash)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_publication_id ON google_ads_page_feed_assets (publication_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_signal_id ON google_ads_page_feed_assets (signal_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_page_url_hash ON google_ads_page_feed_assets (page_url_hash)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_asset_resource_name ON google_ads_page_feed_assets (asset_resource_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_asset_set_asset_resource_name ON google_ads_page_feed_assets (asset_set_asset_resource_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_status ON google_ads_page_feed_assets (status)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_assets_synced_at ON google_ads_page_feed_assets (synced_at)")
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_page_feed_campaign_links ("
                "id SERIAL PRIMARY KEY, "
                "publication_id INTEGER NOT NULL REFERENCES google_ads_page_feed_publications(id), "
                "campaign_id BIGINT NOT NULL, "
                "campaign_name VARCHAR(255) NOT NULL DEFAULT '', "
                "campaign_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "channel_type VARCHAR(80) NOT NULL DEFAULT '', "
                "campaign_asset_set_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "dsa_criterion_resource_names JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "status VARCHAR(40) NOT NULL DEFAULT 'planned', "
                "last_error TEXT NOT NULL DEFAULT '', "
                "synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_page_feed_campaign_link UNIQUE (publication_id, campaign_id)"
                ")"
            )
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_publication_id ON google_ads_page_feed_campaign_links (publication_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_campaign_id ON google_ads_page_feed_campaign_links (campaign_id)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_campaign_name ON google_ads_page_feed_campaign_links (campaign_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_campaign_resource_name ON google_ads_page_feed_campaign_links (campaign_resource_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_channel_type ON google_ads_page_feed_campaign_links (channel_type)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_campaign_asset_set_resource_name ON google_ads_page_feed_campaign_links (campaign_asset_set_resource_name)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_status ON google_ads_page_feed_campaign_links (status)")
        )
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_google_ads_page_feed_campaign_links_synced_at ON google_ads_page_feed_campaign_links (synced_at)")
        )
        customer_match_publication_columns = [
            "data_manager_api_enabled BOOLEAN NOT NULL DEFAULT true",
            "data_manager_user_list_id VARCHAR(80) NOT NULL DEFAULT ''",
            "data_manager_user_list_resource VARCHAR(255) NOT NULL DEFAULT ''",
            "data_manager_user_list_name VARCHAR(255) NOT NULL DEFAULT ''",
            "data_manager_status VARCHAR(40) NOT NULL DEFAULT 'planned'",
            "data_manager_last_request_id VARCHAR(120) NOT NULL DEFAULT ''",
            "data_manager_last_error TEXT NOT NULL DEFAULT ''",
            "data_manager_last_response_json JSONB NOT NULL DEFAULT '{}'::jsonb",
            "data_manager_last_pushed_at TIMESTAMP WITH TIME ZONE",
        ]
        for column_sql in customer_match_publication_columns:
            await conn.execute(text(f"ALTER TABLE google_ads_customer_match_publications ADD COLUMN IF NOT EXISTS {column_sql}"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_customer_match_outbox ("
                "id SERIAL PRIMARY KEY, "
                "publication_id INTEGER NOT NULL REFERENCES google_ads_customer_match_publications(id), "
                "member_id INTEGER NOT NULL REFERENCES odoo_customer_match_members(id), "
                "action VARCHAR(20) NOT NULL DEFAULT 'add', "
                "status VARCHAR(40) NOT NULL DEFAULT 'pending', "
                "attempt_count INTEGER NOT NULL DEFAULT 0, "
                "next_attempt_at TIMESTAMP WITH TIME ZONE, "
                "last_attempt_at TIMESTAMP WITH TIME ZONE, "
                "sent_at TIMESTAMP WITH TIME ZONE, "
                "last_request_id VARCHAR(120) NOT NULL DEFAULT '', "
                "last_error TEXT NOT NULL DEFAULT '', "
                "response_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_customer_match_outbox UNIQUE (publication_id, member_id, action)"
                ")"
            )
        )
        customer_match_indexes = [
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_publications_data_manager_api_enabled ON google_ads_customer_match_publications (data_manager_api_enabled)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_publications_data_manager_user_list_id ON google_ads_customer_match_publications (data_manager_user_list_id)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_publications_data_manager_status ON google_ads_customer_match_publications (data_manager_status)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_publications_data_manager_last_pushed_at ON google_ads_customer_match_publications (data_manager_last_pushed_at)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_publication_id ON google_ads_customer_match_outbox (publication_id)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_member_id ON google_ads_customer_match_outbox (member_id)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_action ON google_ads_customer_match_outbox (action)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_status ON google_ads_customer_match_outbox (status)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_next_attempt_at ON google_ads_customer_match_outbox (next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_sent_at ON google_ads_customer_match_outbox (sent_at)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_last_request_id ON google_ads_customer_match_outbox (last_request_id)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_created_at ON google_ads_customer_match_outbox (created_at)",
            "CREATE INDEX IF NOT EXISTS ix_google_ads_customer_match_outbox_updated_at ON google_ads_customer_match_outbox (updated_at)",
        ]
        for index_sql in customer_match_indexes:
            await conn.execute(text(index_sql))
        asset_preference_columns = [
            "auto_callouts_enabled BOOLEAN NOT NULL DEFAULT true",
            "auto_price_assets_enabled BOOLEAN NOT NULL DEFAULT true",
            "auto_business_messages_enabled BOOLEAN NOT NULL DEFAULT false",
            "auto_campaign_asset_mapping_enabled BOOLEAN NOT NULL DEFAULT true",
            "auto_ad_group_asset_mapping_enabled BOOLEAN NOT NULL DEFAULT true",
            "whatsapp_country_code VARCHAR(8) NOT NULL DEFAULT '+1'",
            "whatsapp_phone_number VARCHAR(40) NOT NULL DEFAULT '+14434007587'",
            "whatsapp_starter_message VARCHAR(140) NOT NULL DEFAULT 'Can I get help choosing a product?'",
            "whatsapp_call_to_action VARCHAR(40) NOT NULL DEFAULT 'MESSAGE'",
            "whatsapp_call_to_action_description VARCHAR(30) NOT NULL DEFAULT 'Chat with us'",
        ]
        for column_sql in asset_preference_columns:
            await conn.execute(text(f"ALTER TABLE google_ads_asset_automation_preferences ADD COLUMN IF NOT EXISTS {column_sql}"))
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS google_ads_brand_list_candidates ("
                "id SERIAL PRIMARY KEY, "
                "account_id INTEGER NOT NULL REFERENCES google_ads_accounts(id), "
                "store_id INTEGER REFERENCES odoo_stores(id), "
                "website_id INTEGER NOT NULL DEFAULT 0, "
                "website_name VARCHAR(255) NOT NULL DEFAULT '', "
                "website_domain VARCHAR(500) NOT NULL DEFAULT '', "
                "country_code VARCHAR(12) NOT NULL DEFAULT '', "
                "brand_name VARCHAR(255) NOT NULL, "
                "normalized_brand VARCHAR(160) NOT NULL, "
                "order_count INTEGER NOT NULL DEFAULT 0, "
                "sales_amount DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "suggested_brand_id VARCHAR(120) NOT NULL DEFAULT '', "
                "suggested_brand_name VARCHAR(255) NOT NULL DEFAULT '', "
                "suggested_primary_url VARCHAR(1000) NOT NULL DEFAULT '', "
                "suggested_urls JSONB NOT NULL DEFAULT '[]'::jsonb, "
                "google_brand_state VARCHAR(80) NOT NULL DEFAULT '', "
                "match_confidence DOUBLE PRECISION NOT NULL DEFAULT 0, "
                "match_status VARCHAR(40) NOT NULL DEFAULT 'needs_review', "
                "source_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "candidate_json JSONB NOT NULL DEFAULT '{}'::jsonb, "
                "google_shared_set_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "google_shared_criterion_resource_name VARCHAR(255) NOT NULL DEFAULT '', "
                "first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "last_synced_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), "
                "CONSTRAINT uq_google_ads_brand_list_candidate UNIQUE (account_id, website_id, normalized_brand)"
                ")"
            )
        )
        for index_name, index_columns in [
            ("ix_google_ads_brand_list_candidates_account_id", "account_id"),
            ("ix_google_ads_brand_list_candidates_store_id", "store_id"),
            ("ix_google_ads_brand_list_candidates_website_id", "website_id"),
            ("ix_google_ads_brand_list_candidates_website_name", "website_name"),
            ("ix_google_ads_brand_list_candidates_website_domain", "website_domain"),
            ("ix_google_ads_brand_list_candidates_country_code", "country_code"),
            ("ix_google_ads_brand_list_candidates_brand_name", "brand_name"),
            ("ix_google_ads_brand_list_candidates_normalized_brand", "normalized_brand"),
            ("ix_google_ads_brand_list_candidates_suggested_brand_id", "suggested_brand_id"),
            ("ix_google_ads_brand_list_candidates_suggested_brand_name", "suggested_brand_name"),
            ("ix_google_ads_brand_list_candidates_suggested_primary_url", "suggested_primary_url"),
            ("ix_google_ads_brand_list_candidates_google_brand_state", "google_brand_state"),
            ("ix_google_ads_brand_list_candidates_match_confidence", "match_confidence"),
            ("ix_google_ads_brand_list_candidates_match_status", "match_status"),
            ("ix_google_ads_brand_list_candidates_google_shared_set_resource_name", "google_shared_set_resource_name"),
            ("ix_google_ads_brand_list_candidates_google_shared_criterion_resource_name", "google_shared_criterion_resource_name"),
            ("ix_google_ads_brand_list_candidates_first_seen_at", "first_seen_at"),
            ("ix_google_ads_brand_list_candidates_last_seen_at", "last_seen_at"),
            ("ix_google_ads_brand_list_candidates_last_synced_at", "last_synced_at"),
            ("ix_google_ads_brand_list_candidates_created_at", "created_at"),
            ("ix_google_ads_brand_list_candidates_updated_at", "updated_at"),
        ]:
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON google_ads_brand_list_candidates ({index_columns})"))
    async with AsyncSessionLocal() as session:
        await seed_database(session, get_settings())
    await engine.dispose()


def main() -> None:
    asyncio.run(init_app_db())
    print("Application database schema and seed data are ready.")


if __name__ == "__main__":
    main()
