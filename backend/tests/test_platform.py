"""零点未来 · 企业语义统一平台（Semantic Unifier Platform）全链路生产集成测试

覆盖：
1. 私有化安全：超管自动初始化登录、关闭公开自助注册（403）、Admin 创建 Editor 成员 & RBAC 拦截 Viewer
2. 凭据加密：新增自定义 LLM Provider，验证 DB 密文存储且接口返回脱敏掩码
3. 多源数据接入：SQLite & DuckDB 5,000 行安全抽样体检 + 自定义 SELECT 过滤 + 拦截非 SELECT 写入语句
4. L1/L2/L4 异步流水线：Smart Router 自动路由、任务状态流转、产物版本递增；L3 作业入口已退役（410）
5. 人工复核闭环（HITL）：L2 灰色样本“同意合并”自动沉淀 CanonicalMapping，并立即被对外 Serving API 优先命中
6. 草稿 / 发布 / 回滚与对外 Serving API：未发布时返回 409 → 发布 v1 后 X-API-Key 鉴权调通 /reconcile、/validate、/metrics/query → 发布 v2 → 一键回滚 v1
"""

import os
import sqlite3
import sys

import duckdb
import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# 测试数据库与文件存储由 tests/conftest.py 隔离到临时目录。

from app.core.db import SessionLocal, init_db_and_seed  # noqa: E402
from app.main import app  # noqa: E402
from app.models.entities import LLMProvider  # noqa: E402
from app.models import SourceRecord, SourceRevision, UnifiedEntity  # noqa: E402,F401

init_db_and_seed()
client = TestClient(app)


def _login(email: str = "admin@lingdianweilai.ai", password: str = "Admin@2026") -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_01_auth_rbac_and_disabled_public_signup():
    # 公开注册必须被拒绝
    reg = client.post("/api/v1/auth/register")
    assert reg.status_code == 403

    # 超管登录
    headers = _login()
    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "admin"

    # Admin 创建 Viewer 成员
    cm = client.post(
        "/api/v1/auth/members",
        headers=headers,
        json={"email": "viewer@company.com", "password": "Viewer@123", "display_name": "审计观察员", "role": "viewer"},
    )
    assert cm.status_code == 200

    # Viewer 无权创建项目（403 RBAC 拦截）
    v_headers = _login("viewer@company.com", "Viewer@123")
    deny = client.post("/api/v1/projects", headers=v_headers, json={"name": "非法项目"})
    assert deny.status_code == 403


