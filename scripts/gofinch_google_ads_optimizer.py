#!/usr/bin/env python3
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Set

import google_ads_optimizer


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = ROOT / "config" / "gofinch_google_ads_accounts.json"
DEFAULT_ENV_PATH = ROOT / ".env.gofinch"
MODES = ["recommend", "validate", "apply", "connection", "history", "pmax-clicks"]


@dataclasses.dataclass(frozen=True)
class Account:
    name: str
    customer_id: str


@dataclasses.dataclass(frozen=True)
class AccountRun:
    manager: Account
    account: Account


def clean_customer_id(value: str) -> str:
    return re.sub(r"[^\d]", "", str(value or ""))


def format_customer_id(value: str) -> str:
    digits = clean_customer_id(value)
    if len(digits) != 10:
        return value
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def load_env_file(
    path: Path,
    *,
    override: bool,
    preserve_keys: Optional[Set[str]] = None,
) -> None:
    preserve_keys = preserve_keys or set()
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in preserve_keys:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def parse_account(raw: dict, path: Path, label: str) -> Account:
    account = Account(
        name=str(raw["name"]),
        customer_id=clean_customer_id(str(raw["customer_id"])),
    )
    if not account.customer_id:
        raise ValueError(f"{label} customer_id is missing in {path}")
    return account


def load_account_config(path: Path, *, include_managers: bool) -> List[AccountRun]:
    data = json.loads(path.read_text())
    if "manager_groups" in data:
        groups = data["manager_groups"]
    else:
        groups = [{"manager": data["manager"], "sub_accounts": data["sub_accounts"]}]

    runs: List[AccountRun] = []
    for index, group in enumerate(groups, start=1):
        manager = parse_account(group["manager"], path, f"Manager group {index}")
        sub_accounts = [
            parse_account(item, path, f"Manager group {index} sub-account")
            for item in group.get("sub_accounts", [])
        ]
        if not sub_accounts:
            raise ValueError(f"No sub_accounts found for {manager.name} in {path}")
        if include_managers:
            runs.append(AccountRun(manager=manager, account=manager))
        runs.extend(AccountRun(manager=manager, account=account) for account in sub_accounts)
    if not runs:
        raise ValueError(f"No accounts found in {path}")
    return runs


def account_matches(run: AccountRun, selectors: Iterable[str]) -> bool:
    selectors = list(selectors)
    if not selectors:
        return True
    account_id = clean_customer_id(run.account.customer_id)
    account_name = run.account.name.lower()
    manager_id = clean_customer_id(run.manager.customer_id)
    manager_name = run.manager.name.lower()
    for selector in selectors:
        selector_id = clean_customer_id(selector)
        if selector_id and selector_id in {account_id, manager_id}:
            return True
        selector_text = selector.lower()
        if selector_text in account_name or selector_text in manager_name:
            return True
    return False


def configure_account(
    manager: Account,
    account: Account,
    report_root: str,
    state_root: str,
) -> None:
    account_id = clean_customer_id(account.customer_id)
    os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"] = clean_customer_id(manager.customer_id)
    os.environ["GOOGLE_ADS_CUSTOMER_ID"] = account_id
    os.environ["GOOGLE_ADS_ACCOUNT_NAME"] = account.name
    os.environ["GOOGLE_ADS_REPORT_DIR"] = str(Path(report_root) / account_id)
    os.environ["GOOGLE_ADS_STATE_DIR"] = str(Path(state_root) / account_id)


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the guarded Google Ads optimizer for Gofinch sub-accounts."
    )
    parser.add_argument("mode", choices=MODES)
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        help=(
            "Limit to an account/manager id or a case-insensitive name fragment. "
            "Can be repeated."
        ),
    )
    parser.add_argument(
        "--include-manager",
        action="store_true",
        help="Also run the selected mode against the Gofinch manager account.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the Gofinch account config JSON.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help="Path to the ignored Gofinch credential env file.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop after the first account that returns a non-zero exit code.",
    )
    return parser.parse_args(argv)


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    original_env_keys = set(os.environ)
    load_env_file(ROOT / ".env", override=False)
    load_env_file(
        args.env_file.expanduser(),
        override=True,
        preserve_keys=original_env_keys,
    )

    runs = load_account_config(args.config, include_managers=args.include_manager)
    runs = [run for run in runs if account_matches(run, args.account)]
    if not runs:
        print("No Gofinch accounts matched the requested selector.", file=sys.stderr)
        return 1

    report_root = (
        os.getenv("GOFINCH_GOOGLE_ADS_REPORT_DIR")
        or os.getenv("GOOGLE_ADS_REPORT_DIR")
        or "reports/gofinch"
    )
    state_root = (
        os.getenv("GOFINCH_GOOGLE_ADS_STATE_DIR")
        or os.getenv("GOOGLE_ADS_STATE_DIR")
        or "state/gofinch"
    )

    failures = 0
    for index, run in enumerate(runs, start=1):
        configure_account(run.manager, run.account, report_root, state_root)
        print(
            "\n"
            f"=== Gofinch {index}/{len(runs)}: "
            f"{run.account.name} ({format_customer_id(run.account.customer_id)}) "
            f"via {run.manager.name} ({format_customer_id(run.manager.customer_id)}) ==="
        )
        result = google_ads_optimizer.main([args.mode])
        if result != 0:
            failures += 1
            if args.stop_on_error:
                return result

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
