"""L4 executes against selected order data or refuses incompatible data."""
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_unification_public_api import public_api  # noqa: F401

from app.models.entities import DataSource, Job
from app.services.engines import build_l4_tables, execute_l4
from app.services.storage import storage
from app.workers.tasks import run_job_sync

DIRTY_CSV = b"supplier_raw_name,city\nAcme,Shenzhen\nBeta,Shanghai\n"
DIRTY_PROFILE = {
    "total_rows": 2, "table_count": 1,
    "tables": [{"name": "suppliers.csv", "rows": 2, "cols": 2, "columns": ["supplier_raw_name", "city"]}],
}


def _tables():
    return {
        "SalesOrder": pd.DataFrame([
            {"order_id": "O1", "order_channel": "CROSS_BORDER_DTC", "total_amount": 100.0},
            {"order_id": "O2", "order_channel": "DOMESTIC_B2B", "total_amount": 50.0},
        ]),
        "ShipmentOrder": pd.DataFrame([
            {"shipment_id": "S1", "order_id": "O1", "carrier_code": "SF", "delivery_status": "DELIVERED"},
            {"shipment_id": "S2", "order_id": "O2", "carrier_code": "YTO", "delivery_status": "EXCEPTION"},
        ]),
        "SettlementBill": pd.DataFrame([
            {"bill_id": "B1", "shipment_id": "S1", "settled_amount": 80.0, "settlement_status": "SETTLED"},
            {"bill_id": "B2", "shipment_id": "S2", "settled_amount": 0.0, "settlement_status": "PENDING"},
        ]),
    }


def _wide_table():
    return pd.DataFrame([
        {
            "order_id": "O1", "order_channel": "CROSS_BORDER_DTC", "total_amount": 100.0,
            "delivery_status": "DELIVERED", "settled_amount": 80.0,
        },
        {
            "order_id": "O2", "order_channel": "DOMESTIC_B2B", "total_amount": 50.0,
            "delivery_status": "EXCEPTION", "settled_amount": 0.0,
        },
    ])


@pytest.fixture
def storage_root(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "local_root", str(tmp_path / "storage"))


def _job(maker) -> str:
    with maker() as db:
        job = Job(project_id="project-one", stage="L4", status="PENDING", progress=0, stage_message="queued", params_json={})
        db.add(job)
        db.commit()
        return job.id


def test_dirty_supplier_data_is_rejected_with_a_clear_message():
    with pytest.raises(ValueError, match="缺少表"):
        build_l4_tables([pd.DataFrame([{"supplier_raw_name": "Acme", "city": "Shenzhen"}])])


def test_l4_uses_the_selected_three_tables(public_api, storage_root):
    _client, maker, _identity = public_api
    job_id = _job(maker)
    with maker() as db:
        result = execute_l4(db, "project-one", job_id, tables=_tables(), data_kind="sources")
    assert result["data_source_kind"] == "sources"
    rows = {row["order_channel"]: row["net_gmv"] for row in result["result_rows"]}
    assert rows == {"CROSS_BORDER_DTC": 100.0}
    conflict = {row["部门口径"]: row["算出金额"] for row in result["conflict_rows"]}
    assert conflict["运营部门（裸查订单表）"] == 100.0
    assert conflict["物流/经营口径（剔除异常）"] == 100.0
    assert conflict["财务部门（只认已核销）"] == 80.0


def test_l4_uses_one_wide_order_table(public_api, storage_root):
    _client, maker, _identity = public_api
    job_id = _job(maker)
    tables = build_l4_tables([_wide_table()])
    with maker() as db:
        result = execute_l4(db, "project-one", job_id, tables=tables, data_kind="sources")
    rows = {row["order_channel"]: row["net_gmv"] for row in result["result_rows"]}
    assert rows == {"CROSS_BORDER_DTC": 100.0}
    assert result["data_source_kind"] == "sources"


def test_l4_job_with_dirty_data_fails_instead_of_returning_gmv(public_api, storage_root, monkeypatch):
    _client, maker, _identity = public_api
    storage.put_bytes("projects/project-one/uploads/suppliers.csv", DIRTY_CSV)
    with maker() as db:
        source = DataSource(
            id="ds-dirty-l4", project_id="project-one", name="suppliers.csv", source_type="file",
            object_key="projects/project-one/uploads/suppliers.csv", profile_json=DIRTY_PROFILE,
        )
        db.add(source)
        job = Job(
            project_id="project-one", stage="L4", status="PENDING", progress=0,
            stage_message="queued", params_json={"datasource_ids": ["ds-dirty-l4"], "metric_name": "net_gmv"},
        )
        db.add(job)
        db.commit()
        job_id = job.id

    monkeypatch.setattr("app.workers.tasks.SessionLocal", maker)
    with pytest.raises(ValueError, match="所选数据源不包含 L4 指标"):
        run_job_sync(job_id)

    with maker() as db:
        failed = db.get(Job, job_id)
        assert failed.status == "FAILED"
        assert "所选数据源不包含 L4 指标" in failed.error_message
        assert not failed.result_json