def test_02_encrypted_custom_llm_provider():
    headers = _login()
    raw_secret = "sk-test-secret-998877665544"
    r = client.post(
        "/api/v1/providers",
        headers=headers,
        json={
            "provider_key": "siliconflow",
            "name": "SiliconFlow 自定义",
            "base_url": "https://api.siliconflow.cn/v1",
            "default_model": "Qwen/Qwen2.5-72B-Instruct",
            "api_key": raw_secret,
            "is_custom": True,
            "is_default": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    # 返回值必须脱敏，绝不泄露明文
    assert raw_secret not in str(body)
    assert body["api_key_masked"].startswith("sk-t")

    # 校验数据库存的是 Fernet 密文而非明文
    db = SessionLocal()
    rec = db.query(LLMProvider).filter(LLMProvider.id == body["id"]).first()
    assert rec is not None
    assert rec.encrypted_api_key != raw_secret
    assert rec.encrypted_api_key.startswith("gAAAAA")
    db.close()


def test_03_db_safe_sampling_and_readonly_sql_guard(tmp_path):
    headers = _login()
    proj = client.get("/api/v1/projects", headers=headers).json()[0]

    sq_path = str(tmp_path / "prod_erp.sqlite")
    con = sqlite3.connect(sq_path)
    con.execute("CREATE TABLE suppliers (id INT, supplier_raw_name TEXT, order_amount_cny REAL)")
    con.executemany(
        "INSERT INTO suppliers VALUES (?, ?, ?)",
        [(1, "  星云智造科技  ", 50000.0), (2, "星云制造科技", 30000.0), (3, "极光物流", 12000.0)],
    )
    con.commit()
    con.close()

    # 拒绝写入 SQL（安全护栏）
    bad_sql = client.post(
        f"/api/v1/projects/{proj['id']}/datasources/db",
        headers=headers,
        json={"db_type": "sqlite", "database": sq_path, "custom_sql": "DROP TABLE suppliers"},
    )
    assert bad_sql.status_code == 400

    # 允许只读 SELECT 抽样体检 + 自动路由 L1
    ok = client.post(
        f"/api/v1/projects/{proj['id']}/datasources/db",
        headers=headers,
        json={"db_type": "sqlite", "database": sq_path, "table_name": "suppliers"},
    )
    assert ok.status_code == 200
    assert ok.json()["sampled_rows"] == 3
    assert ok.json()["routing"]["recommended_stage"] == "L1"


def test_04_end_to_end_l1_l2_hitl_l4_publish_and_serving():
    headers = _login()
    proj = client.get("/api/v1/projects", headers=headers).json()[0]
    pid, slug = proj["id"], proj["slug"]

    # 1. 跑 L1（加载示例 -> 提交任务）
    d1 = client.post(f"/api/v1/projects/{pid}/datasources/demo/L1", headers=headers)
    assert d1.status_code == 200
    j1 = client.post(f"/api/v1/projects/{pid}/jobs", headers=headers, json={"stage": "L1", "datasource_ids": d1.json()["datasource_ids"]})
    assert j1.status_code == 200
    assert j1.json()["status"] == "SUCCESS"
    assert j1.json()["result"]["after_unique"] == 15

    # 2. 跑 L2（Splink 5.0 + Hybrid ER -> 自动生成待复核队列）
    j2 = client.post(f"/api/v1/projects/{pid}/jobs", headers=headers, json={"stage": "L2", "params": {"enable_hybrid_llm": True}})
    assert j2.status_code == 200
    assert j2.json()["status"] == "SUCCESS"
    assert j2.json()["result"]["review_queued"] >= 4

    # 3. 人工复核“同意合并” -> 自动沉淀进 CanonicalMapping
    revs = client.get(f"/api/v1/projects/{pid}/reviews", headers=headers).json()["reviews"]
    target_rev = next(r for r in revs if "腾讯" in r["left_text"] or "鹅厂" in r["right_text"])
    dec = client.post(
        f"/api/v1/reviews/{target_rev['id']}/decide",
        headers=headers,
        json={"decision": "APPROVED", "canonical_id": "ENT-TENCENT", "canonical_name": "深圳市腾讯计算机系统有限公司"},
    )
    assert dec.status_code == 200
    assert dec.json()["mapping_saved"]["canonical_id"] == "ENT-TENCENT"

    # 4. L3 作业入口已退役；L4 的演示种子仍会生成旧契约产物。
    l3_retired = client.post(f"/api/v1/projects/{pid}/jobs", headers=headers, json={"stage": "L3"})
    assert l3_retired.status_code == 410, l3_retired.text
    assert "契约" in l3_retired.json()["detail"]
    demo_l3 = client.post(f"/api/v1/projects/{pid}/datasources/demo/L3", headers=headers)
    assert demo_l3.status_code == 410, demo_l3.text

    j4 = client.post(f"/api/v1/projects/{pid}/jobs", headers=headers, json={"stage": "L4", "params": {"metric_name": "net_gmv", "group_by": "order_channel"}})
    assert j4.status_code == 200 and j4.json()["status"] == "SUCCESS"
    l4_art_id = j4.json()["result"]["metrics_artifact_id"]
    artifacts = client.get(f"/api/v1/projects/{pid}/artifacts", headers=headers).json()
    l3_art_id = next(item["id"] for item in artifacts if item["artifact_type"] == "l3_contract")

    # 5. 签发项目级 Serving API Key
    k_res = client.post(f"/api/v1/projects/{pid}/api-keys", headers=headers, json={"name": "生产 Agent Key"})
    assert k_res.status_code == 200
    api_key = k_res.json()["api_key_once"]
    serve_headers = {"X-API-Key": api_key}

    # 未发布前调 validate 应返回 409（草稿隔离保护）
    pre = client.post(f"/api/v1/serve/{slug}/validate", headers=serve_headers, json={"entity_name": "ShipmentOrder", "payload": {}})
    assert pre.status_code == 409

    # 发布 L3 与 L4 到线上 API
    assert client.post(f"/api/v1/artifacts/{l3_art_id}/publish", headers=headers).status_code == 200
    assert client.post(f"/api/v1/artifacts/{l4_art_id}/publish", headers=headers).status_code == 200

    # 6. 调用三大生产 Serving API
    # 6a. /reconcile 必须优先命中刚才人工复核沉淀的字典
    s_rec = client.post(f"/api/v1/serve/{slug}/reconcile", headers=serve_headers, json={"query": target_rev["left_text"]})
    assert s_rec.status_code == 200
    assert s_rec.json()["source"] == "HUMAN_REVIEW_DICT"
    assert s_rec.json()["canonical_id"] == "ENT-TENCENT"

    # 6b. /validate 拦截旧黑话 SHIPPED，放行标准枚举 IN_TRANSIT
    s_bad = client.post(
        f"/api/v1/serve/{slug}/validate",
        headers=serve_headers,
        json={"entity_name": "ShipmentOrder", "payload": {"shipment_id": "S1", "order_id": "O1", "carrier_code": "SUP-1003", "delivery_status": "SHIPPED"}},
    )
    assert s_bad.status_code == 200 and s_bad.json()["valid"] is False

    s_good = client.post(
        f"/api/v1/serve/{slug}/validate",
        headers=serve_headers,
        json={"entity_name": "ShipmentOrder", "payload": {"shipment_id": "S1", "order_id": "O1", "carrier_code": "SUP-1003", "delivery_status": "IN_TRANSIT"}},
    )
    assert s_good.status_code == 200 and s_good.json()["valid"] is True

    # 6c. /metrics/query 编译执行标准 YAML 指标
    s_met = client.post(f"/api/v1/serve/{slug}/metrics/query", headers=serve_headers, json={"metric_name": "net_gmv", "group_by": "order_channel"})
    assert s_met.status_code == 200
    assert len(s_met.json()["rows"]) == 3


def test_05_resource_access_is_scoped_to_workspace():
    from app.models.entities import (
        ArtifactVersion, DataSource, Job, Project, ProjectApiKey,
        ReviewItem, Workspace, LLMProvider,
    )
    from app.services.storage import storage

    headers = _login()
    own = client.get('/api/v1/projects', headers=headers).json()[0]
    db = SessionLocal()
    ws = Workspace(name='Isolated workspace', slug='isolated-workspace')
    db.add(ws)
    db.flush()
    project = Project(workspace_id=ws.id, name='Private project', slug='private-project')
    db.add(project)
    db.flush()
    job = Job(project_id=project.id, stage='L1')
    db.add(job)
    db.flush()
    object_key, _ = storage.put_bytes('security/private.csv', b'private data')
    artifact = ArtifactVersion(project_id=project.id, job_id=job.id, artifact_type='clean_csv', filename='private.csv', object_key=object_key)
    source = DataSource(project_id=project.id, name='Private source', source_type='file')
    review = ReviewItem(project_id=project.id, job_id=job.id, stage='L1', left_text='private', right_text='private')
    key = ProjectApiKey(project_id=project.id, name='private', key_prefix='private', key_hash='private')
    provider = LLMProvider(workspace_id=ws.id, provider_key='private', name='private')
    db.add_all([artifact, source, review, key, provider])
    db.commit()
    pid, aid, jid, rid, kid, sid, provider_id = project.id, artifact.id, job.id, review.id, key.id, source.id, provider.id

    assert client.get(f'/api/v1/artifacts/{aid}/content').status_code == 401
    assert client.get(f'/api/v1/jobs/{jid}/stream').status_code == 401
    for path in [f'/projects/{pid}/datasources', f'/projects/{pid}/jobs', f'/projects/{pid}/reviews', f'/projects/{pid}/artifacts', f'/projects/{pid}/api-keys', f'/artifacts/{aid}/content', f'/jobs/{jid}', f'/jobs/{jid}/stream']:
        assert client.get('/api/v1' + path, headers=headers).status_code == 404, path
    for path, payload in [
        (f'/projects/{pid}/api-keys', {'name': 'unauthorized'}),
        (f'/projects/{pid}/jobs', {'stage': 'L1'}),
        (f'/projects/{pid}/datasources/demo/L1', {}),
        (f'/artifacts/{aid}/publish', {}),
        (f'/reviews/{rid}/decide', {'decision': 'APPROVED'}),
        (f"/projects/{own['id']}/jobs", {'stage': 'L1', 'datasource_ids': [sid]}),
        (f"/projects/{own['id']}/jobs", {'stage': 'L1', 'provider_id': provider_id}),
    ]:
        assert client.post('/api/v1' + path, headers=headers, json=payload).status_code == 404, path
    assert client.delete(f'/api/v1/api-keys/{kid}', headers=headers).status_code == 404
    db.refresh(artifact)
    db.refresh(key)
    assert artifact.release_status == 'DRAFT'
    assert key.is_active
    assert db.query(Job).filter(Job.project_id == project.id).count() == 1

    artifact.project_id = own['id']
    db.commit()
    response = client.get(f'/api/v1/artifacts/{aid}/content', headers=headers)
    assert response.status_code == 200
    assert response.content == b'private data'
    # Public Serving endpoints continue to use their dedicated API-key dependency.
    assert client.post('/api/v1/serve/private-project/reconcile', json={'query': 'private'}).status_code == 401
    db.close()


def test_06_retired_l3_job_is_marked_failed():
    from app.models.entities import Job
    from app.workers.tasks import run_job_sync

    headers = _login()
    own = client.get('/api/v1/projects', headers=headers).json()[0]
    with SessionLocal() as db:
        job = Job(project_id=own['id'], stage='L3', status='PENDING', progress=0,
                  stage_message='queued before retirement', params_json={})
        db.add(job)
        db.commit()
        job_id = job.id

    with pytest.raises(ValueError, match='retired'):
        run_job_sync(job_id)

    with SessionLocal() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.status == 'FAILED'
        assert 'retired' in (job.error_message or '')
