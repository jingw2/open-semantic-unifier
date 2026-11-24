"""项目管理 + 多源数据接入（文件上传 MinIO + 5 大数据库 5,000 行安全抽样体检与 Smart Router）"""

import json
import os
import re
import tempfile
import uuid
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_resource_access, get_current_user, require_role
from app.core.db import get_db
from app.core.security import encrypt_secret
from app.models.entities import DataSource, Project
from app.services.connectors import SAFE_PROFILE_SAMPLE_LIMIT, load_file, read_db_dataframe, test_db_connection
from app.services.engines import (
    DEFAULT_METRICS_YAML,
    DEFAULT_SCHEMASHEET_TSV,
    build_demo_cross_db_tables,
    build_demo_dirty_suppliers,
)
from app.services.profiler import profile_items, recommend_pipeline
from app.services.storage import storage

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_resource_access)], tags=["Projects & DataSources"])


class CreateProjectRequest(BaseModel):
    name: str
    slug: Optional[str] = None
    description: str = ""


class DBConnectRequest(BaseModel):
    db_type: str
    host: Optional[str] = "127.0.0.1"
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None
    uri: Optional[str] = None
    table_name: Optional[str] = None
    custom_sql: Optional[str] = None
    name: Optional[str] = None
    user_goal: Optional[str] = None


