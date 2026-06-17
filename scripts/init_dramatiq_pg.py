#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

import psycopg2

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app.config import get_settings


DUPLICATE_OBJECT_CODES = {"42P06", "42P07", "42701", "42710"}


def main() -> None:
    import dramatiq_pg

    schema_path = Path(dramatiq_pg.__file__).with_name("schema.sql")
    sql = (
        schema_path.read_text()
        .replace('\\set ON_ERROR_STOP on\n', "")
        .replace("\\set schema 'dramatiq'\n", "")
        .replace("\\set state 'state'\n", "")
        .replace("\\set queue 'queue'\n", "")
        .replace(':"schema"', "dramatiq")
        .replace(':"state"', "state")
        .replace(':"queue"', "queue")
        .replace(") WITHOUT OIDS;", ");")
        .replace(
            'CREATE INDEX ON dramatiq.queue("state", mtime);',
            'CREATE INDEX IF NOT EXISTS dramatiq_queue_state_mtime_idx ON dramatiq.queue("state", mtime);',
        )
    )
    settings = get_settings()
    with psycopg2.connect(settings.dramatiq_pg_url) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for statement in [part.strip() for part in sql.split(";") if part.strip()]:
                try:
                    cursor.execute(f"{statement};")
                except psycopg2.Error as exc:
                    connection.rollback()
                    if exc.pgcode in DUPLICATE_OBJECT_CODES or "already exists" in str(exc):
                        continue
                    raise
    print("Dramatiq Postgres schema is ready.")


if __name__ == "__main__":
    main()
