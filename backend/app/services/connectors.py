"""生产级数据接入层：文件（CSV/XLSX/Parquet/TSV/YAML）+ 5 大数据库（5,000 行安全抽样体检 + 分批落盘 Parquet）"""

import os
import sqlite3
from typing import Any, Dict, Optional, Tuple

import duckdb
import pandas as pd

SUPPORTED_FILE_EXTS = {".csv", ".xlsx", ".xls", ".parquet", ".tsv", ".yaml", ".yml"}
SUPPORTED_DB_TYPES = {"duckdb", "sqlite", "mysql", "postgresql", "mongodb"}
SAFE_PROFILE_SAMPLE_LIMIT = 5000


def load_file(file_path: str) -> Dict[str, Any]:
    ext = os.path.splitext(file_path)[1].lower()
    name = os.path.basename(file_path)
    if ext not in SUPPORTED_FILE_EXTS:
        raise ValueError(f"暂不支持的文件格式: {ext}")

    if ext == ".csv":
        return {"kind": "table", "name": name, "ext": ext, "path": file_path, "df": pd.read_csv(file_path)}
    if ext in (".xlsx", ".xls"):
        try:
            df = pd.read_excel(file_path, engine="openpyxl" if ext == ".xlsx" else None)
        except ImportError as e:
            raise RuntimeError("读取 .xlsx 需要 openpyxl：pip install openpyxl") from e
        return {"kind": "table", "name": name, "ext": ext, "path": file_path, "df": df}
    if ext == ".parquet":
        return {"kind": "table", "name": name, "ext": ext, "path": file_path, "df": pd.read_parquet(file_path)}
    if ext == ".tsv":
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return {"kind": "tsv", "name": name, "ext": ext, "path": file_path, "df": pd.read_csv(file_path, sep="\t").fillna(""), "text": text}
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return {"kind": "yaml", "name": name, "ext": ext, "path": file_path, "text": text}


def _import_driver(db_type: str):
    if db_type == "mysql":
        try:
            import pymysql
            return pymysql
        except ImportError as e:
            raise RuntimeError("连接 MySQL 需要安装驱动：pip install pymysql") from e
    if db_type == "postgresql":
        try:
            import psycopg2
            return psycopg2
        except ImportError as e:
            raise RuntimeError("连接 PostgreSQL 需要安装驱动：pip install psycopg2-binary") from e
    if db_type == "mongodb":
        try:
            import pymongo
            return pymongo
        except ImportError as e:
            raise RuntimeError("连接 MongoDB 需要安装驱动：pip install pymongo") from e
    return None


def test_db_connection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    db_type = str(cfg.get("db_type", "")).lower().strip()
    if db_type not in SUPPORTED_DB_TYPES:
        raise ValueError(f"不支持的数据库类型: {db_type}")

    if db_type == "duckdb":
        path = cfg.get("database") or cfg.get("path") or ":memory:"
        con = duckdb.connect(path, read_only=os.path.exists(path) and path != ":memory:")
        tables = [r[0] for r in con.execute("SHOW TABLES").fetchall()]
        con.close()
        return {"status": "ok", "db_type": db_type, "tables": tables}

    if db_type == "sqlite":
        path = cfg.get("database") or cfg.get("path")
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"SQLite 文件不存在: {path}")
        con = sqlite3.connect(path)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        con.close()
        return {"status": "ok", "db_type": db_type, "tables": tables}

    if db_type == "mysql":
        pymysql = _import_driver("mysql")
        con = pymysql.connect(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port") or 3306),
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", ""),
            connect_timeout=5,
        )
        with con.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [r[0] for r in cur.fetchall()]
        con.close()
        return {"status": "ok", "db_type": db_type, "tables": tables}

    if db_type == "postgresql":
        psycopg2 = _import_driver("postgresql")
        con = psycopg2.connect(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port") or 5432),
            user=cfg.get("user", "postgres"),
            password=cfg.get("password", ""),
            dbname=cfg.get("database", "postgres"),
            connect_timeout=5,
        )
        with con.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
            tables = [r[0] for r in cur.fetchall()]
        con.close()
        return {"status": "ok", "db_type": db_type, "tables": tables}

    pymongo = _import_driver("mongodb")
    uri = cfg.get("uri") or f"mongodb://{cfg.get('host', '127.0.0.1')}:{int(cfg.get('port') or 27017)}/"
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    tables = client[cfg.get("database", "test")].list_collection_names()
    client.close()
    return {"status": "ok", "db_type": db_type, "tables": tables}


