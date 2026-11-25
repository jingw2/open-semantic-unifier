"""Live connector proof against real PostgreSQL, MySQL, and MongoDB.

Opt-in: RUN_DB_CONNECTOR_TESTS=true. Starts isolated containers on free ports with
ephemeral (anonymous, removed with --rm) storage, seeds source rows, then exercises
test_db_connection, read_db_dataframe, extract_db_to_parquet, write_dataframe, and a
read-back of the written target. One L1 job runs database-to-database (PostgreSQL
source to MySQL target).
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RUN = os.getenv("RUN_DB_CONNECTOR_TESTS") == "true"
pytestmark = pytest.mark.skipif(
    not RUN,
    reason="set RUN_DB_CONNECTOR_TESTS=true to run live PostgreSQL/MySQL/MongoDB connectors",
)

from app.core.security import encrypt_secret
from app.models.entities import DataSource, Job, Project, Workspace
from app.services.connectors import extract_db_to_parquet, read_db_dataframe, test_db_connection as check_db_connection
from app.services.etl import write_dataframe

IMAGES = {
    "postgresql": "postgres:16",
    "mysql": "mysql:8",
    "mongodb": "mongo:7.0",
}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run(*args, **kwargs):
    return subprocess.run(list(args), check=True, capture_output=True, text=True, **kwargs)


def _wait_connector(cfg, timeout=120.0):
    last = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            check_db_connection(cfg)
            return
        except Exception as exc:
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"connector never became ready: {last}")


@pytest.fixture(scope="module")
def live_dbs():
    ports = {"postgresql": _free_port(), "mysql": _free_port(), "mongodb": _free_port()}
    prefix = f"osu-etl-{os.getpid()}-{int(time.time())}"
    containers = []

    def start(name, image, env, container_port):
        host_port = ports[name]
        args = ["docker", "run", "-d", "--rm", "--name", f"{prefix}-{name}"]
        for key, value in env.items():
            args += ["-e", f"{key}={value}"]
        args += ["-p", f"127.0.0.1:{host_port}:{container_port}", image]
        _run(*args)
        containers.append(f"{prefix}-{name}")
        return host_port

    pg_port = start("postgresql", IMAGES["postgresql"],
                    {"POSTGRES_PASSWORD": "etlpass", "POSTGRES_DB": "testdb"}, 5432)
    my_port = start("mysql", IMAGES["mysql"],
                    {"MYSQL_ROOT_PASSWORD": "etlpass", "MYSQL_DATABASE": "testdb"}, 3306)
    mongo_port = start("mongodb", IMAGES["mongodb"], {}, 27017)

    configs = {
        "postgresql": {"db_type": "postgresql", "host": "127.0.0.1", "port": pg_port,
                       "user": "postgres", "password": "etlpass", "database": "testdb"},
        "mysql": {"db_type": "mysql", "host": "127.0.0.1", "port": my_port,
                  "user": "root", "password": "etlpass", "database": "testdb"},
        "mongodb": {"db_type": "mongodb", "uri": f"mongodb://127.0.0.1:{mongo_port}/", "database": "testdb"},
    }

    try:
        for cfg in configs.values():
            _wait_connector(cfg)
        yield configs
    finally:
        for name in reversed(containers):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _seed_rows(cfg):
    db_type = cfg["db_type"]
    if db_type == "postgresql":
        import psycopg2
        con = psycopg2.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                               password=cfg["password"], dbname=cfg["database"])
        with con.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS suppliers (supplier_raw_name text, order_amount_cny numeric)")
            cur.execute("DELETE FROM suppliers")
            cur.executemany(
                "INSERT INTO suppliers (supplier_raw_name, order_amount_cny) VALUES (%s, %s)",
                [("Acme", 100.0), ("Beta", 200.0), ("Gamma", 300.0)],
            )
        con.commit()
        con.close()
    elif db_type == "mysql":
        import pymysql
        con = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                              password=cfg["password"], database=cfg["database"])
        with con.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS suppliers (supplier_raw_name varchar(255), order_amount_cny decimal(12,2))")
            cur.execute("DELETE FROM suppliers")
            cur.executemany(
                "INSERT INTO suppliers (supplier_raw_name, order_amount_cny) VALUES (%s, %s)",
                [("Acme", 100.0), ("Beta", 200.0), ("Gamma", 300.0)],
            )
        con.commit()
        con.close()
    else:
        import pymongo
        client = pymongo.MongoClient(cfg["uri"], serverSelectionTimeoutMS=5000)
        collection = client[cfg["database"]]["suppliers"]
        collection.drop()
        collection.insert_many([
            {"supplier_raw_name": "Acme", "order_amount_cny": 100.0},
            {"supplier_raw_name": "Beta", "order_amount_cny": 200.0},
            {"supplier_raw_name": "Gamma", "order_amount_cny": 300.0},
        ])
        client.close()


def _exercise_connector(cfg, tmp_path):
    _seed_rows(cfg)
    info = check_db_connection(cfg)
    assert info["status"] == "ok"
    assert "suppliers" in info["tables"]

    frame = read_db_dataframe(cfg, "suppliers", limit=None)
    assert len(frame) == 3
    assert "supplier_raw_name" in frame.columns

    parquet_path = str(tmp_path / f"{cfg['db_type']}_extract.parquet")
    _, extracted = extract_db_to_parquet(cfg, "suppliers", out_parquet=parquet_path)
    assert extracted == 3
    assert os.path.exists(parquet_path)

    assert write_dataframe(cfg, "etl_target", frame, "replace") == 3
    assert write_dataframe(cfg, "etl_target", frame, "append") == 3
    back = read_db_dataframe(cfg, "etl_target", limit=None)
    assert len(back) == 6


def test_live_postgresql_connector(live_dbs, tmp_path):
    _exercise_connector(live_dbs["postgresql"], tmp_path)


def test_live_mysql_connector(live_dbs, tmp_path):
    _exercise_connector(live_dbs["mysql"], tmp_path)


def test_live_mongodb_connector(live_dbs, tmp_path):
    _exercise_connector(live_dbs["mongodb"], tmp_path)


def test_l1_postgres_source_to_mysql_target(live_dbs, tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.db import Base
    from app.services.storage import storage
    from app.workers.tasks import run_job_sync

    pg = live_dbs["postgresql"]
    my = live_dbs["mysql"]
    _seed_rows(pg)

    engine = create_engine(f"sqlite:///{tmp_path / 'l1.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("app.workers.tasks.SessionLocal", maker)
    monkeypatch.setattr(storage, "local_root", str(tmp_path / "storage"))

    with maker() as db:
        db.add(Workspace(id="ws", name="WS", slug="ws-l1"))
        db.flush()
        db.add(Project(id="proj", workspace_id="ws", name="P", slug="p-l1"))
        db.flush()
        src = DataSource(id="src", project_id="proj", name="pg suppliers", source_type="postgresql",
                         table_name="suppliers", encrypted_config=encrypt_secret(json.dumps(pg)))
        tgt = DataSource(id="tgt", project_id="proj", name="mysql target", source_type="mysql",
                         encrypted_config=encrypt_secret(json.dumps(my)))
        db.add_all([src, tgt])
        job = Job(project_id="proj", stage="L1", status="PENDING", params_json={
            "datasource_ids": ["src"],
            "etl_target_datasource_id": "tgt",
            "etl_target_table": "supplier_clean",
            "etl_write_mode": "replace",
        })
        db.add(job)
        db.commit()
        job_id = job.id

    result = run_job_sync(job_id)

    assert result["etl"]["status"] == "SUCCEEDED"
    assert result["etl"]["target_type"] == "mysql"
    assert result["etl"]["rows_written"] == 3
    back = read_db_dataframe(my, "supplier_clean", limit=None)
    assert len(back) == 3
    assert "canonical_id" in back.columns