@router.get("/projects")
def list_projects(ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(Project).filter(Project.workspace_id == ctx["workspace_id"]).order_by(Project.created_at.desc()).all()
    return [{"id": p.id, "name": p.name, "slug": p.slug, "description": p.description, "created_at": p.created_at.isoformat()} for p in rows]


@router.post("/projects")
def create_project(
    req: CreateProjectRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    slug = req.slug or re.sub(r"[^a-z0-9-]+", "-", req.name.lower()).strip("-") or f"proj-{uuid.uuid4().hex[:6]}"
    if db.query(Project).filter(Project.slug == slug).first():
        slug = f"{slug}-{uuid.uuid4().hex[:4]}"
    p = Project(workspace_id=ctx["workspace_id"], name=req.name.strip(), slug=slug, description=req.description)
    db.add(p)
    db.commit()
    return {"id": p.id, "name": p.name, "slug": p.slug, "description": p.description}


@router.get("/projects/{project_id}/datasources")
def list_datasources(project_id: str, ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(DataSource).filter(DataSource.project_id == project_id).order_by(DataSource.created_at.desc()).all()
    return [
        {
            "id": d.id,
            "name": d.name,
            "source_type": d.source_type,
            "table_name": d.table_name,
            "custom_sql": d.custom_sql,
            "profile": d.profile_json,
            "routing": d.routing_json,
            "created_at": d.created_at.isoformat(),
        }
        for d in rows
    ]


@router.post("/projects/{project_id}/datasources/upload")
async def upload_datasources(
    project_id: str,
    files: List[UploadFile] = File(...),
    user_goal: Optional[str] = Form(None),
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    loaded_items = []
    created_ids = []
    with tempfile.TemporaryDirectory() as tmp:
        for uf in files:
            safe_name = os.path.basename(uf.filename or "data.csv")
            raw_bytes = await uf.read()
            local_p = os.path.join(tmp, safe_name)
            with open(local_p, "wb") as f:
                f.write(raw_bytes)
            try:
                item = load_file(local_p)
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))
            item["name"] = safe_name
            obj_key, _ = storage.put_bytes(f"projects/{project_id}/uploads/{uuid.uuid4().hex[:8]}_{safe_name}", raw_bytes)
            single_profile = profile_items([item])
            single_routing = recommend_pipeline(single_profile, user_goal=user_goal)
            ds = DataSource(
                project_id=project_id,
                name=safe_name,
                source_type="file",
                object_key=obj_key,
                profile_json=single_profile,
                routing_json=single_routing,
            )
            db.add(ds)
            db.flush()
            created_ids.append(ds.id)
            loaded_items.append(item)

    combined_profile = profile_items(loaded_items)
    combined_routing = recommend_pipeline(combined_profile, user_goal=user_goal)
    db.commit()
    return {"datasource_ids": created_ids, "profile": combined_profile, "routing": combined_routing}


@router.post("/db/test")
def api_db_test(req: DBConnectRequest, ctx: Dict[str, str] = Depends(get_current_user)):
    try:
        return test_db_connection(req.model_dump())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/projects/{project_id}/datasources/db")
def connect_and_sample_db(
    project_id: str,
    req: DBConnectRequest,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    if not req.table_name and not req.custom_sql:
        raise HTTPException(status_code=400, detail="请指定表名或自定义只读 SELECT 查询。")
    cfg = req.model_dump(include={"db_type", "host", "port", "user", "password", "database", "uri"})
    try:
        sample_df = read_db_dataframe(cfg, table_name=req.table_name or "", custom_sql=req.custom_sql, limit=SAFE_PROFILE_SAMPLE_LIMIT)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    label = req.name or f"{req.db_type}:{req.table_name or 'custom_sql'}"
    profile = profile_items([{"kind": "table", "name": label, "df": sample_df}])
    routing = recommend_pipeline(profile, user_goal=req.user_goal)

    ds = DataSource(
        project_id=project_id,
        name=label,
        source_type=req.db_type.lower(),
        encrypted_config=encrypt_secret(json.dumps(cfg, ensure_ascii=False)),
        table_name=req.table_name or "",
        custom_sql=req.custom_sql or "",
        profile_json=profile,
        routing_json=routing,
    )
    db.add(ds)
    db.commit()
    return {"datasource_ids": [ds.id], "sampled_rows": len(sample_df), "profile": profile, "routing": routing}


@router.post("/projects/{project_id}/datasources/demo/{stage}")
def load_demo_datasource(
    project_id: str,
    stage: str,
    ctx: Dict[str, str] = Depends(require_role("admin", "editor")),
    db: Session = Depends(get_db),
):
    st = stage.upper()
    items = []
    ds_ids = []
    if st == "L1":
        df = build_demo_dirty_suppliers(250)
        obj_key, _ = storage.put_bytes(f"projects/{project_id}/uploads/demo_dirty_suppliers.csv", df.to_csv(index=False).encode("utf-8-sig"))
        items = [{"kind": "table", "name": "dirty_suppliers.csv (示例250行)", "df": df}]
        prof = profile_items(items)
        rt = recommend_pipeline(prof)
        rt["demo_catalog_key"] = "supplier_master_demo_v1"
        ds = DataSource(project_id=project_id, name=items[0]["name"], source_type="file", object_key=obj_key, profile_json=prof, routing_json=rt)
        db.add(ds)
        db.flush()
        ds_ids.append(ds.id)
    elif st == "L2":
        df_erp, df_wms = build_demo_cross_db_tables(20_000, 15_000)
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = os.path.join(tmp, "erp.parquet"), os.path.join(tmp, "wms.parquet")
            df_erp.to_parquet(p1, index=False)
            df_wms.to_parquet(p2, index=False)
            k1, _ = storage.put_file(f"projects/{project_id}/uploads/demo_erp.parquet", p1)
            k2, _ = storage.put_file(f"projects/{project_id}/uploads/demo_wms.parquet", p2)
        items = [{"kind": "table", "name": "demo_erp.parquet", "df": df_erp}, {"kind": "table", "name": "demo_wms.parquet", "df": df_wms}]
        prof = profile_items(items)
        rt = recommend_pipeline(prof)
        for nm, k, d in [("demo_erp.parquet", k1, df_erp), ("demo_wms.parquet", k2, df_wms)]:
            ds = DataSource(project_id=project_id, name=nm, source_type="file", object_key=k, profile_json=profile_items([{"kind": "table", "name": nm, "df": d}]), routing_json=rt)
            db.add(ds)
            db.flush()
            ds_ids.append(ds.id)
    elif st == "L3":
        obj_key, _ = storage.put_bytes(f"projects/{project_id}/uploads/demo_schema.tsv", DEFAULT_SCHEMASHEET_TSV.encode("utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "demo_schema.tsv")
            open(p, "w", encoding="utf-8").write(DEFAULT_SCHEMASHEET_TSV)
            items = [load_file(p)]
        prof = profile_items(items)
        rt = recommend_pipeline(prof)
        ds = DataSource(project_id=project_id, name="demo_schema.tsv", source_type="file", object_key=obj_key, profile_json=prof, routing_json=rt)
        db.add(ds)
        db.flush()
        ds_ids.append(ds.id)
    elif st == "L4":
        obj_key, _ = storage.put_bytes(f"projects/{project_id}/uploads/demo_metrics.yaml", DEFAULT_METRICS_YAML.encode("utf-8"))
        items = [{"kind": "yaml", "name": "demo_metrics.yaml", "text": DEFAULT_METRICS_YAML}]
        prof = profile_items(items)
        rt = recommend_pipeline(prof)
        ds = DataSource(project_id=project_id, name="demo_metrics.yaml", source_type="file", object_key=obj_key, profile_json=prof, routing_json=rt)
        db.add(ds)
        db.flush()
        ds_ids.append(ds.id)
    else:
        raise HTTPException(status_code=400, detail=f"未知阶段: {stage}")

    db.commit()
    return {"datasource_ids": ds_ids, "profile": prof, "routing": rt}
