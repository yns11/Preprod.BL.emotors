"""Accès PostgreSQL transactionnel, concurrent et compatible Lakebase.

Les connexions sont fournies par un pool thread-safe. Le pool est renouvelé
avant l'expiration habituelle des credentials OAuth Lakebase. Une transaction
multi-requêtes n'est jamais rejouée automatiquement : le code appelant garde
ainsi la maîtrise de l'idempotence.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import get_settings

logger = logging.getLogger("bl.database")

_lock = threading.RLock()
_pool: ConnectionPool | None = None
_pool_created_at = 0.0


def _access_token() -> str:
    from databricks.sdk import WorkspaceClient

    workspace = WorkspaceClient()
    endpoint = os.environ.get("LAKEBASE_ENDPOINT", "")
    if endpoint:
        return workspace.postgres.generate_database_credential(endpoint=endpoint).token
    return workspace.config.oauth_token().access_token


def _connection_kwargs() -> dict[str, Any]:
    settings = get_settings()
    dsn = os.environ.get("BL_DATABASE_DSN", "").strip()
    if dsn:
        return {"conninfo": dsn}
    missing = [name for name in ("PGHOST", "PGDATABASE", "PGUSER") if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Ressource Lakebase absente : variables manquantes "
            + ", ".join(missing)
            + "."
        )
    return {
        "kwargs": {
            "host": os.environ["PGHOST"],
            "port": int(os.environ.get("PGPORT", "5432")),
            "dbname": os.environ["PGDATABASE"],
            "user": os.environ["PGUSER"],
            "password": _access_token(),
            "sslmode": os.environ.get("PGSSLMODE", "require"),
            "application_name": os.environ.get("PGAPPNAME", "bldemat"),
            "connect_timeout": settings.database_connect_timeout_s,
            "row_factory": dict_row,
        }
    }


def _new_pool() -> ConnectionPool:
    settings = get_settings()
    pool = ConnectionPool(
        min_size=settings.database_pool_min,
        max_size=settings.database_pool_max,
        max_lifetime=settings.database_pool_lifetime_s,
        max_idle=600,
        timeout=20,
        check=ConnectionPool.check_connection,
        open=False,
        **_connection_kwargs(),
    )
    pool.open(wait=True, timeout=settings.database_connect_timeout_s)
    return pool


def _get_pool() -> ConnectionPool:
    global _pool, _pool_created_at
    settings = get_settings()
    with _lock:
        expired = time.monotonic() - _pool_created_at >= settings.database_pool_lifetime_s
        if _pool is None or expired:
            old = _pool
            _pool = _new_pool()
            _pool_created_at = time.monotonic()
            if old is not None:
                old.close()
        return _pool


def close_pool() -> None:
    global _pool, _pool_created_at
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _pool_created_at = 0.0


@dataclass
class Transaction:
    connection: psycopg.Connection

    def execute(self, query: str, params: Mapping[str, Any] | Sequence[Any] | None = None) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.rowcount

    def executemany(
        self,
        query: str,
        params_seq: Sequence[Mapping[str, Any] | Sequence[Any]],
    ) -> int:
        with self.connection.cursor() as cursor:
            cursor.executemany(query, params_seq)
            return cursor.rowcount

    def fetch_dataframe(
        self,
        query: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> pd.DataFrame:
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
            columns = [description.name for description in cursor.description or ()]
        return pd.DataFrame(rows, columns=columns)

    def fetch_one(
        self,
        query: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> dict[str, Any] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        return dict(row) if row is not None else None


@contextmanager
def transaction() -> Iterator[Transaction]:
    """Ouvre une transaction atomique avec rollback garanti."""
    pool = _get_pool()
    with pool.connection() as connection:
        try:
            with connection.transaction():
                yield Transaction(connection)
        except Exception:
            logger.exception("Transaction Lakebase annulée")
            raise


def run(
    query: str,
    params: Mapping[str, Any] | Sequence[Any] | None = None,
    *,
    fetch: bool = False,
) -> pd.DataFrame | None:
    """Exécute une requête isolée dans sa propre transaction."""
    with transaction() as tx:
        if fetch:
            return tx.fetch_dataframe(query, params)
        tx.execute(query, params)
        return None
