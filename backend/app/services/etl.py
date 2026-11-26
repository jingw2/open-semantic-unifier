"""ETL sink: write a transform stage result into a target database and resolve schedule targets.

Credentials live only inside the decrypted DataSource config. This module never logs
or embeds them in error messages; write failures surface as a generic per-db-type error.
"""

import io
import json
import math
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Tuple

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

from app.core.security import decrypt_secret
from app.models.entities import ArtifactVersion, DataSource
from app.services.storage import storage

SUPPORTED_SINK_TYPES = {"sqlite", "duckdb", "postgresql", "mysql", "mongodb"}

_TABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\Z")
_TARGET_LOCKS: dict[str, threading.Lock] = {}
_TARGET_LOCKS_GUARD = threading.Lock()


@contextmanager
def _sink_lock(config: Dict[str, Any], table: str):
    """Serialize writers for one target table. File databases also use a file lock."""
    db_type = str(config.get("db_type", "")).lower().strip()
    identity = "|".join([
        db_type,
        str(config.get("database") or config.get("path") or config.get("uri") or ""),
        str(config.get("host") or ""),
        str(config.get("port") or ""),
        table,
    ])
    with _TARGET_LOCKS_GUARD:
        lock = _TARGET_LOCKS.setdefault(identity, threading.Lock())
    file_handle = None
    with lock:
        if db_type in ("sqlite", "duckdb"):
            path = config.get("database") or config.get("path")
            if path:
                lock_path = f"{path}.{table}.etl.lock"
                try:
                    import fcntl
                    file_handle = open(lock_path, "a+")
                    fcntl.flock(file_handle, fcntl.LOCK_EX)
                except (ImportError, OSError):
                    file_handle = None
        try:
            yield
        finally:
            if file_handle is not None:
                try:
                    import fcntl
                    fcntl.flock(file_handle, fcntl.LOCK_UN)
                finally:
                    file_handle.close()


def validate_table_name(name: str) -> str:
    """Accept a plain identifier or a dotted, schema-qualified name; reject everything else.

    MongoDB collections accept the same subset. Return the name unchanged on success.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("table name must be a non-empty string")
    if not _TABLE_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid table name: {name!r}")
    return name


def write_dataframe(config: Dict[str, Any], table: str, frame: pd.DataFrame, mode: str) -> int:
    """Write a DataFrame to a target database. Returns the number of rows written.

    ``config`` is the decrypted DB DataSource config. ``mode`` is "replace" (drop and
    recreate) or "append". Credentials are never included in raised messages.
    """
    validate_table_name(table)
    db_type = str(config.get("db_type", "")).lower().strip()
    if db_type not in SUPPORTED_SINK_TYPES:
        raise ValueError(f"unsupported sink type: {db_type}")
    if mode not in ("replace", "append"):
        raise ValueError(f"invalid write mode: {mode}")
    if mode == "replace" and frame.empty:
        raise ValueError("refusing to replace a target table with an empty result")
    try:
        with _sink_lock(config, table):
            if db_type == "sqlite":
                return _write_sqlite(config, table, frame, mode)
            if db_type == "duckdb":
                return _write_duckdb(config, table, frame, mode)
            if db_type == "postgresql":
                return _write_sqlalchemy("postgresql", config, table, frame, mode)
            if db_type == "mysql":
                return _write_sqlalchemy("mysql", config, table, frame, mode)
            return _write_mongo(config, table, frame, mode)
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"database sink write failed ({db_type})") from exc


def _write_sqlite(config: Dict[str, Any], table: str, frame: pd.DataFrame, mode: str) -> int:
    import sqlite3

    path = config.get("database") or config.get("path")
    if not path:
        raise ValueError("sqlite sink requires a database path")
    con = sqlite3.connect(path)
    try:
        frame.to_sql(table, con, if_exists=mode, index=False)
    finally:
        con.close()
    return len(frame)


def _write_duckdb(config: Dict[str, Any], table: str, frame: pd.DataFrame, mode: str) -> int:
    import duckdb

    path = config.get("database") or config.get("path") or ":memory:"
    con = duckdb.connect(path)
    try:
        con.register("etl_sink_frame", frame)
        if mode == "replace":
            con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM etl_sink_frame')
        else:
            con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" AS SELECT * FROM etl_sink_frame WHERE 1=0')
            con.execute(f'INSERT INTO "{table}" SELECT * FROM etl_sink_frame')
    finally:
        con.close()
    return len(frame)


def _write_sqlalchemy(db_type: str, config: Dict[str, Any], table: str, frame: pd.DataFrame, mode: str) -> int:
    if db_type == "postgresql":
        url = URL.create(
            "postgresql+psycopg2",
            username=config.get("user", "postgres"),
            password=config.get("password", ""),
            host=config.get("host", "127.0.0.1"),
            port=int(config.get("port") or 5432),
            database=config.get("database", "postgres"),
        )
    else:
        url = URL.create(
            "mysql+pymysql",
            username=config.get("user", "root"),
            password=config.get("password", ""),
            host=config.get("host", "127.0.0.1"),
            port=int(config.get("port") or 3306),
            database=config.get("database", ""),
        )
    engine = create_engine(url)
    try:
        frame.to_sql(table, engine, if_exists=mode, index=False)
    finally:
        engine.dispose()
    return len(frame)


def _mongo_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _mongo_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mongo_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, (int, str, bool)):
        return value
    return str(value)


def _write_mongo(config: Dict[str, Any], table: str, frame: pd.DataFrame, mode: str) -> int:
    import pymongo

    uri = config.get("uri") or f"mongodb://{config.get('host', '127.0.0.1')}:{int(config.get('port') or 27017)}/"
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    try:
        collection = client[config.get("database", "test")][table]
        if mode == "replace":
            collection.drop()
        docs = [_mongo_safe(record) for record in frame.to_dict(orient="records")]
        if docs:
            collection.insert_many(docs)
    finally:
        client.close()
    return len(frame)


def load_job_frame(db: Session, stage: str, result: Dict[str, Any]) -> pd.DataFrame:
    """Select the writable frame for a finished stage.

    L1 -> the ``clean_csv`` artifact; L2 -> the full ``clusters_csv`` artifact (one
    row per record, not the pair set); L4 -> ``result_rows``.
    """
    if stage == "L4":
        return pd.DataFrame(result.get("result_rows") or [])

    artifact_type = "clean_csv" if stage == "L1" else "clusters_csv"
    for artifact in result.get("artifacts") or []:
        if artifact.get("type") == artifact_type:
            row = db.get(ArtifactVersion, artifact.get("id"))
            if row is None:
                raise ValueError(f"{artifact_type} artifact not found")
            return pd.read_csv(io.BytesIO(storage.get_bytes(row.object_key)))
    raise ValueError(f"{artifact_type} artifact not present in {stage} result")


def resolve_target(db: Session, project_id: str, datasource_id: str) -> Tuple[DataSource, Dict[str, Any]]:
    """Return (datasource, decrypted config) for a DB DataSource in the project, or raise."""
    source = (
        db.query(DataSource)
        .filter(DataSource.id == datasource_id, DataSource.project_id == project_id)
        .first()
    )
    if source is None:
        raise ValueError("target data source not found in project")
    if source.source_type not in SUPPORTED_SINK_TYPES:
        raise ValueError(f"target data source is not a writable database: {source.source_type}")
    config = json.loads(decrypt_secret(source.encrypted_config) or "{}")
    return source, config
