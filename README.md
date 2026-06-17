# Google Ads Optimizer

This workspace contains a guarded Google Ads optimization agent. It is scheduled to run every 8 hours, inspect the latest run history first, then check active campaigns, produce a report, and apply only bounded changes when mutations are explicitly enabled. By default, decisions are based on the last 7 days, with the last 30 days used as baseline context.

## FastAPI Command Center

The workspace now also includes a production-shaped FastAPI app for managing all configured Google Ads accounts from one UI. It uses:

- FastAPI for the web app and JSON endpoints.
- PostgreSQL for users, accounts, strategies, settings, optimizer state, reports, and run history.
- `dramatiq-pg` so Dramatiq jobs are queued in PostgreSQL instead of needing a separate Redis/RabbitMQ service.
- Tabler UI components via CDN for a fast, responsive operator dashboard.
- A Cost Dashboard that stores daily Google Ads campaign metrics, currency-safe cost totals, strategy recommendations, and Razorpay daily receipts in PostgreSQL.
- A Conversion Goals page that snapshots customer goals, campaign goals, and campaign goal config levels before any bulk mutation workflow is enabled.
- A Keyword Bank page that stores deduped Google Ads search-term and search-term-insight keywords per account for manual review and one-click copy.

### Local App Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Update `.env` with a strong `SECRET_KEY`, `DATABASE_URL` or `POSTGRES_URL`, and an initial `ADMIN_PASSWORD` for the first boot. Google Ads credentials, OAuth refresh tokens, Odoo/API credentials, optimizer controls, reports, cooldown state, and automation settings are stored in PostgreSQL by `scripts/init_app_db.py`; after that, edit them from the app's Settings page.

Initialize the Dramatiq PostgreSQL schema once:

```bash
python scripts/init_dramatiq_pg.py
python scripts/init_app_db.py
```