def _build_safe_query(db_type: str, table_name: str, custom_sql: Optional[str], limit: Optional[int]) -> str:
    if custom_sql and custom_sql.strip():
        sql = custom_sql.strip().rstrip(";")
        if not sql.lower().startswith("select"):
            raise ValueError("出于生产库安全保护，自定义查询仅允许只读 SELECT 语句！")
        return f"SELECT * FROM ({sql}) AS _sub LIMIT {int(limit)}" if limit else sql
    safe_table = table_name.replace('"', "").replace("`", "").replace(";", "")
    q = f"SELECT * FROM `{safe_table}`" if db_type == "mysql" else f'SELECT * FROM "{safe_table}"'
    return f"{q} LIMIT {int(limit)}" if limit else q


def read_db_dataframe(
    cfg: Dict[str, Any],
    table_name: str,
    custom_sql: Optional[str] = None,
    limit: Optional[int] = SAFE_PROFILE_SAMPLE_LIMIT,
) -> pd.DataFrame:
    """安全读取数据库表（体检默认仅抽样前 5,000 行；Worker 异步任务可全量读取并落盘 Parquet）"""
    db_type = str(cfg.get("db_type", "")).lower().strip()
    if db_type == "duckdb":
        con = duckdb.connect(cfg.get("database") or cfg.get("path"), read_only=True)
        df = con.execute(_build_safe_query(db_type, table_name, custom_sql, limit)).df()
        con.close()
        return df

    if db_type == "sqlite":
        con = sqlite3.connect(cfg.get("database") or cfg.get("path"))
        df = pd.read_sql_query(_build_safe_query(db_type, table_name, custom_sql, limit), con)
        con.close()
        return df

    if db_type == "mysql":
        pymysql = _import_driver("mysql")
        con = pymysql.connect(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port") or 3306),
            user=cfg.get("user", "root"),
            password=cfg.get("password", ""),
            database=cfg.get("database", ""),
            connect_timeout=5,
        )
        df = pd.read_sql(_build_safe_query(db_type, table_name, custom_sql, limit), con)
        con.close()
        return df

    if db_type == "postgresql":
        psycopg2 = _import_driver("postgresql")
        con = psycopg2.connect(
            host=cfg.get("host", "127.0.0.1"),
            port=int(cfg.get("port") or 5432),
            user=cfg.get("user", "postgres"),
            password=cfg.get("password", ""),
            dbname=cfg.get("database", "postgres"),
            connect_timeout=5,
        )
        df = pd.read_sql(_build_safe_query(db_type, table_name, custom_sql, limit), con)
        con.close()
        return df

    pymongo = _import_driver("mongodb")
    uri = cfg.get("uri") or f"mongodb://{cfg.get('host', '127.0.0.1')}:{int(cfg.get('port') or 27017)}/"
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    cursor = client[cfg.get("database", "test")][table_name].find({}, {"_id": 0})
    if limit:
        cursor = cursor.limit(int(limit))
    docs = list(cursor)
    client.close()
    return pd.DataFrame(docs)


def extract_db_to_parquet(
    cfg: Dict[str, Any],
    table_name: str,
    out_parquet: str,
    custom_sql: Optional[str] = None,
    max_rows: Optional[int] = None,
) -> Tuple[str, int]:
    """Celery Worker 异步安全抽取源库落盘 Parquet，彻底隔离生产库计算压力"""
    df = read_db_dataframe(cfg, table_name=table_name, custom_sql=custom_sql, limit=max_rows)
    os.makedirs(os.path.dirname(out_parquet) or ".", exist_ok=True)
    df.to_parquet(out_parquet, index=False)
    return out_parquet, len(df)
