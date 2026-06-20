"""
RobinHealth: Postgres connection management.

Two modes depending on how the module is used:

  POOLED (production / API server):
    Call db.init_pool() once at startup (in api.py's lifespan handler).
    Subsequent db.connection() calls check out a connection from the pool
    and return it when the context manager exits.  The pool is configured
    via the same environment variables as the direct-connection mode.

    Pool parameters (environment variables):
        DB_POOL_MIN_CONN   default: 2
        DB_POOL_MAX_CONN   default: 10

  DIRECT (tests / worker process):
    If init_pool() has never been called, db.connection() opens a fresh
    connection each call and closes it on exit -- exactly the original
    behaviour, so nothing in test_pipeline.py / test_repository.py /
    worker.py needs to change.

Configuration (environment variables, shared between both modes):
    DATABASE_URL          full DSN, e.g. postgresql://user:pass@host:5432/db
                           -- takes priority over the individual vars below
    ROBINHEALTH_DB_HOST    default: localhost
    ROBINHEALTH_DB_PORT    default: 5432
    ROBINHEALTH_DB_NAME    default: robinhealth
    ROBINHEALTH_DB_USER    default: robinhealth
    ROBINHEALTH_DB_PASSWORD default: robinhealth
"""

from __future__ import annotations

import os
import urllib.parse
from contextlib import contextmanager
from typing import Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool


_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    host = os.environ.get("ROBINHEALTH_DB_HOST", "localhost")
    port = os.environ.get("ROBINHEALTH_DB_PORT", "5432")
    dbname = os.environ.get("ROBINHEALTH_DB_NAME", "robinhealth")
    user = os.environ.get("ROBINHEALTH_DB_USER", "robinhealth")
    password = os.environ.get("ROBINHEALTH_DB_PASSWORD", "robinhealth")
    # URL-encode user/password -- many managed Postgres providers
    # auto-generate passwords containing @, :, or / by default, which
    # would otherwise be misparsed (verified directly: an unencoded
    # "p@ss:word/123" produced a DSN that parsed to host="ss" and
    # password="p", silently wrong rather than a clean failure).
    user = urllib.parse.quote(user, safe="")
    password = urllib.parse.quote(password, safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def init_pool(
    min_conn: int | None = None,
    max_conn: int | None = None,
) -> None:
    """
    Create the module-level connection pool.  Call once at application
    startup (api.py lifespan).  Safe to call multiple times -- a second
    call closes the existing pool and opens a new one (useful for tests
    that need to reinitialise with a different DSN).

    min_conn / max_conn default to DB_POOL_MIN_CONN / DB_POOL_MAX_CONN
    env vars (defaults: 2 / 10).
    """
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None

    min_c = min_conn or int(os.environ.get("DB_POOL_MIN_CONN", "2"))
    max_c = max_conn or int(os.environ.get("DB_POOL_MAX_CONN", "10"))
    _pool = psycopg2.pool.ThreadedConnectionPool(min_c, max_c, _dsn())


def close_pool() -> None:
    """Close all connections in the pool.  Call at shutdown (api.py lifespan)."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


def get_connection() -> psycopg2.extensions.connection:
    """
    Return a connection.  If a pool is active, checks one out; otherwise
    opens a fresh direct connection.

    Rows from cursor.fetchall()/fetchone() come back as plain tuples by
    default -- repository.py uses psycopg2.extras.RealDictCursor explicitly
    wherever it wants dict-like, column-name-keyed rows instead.

    Prefer connection() below over calling this directly.
    """
    if _pool is not None:
        return _pool.getconn()
    return psycopg2.connect(_dsn())


def _return_connection(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool, or close it if no pool is active."""
    if _pool is not None:
        _pool.putconn(conn)
    else:
        conn.close()


@contextmanager
def connection() -> Iterator[psycopg2.extensions.connection]:
    """
    Context manager that commits on success, rolls back on exception, and
    ALWAYS returns the connection to the pool (or closes it) on exit.

    This exists because psycopg2 connections support `with conn:` too,
    but that form only wraps the transaction (commit/rollback) -- it does
    NOT close/return the connection, a well-known psycopg2 gotcha that
    would leak a connection on every call.  Verified empirically, not
    just assumed: `with get_connection(): pass` leaves conn.closed == 0
    afterward.
    """
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _return_connection(conn)
