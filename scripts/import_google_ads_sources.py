#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.database import AsyncSessionLocal
from app.services.google_ads_script_importer import (
    default_import_paths,
    discover_import_paths,
    import_google_ads_script_sources,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Google Ads script credentials and account lists into Postgres.")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or folders to scan. Defaults to this app's Google Ads env/script files.",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Treat path arguments as folders to recursively scan for .env and google_ads*.py files.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    paths = args.paths or default_import_paths()
    if args.discover:
        paths = discover_import_paths(paths)
    async with AsyncSessionLocal() as session:
        summary = await import_google_ads_script_sources(session, paths)
        await session.commit()
    print(f"Scanned {summary['scanned']} files.")
    print(f"Imported {summary['imported_connections']} Google Ads connection(s).")
    print(f"Linked {summary['linked_accounts']} account row(s).")
    if summary["labels"]:
        print("Connections:", ", ".join(summary["labels"]))


if __name__ == "__main__":
    asyncio.run(main())
