"""
RobinHealth: one-time database setup.

Applies the SQL schema files against DATABASE_URL, in dependency order:
    db/schema.sql        -> core tables (patients, cases, facilities, ...)
    db/jobs_schema.sql   -> background-job queue
    db/bills_schema.sql  -> bills/EOB/negotiations + additive columns
                            (patients.plan, cases.synthesis_json, ...)

Safe to re-run: if a file's objects already exist, that file is skipped
rather than erroring -- so it's fine to leave AUTO_MIGRATE on across deploys.
(It's intended for first-time setup; adding brand-new columns to an
already-populated database is still best done as a deliberate migration.)

Two ways to use it:
    python migrate.py                 # run it once by hand
    AUTO_MIGRATE=true (env var)        # api.py applies it automatically on boot
"""
from __future__ import annotations

import os

import psycopg2

import db

# Postgres error codes meaning "this object already exists" -- duplicate
# table / object(type/enum) / column / schema / function. If a whole file
# trips one of these, the schema is already in place, so we skip it.
_ALREADY_EXISTS = {"42P07", "42710", "42701", "42P06", "42723"}
_FILES = ["schema.sql", "jobs_schema.sql", "bills_schema.sql"]


def run_migrations() -> None:
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "db")
    for name in _FILES:
        path = os.path.join(base, name)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        try:
            with db.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
            print(f"[migrate] applied {name}")
        except psycopg2.Error as exc:
            if exc.pgcode in _ALREADY_EXISTS:
                print(f"[migrate] {name}: objects already exist, skipping")
            else:
                raise


if __name__ == "__main__":
    run_migrations()
    print("[migrate] done")