Start the web app and worker in two terminals:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8010
dramatiq app.tasks --processes 1 --threads 2
```

For continuous automation monitoring, run the lightweight scheduler as a third process:

```bash
scripts/run_automation_scheduler_loop.sh
```

Open [http://127.0.0.1:8010](http://127.0.0.1:8010), sign in, select accounts and strategies, then queue the run. The request returns immediately; the worker executes the optimizer account by account in the background.

### macOS Dock Launcher

Install the local launcher once:

```bash
./scripts/install_macos_launcher.sh
```

This creates `/Users/amitsoni/Applications/Google Ads Command Center.app` with a custom app icon and pins it to the Dock. Clicking **Google Ads Command Center** opens Terminal briefly, starts or reuses the FastAPI app on [http://127.0.0.1:8010](http://127.0.0.1:8010), starts or reuses the Dramatiq worker, waits for `/healthz`, then opens the portal in the browser.

### Settings Source of Truth

The app stores operational settings in the `app_settings` Postgres table. The Settings page controls the same Google Ads script knobs that were previously environment variables:

- Google Ads API credentials and API version.
- Mutation and dry-run controls.
- Cooldown, date ranges, thresholds, budget guardrails, and ROAS guardrails.
- Storage behavior for reports and optimizer cooldown state.
- Razorpay key ID/key secret and sync window.
- Google Ads reporting sync window for campaign metrics.

Workers read these values from Postgres at run time, generate temporary account/env files only inside a per-run temporary directory, then save generated report JSON and optimizer cooldown state back into Postgres. Temporary files are removed after each account run, so server migration only needs the database plus the application code.

### Coolify / Docker Deployment

The repository includes a production Dockerfile that starts:

- the FastAPI web server;
- one Dramatiq worker for background jobs;
- the automation scheduler loop that queues due monitor jobs.

In Coolify, deploy the public GitHub repository with the included `Dockerfile` and set only runtime values as environment variables. Do not place Google/Odoo/OpenAI/OAuth credentials in the Dockerfile.

Required Coolify variables:

```bash
DATABASE_URL=postgres://...
SECRET_KEY=<long random value>
ADMIN_EMAIL=<initial admin email>
ADMIN_PASSWORD=<initial strong password>
```

`POSTGRES_URL` is also accepted as an alias for `DATABASE_URL`. Service credentials are saved in PostgreSQL from the app UI, so the container image remains safe to publish.

Useful optional variables:

```bash
PORT=8000
WEB_CONCURRENCY=1
INIT_DRAMATIQ_SCHEMA=true
INIT_APP_DB=true
DRAMATIQ_ENABLED=true
DRAMATIQ_PROCESSES=1
DRAMATIQ_THREADS=2
SCHEDULER_ENABLED=true
AUTOMATION_SCHEDULER_INTERVAL_SECONDS=900
AUTOMATION_SCHEDULER_RECOMPUTE_EVERY_RUNS=4
```

If you split Coolify into separate services later, use the same image and set `DRAMATIQ_ENABLED=false` or `SCHEDULER_ENABLED=false` on the web-only service.

### Production Notes

- Set `APP_ENV=production`, a strong `SECRET_KEY`, and a non-default admin password.
- Run the bundled Docker command for simple Coolify deploys, or split web/worker/scheduler into separate services for larger deployments.
- Run `python scripts/init_app_db.py` during deploys instead of enabling app startup migrations on every web worker.
- Keep `Allow mutations` off and `Dry run` on in Settings until reports are reviewed.
- Use `/healthz` for load balancer health checks.
- Use Python 3.10+ in production. The local macOS Python 3.9 environment works, but Google client libraries now warn that 3.9 is past their supported baseline.

### Research-backed operating model

The UI follows the current Google Ads API goal hierarchy:

- Customer conversion goals are account defaults.
- Campaign conversion goals are campaign-level overrides.
- Custom conversion goals are the path for exact conversion-action bundles when category/origin is too broad.
- `primary_for_goal` and goal biddability determine what is used for bidding and the Conversions metric.

References:

- Google Ads API conversion goals: https://developers.google.com/google-ads/api/docs/conversions/goals/overview
- Google Ads API campaign goals and custom goals: https://developers.google.com/google-ads/api/docs/conversions/goals/campaign-goals
- Google Ads Target ROAS help: https://support.google.com/google-ads/answer/6268637
- Razorpay Fetch All Payments API: https://razorpay.com/docs/api/payments/fetch-all-payments

### Dashboard Syncs

The Cost Dashboard has Daily/7/30/90-day views and sync buttons for:

- Google Ads campaign metrics: daily cost, clicks, conversions, conversion value, all conversion value, bidding type, Target ROAS, and budget.
- Razorpay payments: daily captured total, fees/tax, authorized count, failed count, and captured count.
- Currency-safe ad cost groups, so multi-country accounts are not blended into one misleading total.
- Daily account drilldown for the selected window.
- Value-focused recommendations: missing conversion value, value only in All conversions, ROAS movement, daily spend spikes, spend without value, value-bidding readiness, and guarded scale candidates.
- An action board that turns synced data into next steps such as review data coverage, hold Target ROAS changes, fix risks before scaling, and review scale candidates.
- Autopilot delivery rescue: purchase-goal alignment, Add to Cart de-prioritization, low/no-impression budget unlocks, Target ROAS lowering, Search Maximize Clicks rescue with CPC caps, CPC step-ups, and 3-day no-impression pausing. Live mutation still requires the Postgres `Autopilot enabled`, `Allow mutations`, and `Dry run` settings to be deliberately aligned.
- Ad Factory: OpenAI-powered PMax/RSA/DSA copy drafts saved in Postgres with character-limit validation and Google AI automation flags. PMax draft creation stores text assets first because live PMax launch also requires image/logo assets.

The Conversion Goals page syncs:

- Customer conversion goals.
- Campaign conversion goals.
- Campaign `goal_config_level` and custom goal assignments.

The Keyword Bank page syncs:

- Search terms and search-term insight categories from saved Google Ads insight snapshots.
- One normalized keyword row per Google Ads account, with clicks, conversions, value, source campaigns, and first/last seen timestamps.
- Exact, phrase, or plain one-click copy lists for manual Google Ads review.

Queue the daily keyword pull from cron or launchd while the Dramatiq worker is running:

```bash
python scripts/queue_daily_keyword_sync.py --days 60 --max-rows 5000
```

Queue the Google Ads automation monitor continuously with a light local scheduler:

```bash
scripts/run_automation_scheduler_loop.sh
```

The loop wakes every 15 minutes, refreshes saved schedule decisions hourly, and only queues work when an account is due for the daily low-traffic pull, the 6-hour Odoo sales budget guard, or an hourly peak-budget check.

Automation budget policy:

- The Odoo sales guard uses a rolling sales window, not a midnight-reset counter.
- Normal account spend is capped at 15% of synced Odoo website sales.
- During the conversion peak window, eligible campaign budgets can use a temporary 5% extra sales buffer, so the peak cap is 20%.
- After the peak window, original campaign budgets are restored first, then the normal 15% rolling sales cap is enforced again.
- Fix / Watch campaigns use a 350% Target ROAS repair threshold.
- Accounts with no last-7-day impressions enter a 14-day Testing / Discovery bootstrap: Maximize Clicks RSA from the keyword bank plus Maximize Clicks Dynamic Search Ads, with no PMax.
- PMax drafts are planned only after the account has enough last-7-day purchase conversions, default 5.
- Every account can maintain a Testing / Discovery all-pages Dynamic Search Ad draft using 5% of rolling Odoo sales, while still respecting the account spend guard.
- Automation-created campaign names include the campaign category plus a deterministic `AUTO-...` code derived from the Google Ads customer, domain, category, and campaign intent. If the account is reconnected later, fresh Google campaign metrics let automation match that visible code and resume the same campaign instead of planning a duplicate.

## Setup

1. Copy `.env.example` to `.env`.
2. Put your Google Ads credentials in `.env`.
   If you already have a Python script that defines `GOOGLE_ADS_CONFIG` and `CUSTOMER_ID`, you can avoid duplicating credentials by setting `GOOGLE_ADS_CONFIG_PY=/absolute/path/to/script.py`.
3. Run a read-only recommendation pass:

```bash
python3 scripts/google_ads_optimizer.py recommend
```

4. Check whether the Google Ads connection works:

```bash
python3 scripts/google_ads_optimizer.py connection
```

5. Show the latest run history and the changes made:

```bash
python3 scripts/google_ads_optimizer.py history
```

6. Validate intended API mutations without applying them:

```bash
python3 scripts/google_ads_optimizer.py validate
```

7. Apply guarded changes only after you have reviewed the report:

```bash
GOOGLE_ADS_ALLOW_MUTATIONS=true GOOGLE_ADS_DRY_RUN=false python3 scripts/google_ads_optimizer.py apply
```

## What It Optimizes

- Daily budgets for enabled campaigns with non-shared budgets.
- Target ROAS on campaigns using `MAXIMIZE_CONVERSION_VALUE`.
- Target CPA on campaigns using `MAXIMIZE_CONVERSIONS`.
- Spend reduction on high-spend, low-conversion campaigns from the last 7 days.
- Trend checks for campaigns whose weekly spend is holding or rising while sales drop.
- Scaling signals for campaigns with strong ROAS and enough conversion volume.
- Target ROAS unlocks for campaigns with 0 impressions in the last 7 days; those are reduced to 100% ROAS.
- Recommendations and search-term waste are reported for human review.

The default policy prevents total daily budget from increasing. Budget increases for winning campaigns are funded only by reductions elsewhere unless `GOOGLE_ADS_ALLOW_TOTAL_BUDGET_INCREASE=true`.

## Run Audit

Every run writes a full JSON report to `reports/` and appends a compact audit record to `state/google_ads_optimizer_runs.jsonl`. The audit record includes connection status, planned changes, skipped/review-only changes, and applied changes. Use `python3 scripts/google_ads_optimizer.py history` to see the latest runs from the terminal.

## Codex Cloud / Server Migration

Cloud and production servers run in isolated environments. They cannot read this machine's ignored `.env` files, so the app treats Postgres as the portable source of truth.

Set only runtime/app variables in the deployment environment:

```bash
DATABASE_URL or POSTGRES_URL
SECRET_KEY
ADMIN_EMAIL
ADMIN_PASSWORD
```

Google Ads developer token, OAuth client details, refresh tokens, Google Analytics refresh tokens, automation settings, reports, and cooldown state are saved in Postgres from the Settings / Analytics pages. A server migration should restore or point to the same Postgres database, run migrations/init, then start the web and worker processes.

Then use the setup script:

```bash
bash scripts/setup_cloud_env.sh
```

For active mutation runs, set `GOOGLE_ADS_ALLOW_MUTATIONS=true` and `GOOGLE_ADS_DRY_RUN=false`. For read-only runs, set `GOOGLE_ADS_ALLOW_MUTATIONS=false` and `GOOGLE_ADS_DRY_RUN=true`. Legacy Google credential environment variables are only a one-time import fallback for old scripts; they are not required by the app/worker once credentials are saved in Postgres.

## Manager Automation

Manager account groups should be stored in Postgres from the app. For legacy local scripts, copy `config/google_ads_accounts.example.json` to a private ignored config file and fill your own manager/customer IDs.

Run the optimizer across configured sub-accounts:

```bash
python3 scripts/gofinch_google_ads_optimizer.py connection
python3 scripts/gofinch_google_ads_optimizer.py recommend
```

Limit a run to one account by ID or name fragment:

```bash
python3 scripts/gofinch_google_ads_optimizer.py recommend --account 123-456-7890
python3 scripts/gofinch_google_ads_optimizer.py history --account example
```

Apply guarded changes after reviewing reports:

```bash
GOOGLE_ADS_ALLOW_MUTATIONS=true GOOGLE_ADS_DRY_RUN=false python3 scripts/gofinch_google_ads_optimizer.py apply
```

## Important PMax Note

Google Ads API does not support a Maximize Clicks bidding strategy for Performance Max. Performance Max supports Maximize Conversions and Maximize Conversion Value, with optional target CPA or target ROAS. Creating a serving PMax campaign also requires assets and an asset group, not just a budget and bidding setting.
