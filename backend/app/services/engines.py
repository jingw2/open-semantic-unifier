"""生产级独立 L1–L4 语义统一执行引擎（无外部路径依赖，Docker 容器独立可运行）

特性：
1. L1/L2 自动优先命中项目级 CanonicalMapping 人工沉淀字典，并将未命中/灰色样本推送至 ReviewItem 待复核队列
2. L3/L4 编译产物自动上传至 MinIO/Local 对象存储，写入递增版本号的 ArtifactVersion（默认 DRAFT）
3. 为对外 Serving API 提供 reconcile / validate / query_metric 核心能力
"""

import json
import math
import os
import random
import re
import subprocess
import tempfile
import time
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb
import jsonschema
import numpy as np
import pandas as pd
import yaml
from sqlalchemy.orm import Session

from app.models.entities import ArtifactVersion, CanonicalMapping, ReviewItem
from app.demo.catalogs import MASTER_SUPPLIERS
from app.services.llm_client import call_llm_json
from app.services.storage import storage

# ==================== 内置默认主数据、词表与指标模板 ====================


DEFAULT_SCHEMASHEET_TSV = """class\tslot\trange\tmultivalued\trequired\tidentifier\tenum\tpermissible_value\tdescription
>class\tslot\trange\tmultivalued\trequired\tidentifier\tenum\tpermissible_value\tdescription
SalesOrder\torder_id\tstring\tfalse\ttrue\ttrue\t\t\t全局唯一销售订单号
SalesOrder\tcustomer_id\tstring\tfalse\ttrue\tfalse\t\t\t全局标准客户编码（cluster_id）
SalesOrder\torder_channel\tOrderChannelEnum\tfalse\ttrue\tfalse\t\t\t标准销售渠道枚举
SalesOrder\ttotal_amount\tfloat\tfalse\ttrue\tfalse\t\t\t订单含税总金额（CNY）
SalesOrder\tcurrency\tstring\tfalse\ttrue\tfalse\t\t\tISO 4217 标准币种
ShipmentOrder\tshipment_id\tstring\tfalse\ttrue\ttrue\t\t\t全局唯一物流履约单号
ShipmentOrder\torder_id\tSalesOrder\tfalse\ttrue\tfalse\t\t\t外键关联销售订单
ShipmentOrder\tcarrier_code\tstring\tfalse\ttrue\tfalse\t\t\t标准承运商编码（如 SUP-1003）
ShipmentOrder\tdelivery_status\tDeliveryStatusEnum\tfalse\ttrue\tfalse\t\t\t统一物流履约状态枚举
ShipmentOrder\tsigned_at\tdatetime\tfalse\tfalse\tfalse\t\t\t客户实际签收时间戳
SettlementBill\tbill_id\tstring\tfalse\ttrue\ttrue\t\t\t全局唯一财务结算单号
SettlementBill\tshipment_id\tShipmentOrder\tfalse\ttrue\tfalse\t\t\t外键关联物流履约单
SettlementBill\tsettled_amount\tfloat\tfalse\ttrue\tfalse\t\t\t财务实际核销结算金额
SettlementBill\tsettlement_status\tSettlementStatusEnum\tfalse\ttrue\tfalse\t\t\t统一财务对账结算状态
\t\t\t\t\t\tOrderChannelEnum\tDOMESTIC_B2B\t国内经销渠道
\t\t\t\t\t\tOrderChannelEnum\tCROSS_BORDER_DTC\t跨境直邮零售
\t\t\t\t\t\tOrderChannelEnum\tKA_DIRECT\t大客户直供
\t\t\t\t\t\tDeliveryStatusEnum\tPENDING_PICK\t待拣货出库
\t\t\t\t\t\tDeliveryStatusEnum\tIN_TRANSIT\t干线与末端在途（替代 SHIPPED / ON_WAY）
\t\t\t\t\t\tDeliveryStatusEnum\tDELIVERED\t客户已签收
\t\t\t\t\t\tDeliveryStatusEnum\tEXCEPTION\t异常滞留或拒收件
\t\t\t\t\t\tSettlementStatusEnum\tUNBILLED\t未出账单
\t\t\t\t\t\tSettlementStatusEnum\tRECONCILED\t业财已对账一致
\t\t\t\t\t\tSettlementStatusEnum\tSETTLED\t财务已打款核销
"""

DEFAULT_BASE_SCHEMA_YAML = """id: https://lingdianweilai.ai/schemas/enterprise_order
name: enterprise_order_model
prefixes:
  linkml: https://w3id.org/linkml/
  enterprise_order_model: https://lingdianweilai.ai/schemas/enterprise_order/
default_prefix: enterprise_order_model
imports:
  - linkml:types
default_range: string
"""

DEFAULT_METRICS_YAML = """semantic_model:
  name: order_fulfillment_finance
  base_table: SalesOrder
  joins:
    - table: ShipmentOrder
      join_on: SalesOrder.order_id = ShipmentOrder.order_id
      type: LEFT JOIN
    - table: SettlementBill
      join_on: ShipmentOrder.shipment_id = SettlementBill.shipment_id
      type: LEFT JOIN

metrics:
  net_gmv:
    label: 净成交额 (Net GMV)
    owner: 经营分析部（运营与财务联合签字）
    description: 剔除物流异常拒收单（EXCEPTION）后的有效订单含税总金额
    sql_expr: SUM(SalesOrder.total_amount)
    where: ShipmentOrder.delivery_status != 'EXCEPTION'

  signed_fulfillment_rate:
    label: 签收履约率
    owner: 供应链物流部
    description: 已签收订单数占有效订单的百分比
    sql_expr: ROUND(100.0 * SUM(CASE WHEN ShipmentOrder.delivery_status = 'DELIVERED' THEN 1 ELSE 0 END) / COUNT(DISTINCT SalesOrder.order_id), 2)
    where: ShipmentOrder.delivery_status != 'EXCEPTION'

  settled_revenue:
    label: 财务实收核销额
    owner: 财务部
    description: 财务已正式打款核销（SETTLED）的实际回款金额
    sql_expr: COALESCE(SUM(CASE WHEN SettlementBill.settlement_status = 'SETTLED' THEN SettlementBill.settled_amount ELSE 0 END), 0)
    where: 1 = 1
"""

UNSTRUCTURED_HARD_CASES = [
    {"case_type": "互联网简称/花名", "left_text": "深圳市腾讯计算机系统有限公司", "right_text": "鹅厂（Tencent Holdings）"},
    {"case_type": "工业物料国标 vs 德标", "left_text": "304不锈钢外六角螺栓 M8*20", "right_text": "DIN933 M8x20 A2-70 Hex Bolt"},
    {"case_type": "跨语言 + 业务缩写", "left_text": "极光跨境物流集团（华南仓）", "right_text": "Aurora Cross-Border Logistics SZ Hub"},
    {"case_type": "高相似度假阳性陷阱", "left_text": "304不锈钢外六角螺栓 M8*20", "right_text": "316不锈钢内六角螺钉 M8*20"},
]

_OFFLINE_KNOWLEDGE = {
    ("深圳市腾讯计算机系统有限公司", "鹅厂（Tencent Holdings）"): (True, 0.99, "“鹅厂”是腾讯在国内通用别称，Tencent Holdings 为其英文集团名，指向同一实体。"),
    ("304不锈钢外六角螺栓 M8*20", "DIN933 M8x20 A2-70 Hex Bolt"): (True, 0.97, "工业标准完全等价：DIN933 = 外六角全螺纹螺栓，A2-70 = 304不锈钢，规格同为 M8x20。"),
    ("极光跨境物流集团（华南仓）", "Aurora Cross-Border Logistics SZ Hub"): (True, 0.95, "Aurora 对应极光，Cross-Border Logistics 对应跨境物流，SZ Hub 对应华南仓。"),
    ("304不锈钢外六角螺栓 M8*20", "316不锈钢内六角螺钉 M8*20"): (False, 0.98, "虽尺寸同为 M8*20，但材质（304 vs 316）与头型（外六角 vs 内六角）不同，不可合并。"),
}

_STOP_TOKENS = ("股份有限公司", "有限责任公司", "科技有限公司", "有限公司", "集团", "深圳市", "上海市", "东莞", "苏州", "武汉")


def fingerprint_key(text: str) -> str:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", str(text).strip().lower())
    return " ".join(sorted(set(cleaned.split())))


def _calc_supplier_score(query_text: str, item: Dict[str, Any]) -> float:
    q_raw = query_text.strip().lower()
    q_fp = fingerprint_key(query_text)
    scores = [SequenceMatcher(None, q_raw, item["name"].lower()).ratio()]
    for alias in item.get("aliases", []):
        if q_fp and q_fp == fingerprint_key(alias):
            scores.append(0.97)
        else:
            scores.append(SequenceMatcher(None, q_raw, alias.lower()).ratio() * 0.94)
    return round(max(scores) * 100, 1)


def _cosine_ngram(a: str, b: str) -> float:
    def vec(s: str) -> Counter:
        t = re.sub(r"\s+", "", s.lower())
        return Counter([t[i : i + 2] for i in range(max(len(t) - 1, 1))])

    va, vb = vec(a), vec(b)
    dot = sum(va[k] * vb[k] for k in set(va) & set(vb))
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return round(dot / (na * nb), 3) if na and nb else 0.0


def _next_version_no(db: Session, project_id: str, artifact_type: str) -> int:
    last = (
        db.query(ArtifactVersion)
        .filter(ArtifactVersion.project_id == project_id, ArtifactVersion.artifact_type == artifact_type)
        .order_by(ArtifactVersion.version_no.desc())
        .first()
    )
    return (last.version_no + 1) if last else 1


def _save_artifact(
    db: Session,
    project_id: str,
    job_id: str,
    artifact_type: str,
    filename: str,
    data: bytes,
    meta: Optional[Dict[str, Any]] = None,
) -> ArtifactVersion:
    ver = _next_version_no(db, project_id, artifact_type)
    obj_key = f"projects/{project_id}/{artifact_type}/v{ver}_{filename}"
    key, size = storage.put_bytes(obj_key, data)
    art = ArtifactVersion(
        project_id=project_id,
        job_id=job_id,
        version_no=ver,
        artifact_type=artifact_type,
        release_status="DRAFT",
        filename=filename,
        object_key=key,
        size_bytes=size,
        meta_json=meta or {},
    )
    db.add(art)
    db.flush()
    return art


# ==================== 内置仿真数据生成器 ====================

def build_demo_dirty_suppliers(n_rows: int = 250, seed: int = 42) -> pd.DataFrame:
    random.seed(seed)
    systems = ["ERP", "WMS", "OMS", "TMS"]
    rows = []
    weights = [4, 3, 3] + [1] * (len(MASTER_SUPPLIERS) - 3)
    for i in range(1, n_rows + 1):
        item = random.choices(MASTER_SUPPLIERS, weights=weights, k=1)[0]
        raw = item["name"] if random.random() < 0.18 else random.choice(item["aliases"])
        if random.random() < 0.15 and not raw.startswith(" "):
            raw = f"  {raw}  "
        rows.append(
            {
                "row_id": f"PO-{10000 + i}",
                "supplier_raw_name": raw,
                "source_system": random.choice(systems),
                "order_amount_cny": round(random.uniform(8000, 320000), 2),
                "ground_truth_id": item["id"],
            }
        )
    return pd.DataFrame(rows)


def build_demo_cross_db_tables(n_erp: int = 25_000, n_wms: int = 18_000, seed: int = 42) -> Tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    cities = np.array(["深圳", "上海", "广州", "苏州", "杭州", "成都", "武汉", "北京"])
    p1 = np.array(["星云", "苍穹", "极光", "瀚海", "青鸾", "玄武", "赤霄", "昆仑", "凌云", "沧澜", "破军", "天枢", "瑶光", "北斗", "麒麟"])
    p2 = np.array(["华", "联", "通", "达", "科", "智", "新", "精", "诚", "信"])
    ind = np.array(["智造", "精密", "跨境物流", "半导体", "智能装备", "自动化", "新能源", "光学"])
    suf = np.array(["科技有限公司", "股份有限公司", "集团有限公司", "实业有限公司"])

    n_ent = max(n_erp // 2, 5000)
    ent_city = rng.choice(cities, size=n_ent)
    ent_core = np.char.add(np.char.add(rng.choice(p1, size=n_ent), rng.choice(p2, size=n_ent)), rng.choice(ind, size=n_ent))
    base_names = np.char.add(ent_core, rng.choice(suf, size=n_ent))
    base_tax = np.char.add("91440300MA", np.char.mod("%08d", np.arange(10_000_000, 10_000_000 + n_ent)))
    base_phone = np.char.add("138", np.char.mod("%08d", rng.integers(10_000_000, 99_999_999, size=n_ent)))

    erp_idx = rng.integers(0, n_ent, size=n_erp)
    df_erp = pd.DataFrame(
        {
            "unique_id": [f"erp_{i}" for i in range(n_erp)],
            "company_name": base_names[erp_idx],
            "tax_id": base_tax[erp_idx],
            "city": ent_city[erp_idx],
            "phone": base_phone[erp_idx],
            "ground_truth_entity": erp_idx,
        }
    )

    wms_idx = rng.integers(0, n_ent, size=n_wms)
    w_names = base_names[wms_idx].copy()
    m_short = rng.random(n_wms) < 0.40
    w_names[m_short] = ent_core[wms_idx][m_short]
    w_tax = base_tax[wms_idx].astype(object)
    w_tax[rng.random(n_wms) < 0.25] = None
    w_phone = base_phone[wms_idx].copy()
    m_np = rng.random(n_wms) < 0.15
    w_phone[m_np] = np.char.add("139", np.char.mod("%08d", rng.integers(10_000_000, 99_999_999, size=int(m_np.sum()))))

    df_wms = pd.DataFrame(
        {
            "unique_id": [f"wms_{i}" for i in range(n_wms)],
            "company_name": w_names,
            "tax_id": w_tax,
            "city": ent_city[wms_idx],
            "phone": w_phone,
            "ground_truth_entity": wms_idx,
        }
    )
    return df_erp, df_wms


# ==================== L1 执行引擎（集成人工沉淀字典 + 推送低分复核队列） ====================

def execute_l1(
    db: Session,
    project_id: str,
    job_id: str,
    df: pd.DataFrame,
    target_col: str,
    amount_col: Optional[str] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
    demo_catalog: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    if progress_cb:
        progress_cb(15, "正在加载项目已沉淀的人工映射字典...")

    learned = {
        m.raw_alias: (m.canonical_id, m.canonical_name)
        for m in db.query(CanonicalMapping).filter(CanonicalMapping.project_id == project_id).all()
    }

    df_work = df.copy()
    raw_series = df_work[target_col].fillna("").astype(str)
    unique_raw = raw_series.unique().tolist()

    fp_groups: Dict[str, List[str]] = {}
    for val in unique_raw:
        fp_groups.setdefault(fingerprint_key(val), []).append(val)

    if progress_cb:
        progress_cb(45, f"正在对 {len(unique_raw)} 种原始名称执行指纹聚类与标准花名册对齐...")

    mapping: Dict[str, Dict[str, Any]] = {}
    review_added = 0
    for val in unique_raw:
        if val in learned:
            cid, cname = learned[val]
            mapping[val] = {"raw": val, "canonical_id": cid, "canonical_name": cname, "score": 100.0, "mode": "人工沉淀字典命中"}
            continue
        best_item, best_score = None, 0.0
        for item in demo_catalog or ():
            sc = _calc_supplier_score(val, item)
            if sc > best_score:
                best_score, best_item = sc, item
        if best_item and best_score >= 65.0:
            mapping[val] = {"raw": val, "canonical_id": best_item["id"], "canonical_name": best_item["name"], "score": best_score, "mode": "W3C标准花名册"}
            # 低置信度边界项（65~85）推入人工复核队列
            if best_score < 85.0 and review_added < 10:
                db.add(
                    ReviewItem(
                        project_id=project_id,
                        job_id=job_id,
                        stage="L1",
                        case_type="L1低置信度对齐",
                        left_text=val,
                        right_text=best_item["name"],
                        score=round(best_score / 100.0, 3),
                        llm_suggestion={"recommended_id": best_item["id"], "recommended_name": best_item["name"], "reason": f"模糊匹配打分 {best_score}，建议业务员确认。"},
                    )
                )
                review_added += 1
        else:
            fp = fingerprint_key(val)
            rep = sorted(fp_groups[fp], key=lambda x: (-len(x.strip()), x.strip()))[0].strip()
            cid = f"ENT-{abs(hash(rep)) % 9000 + 1000}"
            mapping[val] = {"raw": val, "canonical_id": cid, "canonical_name": rep, "score": 78.0, "mode": "指纹聚类"}
            if review_added < 10:
                db.add(
                    ReviewItem(
                        project_id=project_id,
                        job_id=job_id,
                        stage="L1",
                        case_type="L1未命中花名册",
                        left_text=val,
                        right_text=rep,
                        score=0.78,
                        llm_suggestion={"recommended_id": cid, "recommended_name": rep, "reason": "未在内置花名册命中，按指纹代表名暂定，请复核确认。"},
                    )
                )
                review_added += 1

    df_work["canonical_id"] = raw_series.map(lambda x: mapping[x]["canonical_id"])
    df_work["canonical_name"] = raw_series.map(lambda x: mapping[x]["canonical_name"])
    df_work["reconcile_score"] = raw_series.map(lambda x: mapping[x]["score"])

    if progress_cb:
        progress_cb(80, "正在上传清洗产物与可复用 JSON 规则至对象存储...")

    csv_art = _save_artifact(db, project_id, job_id, "clean_csv", "l1_cleaned.csv", df_work.to_csv(index=False).encode("utf-8-sig"))
    rules_art = _save_artifact(db, project_id, job_id, "rules_json", "l1_rules.json", json.dumps(list(mapping.values()), ensure_ascii=False, indent=2).encode("utf-8"))

    if amount_col and amount_col in df_work.columns and pd.api.types.is_numeric_dtype(df_work[amount_col]):
        before_top = df_work.groupby(target_col, as_index=False).agg(count=(target_col, "count"), total=(amount_col, "sum")).sort_values("total", ascending=False).head(8).to_dict(orient="records")
        after_top = df_work.groupby(["canonical_id", "canonical_name"], as_index=False).agg(count=(target_col, "count"), total=(amount_col, "sum")).sort_values("total", ascending=False).head(8).to_dict(orient="records")
    else:
        before_top = df_work.groupby(target_col, as_index=False).size().rename(columns={"size": "count"}).sort_values("count", ascending=False).head(8).to_dict(orient="records")
        after_top = df_work.groupby(["canonical_id", "canonical_name"], as_index=False).size().rename(columns={"size": "count"}).sort_values("count", ascending=False).head(8).to_dict(orient="records")

    return {
        "stage": "L1",
        "total_rows": len(df_work),
        "before_unique": len(unique_raw),
        "after_unique": int(df_work["canonical_name"].nunique()),
        "review_queued": review_added,
        "before_top": before_top,
        "after_top": after_top,
        "artifacts": [{"id": csv_art.id, "type": csv_art.artifact_type, "filename": csv_art.filename, "version": csv_art.version_no}, {"id": rules_art.id, "type": rules_art.artifact_type, "filename": rules_art.filename, "version": rules_art.version_no}],
    }


# ==================== L2 执行引擎（Splink 5.0 + Hybrid ER + 灰色队列推送） ====================

def execute_l2(
    db: Session,
    project_id: str,
    job_id: str,
    dfs: List[pd.DataFrame],
    params: Dict[str, Any],
    llm_cfg: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on
    import splink.comparison_library as cl

    t0 = time.time()
    if progress_cb:
        progress_cb(15, "正在准备 DuckDB 表并构建 Splink 5.0 阻塞分桶规则...")

    prepared = []
    for idx, raw_df in enumerate(dfs):
        d = raw_df.copy()
        if "unique_id" not in d.columns:
            d.insert(0, "unique_id", [f"src{idx}_{i}" for i in range(len(d))])
        if "ground_truth_entity" not in d.columns:
            d["ground_truth_entity"] = -1
        prepared.append(d)

    cols = set(prepared[0].columns)
    name_col = params.get("name_col") if params.get("name_col") in cols else ("company_name" if "company_name" in cols else None)
    tax_col = params.get("tax_col") if params.get("tax_col") in cols else ("tax_id" if "tax_id" in cols else None)
    phone_col = params.get("phone_col") if params.get("phone_col") in cols else ("phone" if "phone" in cols else None)
    city_col = params.get("city_col") if params.get("city_col") in cols else ("city" if "city" in cols else None)

    blocking_rules, comparisons = [], []
    if city_col and phone_col:
        blocking_rules.append(block_on(city_col, phone_col))
    elif phone_col:
        blocking_rules.append(block_on(phone_col))
    if tax_col:
        blocking_rules.append(block_on(tax_col))
    if not blocking_rules and name_col:
        blocking_rules.append(block_on(name_col))

    if name_col:
        comparisons.append(cl.LevenshteinAtThresholds(name_col, [2, 4]))
    if tax_col:
        comparisons.append(cl.ExactMatch(tax_col).configure(term_frequency_adjustments=True))
    if phone_col:
        comparisons.append(cl.ExactMatch(phone_col))
    if city_col:
        comparisons.append(cl.ExactMatch(city_col))

    settings = SettingsCreator(
        link_type="link_and_dedupe" if len(prepared) >= 2 else "dedupe_only",
        blocking_rules_to_generate_predictions=blocking_rules,
        comparisons=comparisons,
        retain_intermediate_calculation_columns=True,
        additional_columns_to_retain=["ground_truth_entity"],
    )

    if progress_cb:
        progress_cb(35, "正在执行 EM 无监督概率训练...")

    db_api = DuckDBAPI()
    sdfs = [db_api.register(d, dataset_display_name=f"t{i}") for i, d in enumerate(prepared)]
    linker = Linker(sdfs if len(sdfs) > 1 else sdfs[0], settings)
    n_total = sum(len(d) for d in prepared)
    linker.training.estimate_u_using_random_sampling(max_pairs=min(5e5, max(5e4, n_total)))
    for br in blocking_rules[:2]:
        try:
            linker.training.estimate_parameters_using_expectation_maximisation(br)
        except Exception:
            pass

    if progress_cb:
        progress_cb(60, "正在执行跨库推理与实体聚类归并...")

    df_pred = linker.inference.predict(threshold_match_probability=0.15)
    pred_pdf = df_pred.as_pandas_dataframe()
    df_clusters = linker.clustering.cluster_pairwise_predictions_at_threshold(df_pred, threshold_match_probability=0.85).as_pandas_dataframe()

    # 导出权重图与 3 类独立瀑布图至对象存储
    artifacts_meta = []
    waterfall_arts = []
    with tempfile.TemporaryDirectory() as tmp:
        w_path = os.path.join(tmp, "weights.html")
        linker.visualisations.match_weights_chart().save(w_path)
        w_art = _save_artifact(db, project_id, job_id, "weights_html", "splink_weights.html", open(w_path, "rb").read())
        artifacts_meta.append({"id": w_art.id, "type": w_art.artifact_type, "filename": w_art.filename, "version": w_art.version_no})

        case_defs = [
            ("案例 1：缺税号 + 简写救回", pred_pdf[(pred_pdf["match_probability"] >= 0.85) & (pred_pdf.get("tax_id_r", pd.Series(dtype=object)).isna() | pred_pdf.get("tax_id_l", pd.Series(dtype=object)).isna())].head(1) if "tax_id_r" in pred_pdf else pred_pdf.head(0)),
            ("案例 2：换手机号靠税号认亲", pred_pdf[(pred_pdf["match_probability"] >= 0.85) & (pred_pdf.get("phone_l") != pred_pdf.get("phone_r"))].head(1) if "phone_l" in pred_pdf else pred_pdf.head(0)),
            ("案例 3：灰色边界拒识样本", pred_pdf[(pred_pdf["match_probability"] >= 0.15) & (pred_pdf["match_probability"] < 0.85)].head(1)),
        ]
        for idx, (title, sub) in enumerate(case_defs, 1):
            if len(sub) == 0:
                continue
            rec = sub.to_dict(orient="records")[0]
            wf_path = os.path.join(tmp, f"wf_{idx}.html")
            linker.visualisations.waterfall_chart([rec], filter_nulls=False).save(wf_path)
            wf_art = _save_artifact(db, project_id, job_id, "waterfall_html", f"splink_waterfall_{idx}.html", open(wf_path, "rb").read(), meta={"title": title})
            waterfall_arts.append({"id": wf_art.id, "title": title})

    if progress_cb:
        progress_cb(82, "正在执行 L2-B 混合漏斗（5% 灰色边界 + 黑话交由 LLM 终审并推送复核队列）...")

    grey_df = pred_pdf[(pred_pdf["match_probability"] >= 0.35) & (pred_pdf["match_probability"] < 0.85)]
    token_saved = round((1 - len(grey_df) / max(len(pred_pdf), 1)) * 100, 2)
    hybrid_verdicts = []
    if params.get("enable_hybrid_llm", True):
        cases = list(UNSTRUCTURED_HARD_CASES)
        for _, row in grey_df.head(2).iterrows():
            cases.append(
                {
                    "case_type": f"Splink灰色样本(p={row['match_probability']:.2f})",
                    "left_text": f"{row.get('company_name_l', '')} | 税号:{row.get('tax_id_l')}",
                    "right_text": f"{row.get('company_name_r', '')} | 税号:{row.get('tax_id_r')}",
                }
            )
        for item in cases:
            sim = _cosine_ngram(item["left_text"], item["right_text"])
            live = None
            try:
                live = call_llm_json(
                    f'判断两条记录是否同一实体，严格返回 JSON {{"is_same_entity": bool, "confidence": 0~1, "reason": "一句话中文理由"}}。\nA: {item["left_text"]}\nB: {item["right_text"]}',
                    llm_cfg,
                )
            except Exception:
                live = None

            if live:
                same, conf, reason, mode = bool(live.get("is_same_entity")), float(live.get("confidence", 0.9)), str(live.get("reason", "")), live.get("_mode", "LIVE")
            elif (item["left_text"], item["right_text"]) in _OFFLINE_KNOWLEDGE:
                same, conf, reason = _OFFLINE_KNOWLEDGE[(item["left_text"], item["right_text"])]
                mode = "离线演示模式"
            else:
                same, conf, reason, mode = sim >= 0.35, round(min(0.95, 0.55 + sim * 0.45), 2), "字号与上下文特征一致，建议合并。" if sim >= 0.35 else "属性存在分歧，建议拆分。", "离线演示模式"

            verdict = {"case_type": item["case_type"], "left": item["left_text"], "right": item["right_text"], "literal_sim": sim, "is_same_entity": same, "confidence": conf, "reason": reason, "mode": mode}
            hybrid_verdicts.append(verdict)
            # 推送至人工复核队列，等待业务员一键拍板沉淀规则
            db.add(
                ReviewItem(
                    project_id=project_id,
                    job_id=job_id,
                    stage="L2",
                    case_type=item["case_type"],
                    left_text=item["left_text"],
                    right_text=item["right_text"],
                    score=conf,
                    llm_suggestion=verdict,
                )
            )

    cl_art = _save_artifact(db, project_id, job_id, "clusters_csv", "l2_clusters_sample.csv", df_clusters.head(2000).to_csv(index=False).encode("utf-8-sig"))
    artifacts_meta.append({"id": cl_art.id, "type": cl_art.artifact_type, "filename": cl_art.filename, "version": cl_art.version_no})

    naive_pairs = n_total * (n_total - 1) // 2
    return {
        "stage": "L2",
        "elapsed_sec": round(time.time() - t0, 2),
        "total_rows": n_total,
        "evaluated_pairs": len(pred_pdf),
        "reduction_pct": round((1 - len(pred_pdf) / max(naive_pairs, 1)) * 100, 4),
        "unique_clusters": int(df_clusters["cluster_id"].nunique()),
        "token_cost_saved_pct": token_saved,
        "weights_artifact_id": w_art.id,
        "waterfall_artifacts": waterfall_arts,
        "hybrid_verdicts": hybrid_verdicts,
        "review_queued": len(hybrid_verdicts),
        "artifacts": artifacts_meta,
    }


# ==================== L3 执行引擎（LinkML 编译 + 版本化 DRAFT 契约） ====================

def _seed_demo_duckdb(sql_text: str, db_path: str) -> None:
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)
    for stmt in sql_text.split(";"):
        clean = "\n".join(ln for ln in stmt.splitlines() if not ln.strip().startswith("--")).strip()
        if clean:
            con.execute(clean)
    con.executemany(
        "INSERT INTO SalesOrder VALUES (?, ?, ?, ?, ?)",
        [
            ("SO-202601", "CLU-1001", "CROSS_BORDER_DTC", 180000.0, "CNY"),
            ("SO-202602", "CLU-1002", "CROSS_BORDER_DTC", 150000.0, "CNY"),
            ("SO-202603", "CLU-1003", "CROSS_BORDER_DTC", 220000.0, "CNY"),
            ("SO-202604", "CLU-1004", "CROSS_BORDER_DTC", 120000.0, "CNY"),
            ("SO-202605", "CLU-1005", "CROSS_BORDER_DTC", 200000.0, "CNY"),
            ("SO-202606", "CLU-1006", "CROSS_BORDER_DTC", 130000.0, "CNY"),
            ("SO-202607", "CLU-1001", "DOMESTIC_B2B", 320000.0, "CNY"),
            ("SO-202608", "CLU-1002", "KA_DIRECT", 450000.0, "CNY"),
        ],
    )
    con.executemany(
        "INSERT INTO ShipmentOrder VALUES (?, ?, ?, ?, ?)",
        [
            ("SHP-9001", "SO-202601", "SUP-1003", "IN_TRANSIT", None),
            ("SHP-9002", "SO-202602", "SUP-1003", "IN_TRANSIT", None),
            ("SHP-9003", "SO-202603", "SUP-1003", "IN_TRANSIT", None),
            ("SHP-9004", "SO-202604", "SUP-1003", "EXCEPTION", None),
            ("SHP-9005", "SO-202605", "SUP-1003", "DELIVERED", "2026-11-10T14:20:00"),
            ("SHP-9006", "SO-202606", "SUP-1003", "DELIVERED", "2026-11-12T09:15:00"),
            ("SHP-9007", "SO-202607", "SUP-1010", "DELIVERED", "2026-11-08T16:40:00"),
            ("SHP-9008", "SO-202608", "SUP-1003", "DELIVERED", "2026-11-05T11:30:00"),
        ],
    )
    con.executemany(
        "INSERT INTO SettlementBill VALUES (?, ?, ?, ?)",
        [
            ("BIL-7001", "SHP-9005", 200000.0, "SETTLED"),
            ("BIL-7002", "SHP-9006", 130000.0, "RECONCILED"),
            ("BIL-7003", "SHP-9007", 320000.0, "SETTLED"),
            ("BIL-7004", "SHP-9008", 450000.0, "SETTLED"),
        ],
    )
    con.close()


def execute_l3(
    db: Session,
    project_id: str,
    job_id: str,
    tsv_text: Optional[str] = None,
    progress_cb: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    """Legacy L4 demo seed. The public L3 workflow uses app.services.contracts."""
    if progress_cb:
        progress_cb(25, "正在调用 LinkML 编译 YAML 本体、SQL DDL、JSON Schema 与 ER 图...")

    with tempfile.TemporaryDirectory() as tmp:
        tsv_path = os.path.join(tmp, "schema.tsv")
        base_path = os.path.join(tmp, "base.yaml")
        yaml_path = os.path.join(tmp, "model.yaml")
        sql_path = os.path.join(tmp, "schema.sql")
        json_path = os.path.join(tmp, "schema.json")
        er_path = os.path.join(tmp, "er.md")

        with open(tsv_path, "w", encoding="utf-8") as f:
            f.write(tsv_text if tsv_text and tsv_text.strip() else DEFAULT_SCHEMASHEET_TSV)
        with open(base_path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_BASE_SCHEMA_YAML)

        subprocess.run(["sheets2linkml", "--base-schema-path", base_path, tsv_path, "-o", yaml_path], check=True)
        with open(sql_path, "w", encoding="utf-8") as f:
            subprocess.run(["gen-sqlddl", yaml_path], stdout=f, check=True)
        with open(json_path, "w", encoding="utf-8") as f:
            subprocess.run(["gen-json-schema", yaml_path], stdout=f, check=True)
        with open(er_path, "w", encoding="utf-8") as f:
            subprocess.run(["gen-erdiagram", yaml_path], stdout=f, check=True)

        sql_text = open(sql_path, encoding="utf-8").read()
        schema_text = open(json_path, encoding="utf-8").read()
        er_text = open(er_path, encoding="utf-8").read()
        yaml_text = open(yaml_path, encoding="utf-8").read()

        if progress_cb:
            progress_cb(75, "正在写入版本化契约产物并生成仿真 DuckDB 闭环验证...")

        # 保存核心可发布契约产物 l3_contract（包含完整 JSON Schema，用于发布到线上 Serving API）
        contract_art = _save_artifact(db, project_id, job_id, "l3_contract", "schema.json", schema_text.encode("utf-8"))
        sql_art = _save_artifact(db, project_id, job_id, "sql_ddl", "schema.sql", sql_text.encode("utf-8"))
        er_art = _save_artifact(db, project_id, job_id, "er_diagram", "er_diagram.md", er_text.encode("utf-8"))
        yaml_art = _save_artifact(db, project_id, job_id, "linkml_yaml", "enterprise_model.yaml", yaml_text.encode("utf-8"))

        comparison = None
        if "SalesOrder" in sql_text and "ShipmentOrder" in sql_text and "SettlementBill" in sql_text:
            duck_path = os.path.join(tmp, "unified.duckdb")
            _seed_demo_duckdb(sql_text, duck_path)
            storage.put_file(f"projects/{project_id}/duckdb/unified.duckdb", duck_path)
            con = duckdb.connect(duck_path, read_only=True)
            q = "SELECT s.shipment_id, o.order_id, o.order_channel, s.carrier_code, s.delivery_status, o.total_amount FROM ShipmentOrder s JOIN SalesOrder o ON s.order_id=o.order_id WHERE o.order_channel=? AND s.delivery_status=?"
            bad = con.execute(q, ["CB_WEB", "SHIPPED"]).df().to_dict(orient="records")
            good = con.execute(q, ["CROSS_BORDER_DTC", "IN_TRANSIT"]).df()
            con.close()
            comparison = {
                "unconstrained": {"args": {"order_channel": "CB_WEB", "delivery_status": "SHIPPED"}, "hit_count": len(bad)},
                "constrained": {"args": {"order_channel": "CROSS_BORDER_DTC", "delivery_status": "IN_TRANSIT"}, "hit_count": len(good), "total_amount": float(good["total_amount"].sum()), "rows": good.to_dict(orient="records")},
            }

    return {
        "stage": "L3",
        "contract_artifact_id": contract_art.id,
        "contract_version": contract_art.version_no,
        "sql_text": sql_text,
        "json_schema_text": schema_text,
        "er_text": er_text,
        "yaml_text": yaml_text,
        "comparison": comparison,
        "artifacts": [
            {"id": contract_art.id, "type": contract_art.artifact_type, "filename": contract_art.filename, "version": contract_art.version_no, "release_status": contract_art.release_status},
            {"id": sql_art.id, "type": sql_art.artifact_type, "filename": sql_art.filename, "version": sql_art.version_no},
            {"id": er_art.id, "type": er_art.artifact_type, "filename": er_art.filename, "version": er_art.version_no},
            {"id": yaml_art.id, "type": yaml_art.artifact_type, "filename": yaml_art.filename, "version": yaml_art.version_no},
        ],
    }


def validate_with_schema_text(schema_text: str, entity_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    full = json.loads(schema_text)
    sub = {"$schema": full.get("$schema", "https://json-schema.org/draft/2019-09/schema"), "$defs": full.get("$defs", {}), "$ref": f"#/$defs/{entity_name}"}
    try:
        jsonschema.validate(instance=payload, schema=sub)
        return {"valid": True, "message": f"✅ 校验通过！数据完全符合 {entity_name} 标准契约。"}
    except jsonschema.ValidationError as e:
        return {"valid": False, "message": f"❌ 契约拦截！{e.message}"}


# ==================== L4 执行引擎（YAML 指标语义层 + 版本化 DRAFT 指标） ====================


L4_TABLE_COLUMNS = {
    "SalesOrder": ("order_id", "order_channel", "total_amount"),
    "ShipmentOrder": ("shipment_id", "order_id", "delivery_status"),
    "SettlementBill": ("bill_id", "shipment_id", "settled_amount", "settlement_status"),
}
L4_WIDE_COLUMNS = ("order_id", "order_channel", "total_amount", "delivery_status", "settled_amount")


def _wide_l4_tables(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    order_id = df["order_id"].astype(str)
    shipment_id = df["shipment_id"].astype(str) if "shipment_id" in df.columns else order_id
    carrier = df["carrier_code"].astype(str) if "carrier_code" in df.columns else pd.Series([""] * len(df), index=df.index)
    bill_id = df["bill_id"].astype(str) if "bill_id" in df.columns else order_id
    settlement_status = (
        df["settlement_status"].astype(str) if "settlement_status" in df.columns
        else pd.Series(["SETTLED"] * len(df), index=df.index)
    )
    return {
        "SalesOrder": pd.DataFrame({
            "order_id": order_id,
            "order_channel": df["order_channel"].astype(str),
            "total_amount": pd.to_numeric(df["total_amount"], errors="coerce"),
        }),
        "ShipmentOrder": pd.DataFrame({
            "shipment_id": shipment_id,
            "order_id": order_id,
            "carrier_code": carrier,
            "delivery_status": df["delivery_status"].astype(str),
        }),
        "SettlementBill": pd.DataFrame({
            "bill_id": bill_id,
            "shipment_id": shipment_id,
            "settled_amount": pd.to_numeric(df["settled_amount"], errors="coerce"),
            "settlement_status": settlement_status,
        }),
    }


def build_l4_tables(dfs: List[pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    """Map selected tables onto the metric model, or explain exactly what is missing."""
    for df in dfs:
        if set(L4_WIDE_COLUMNS) <= set(df.columns):
            return _wide_l4_tables(df)
    tables: Dict[str, pd.DataFrame] = {}
    for df in dfs:
        for role, required in L4_TABLE_COLUMNS.items():
            if role not in tables and set(required) <= set(df.columns):
                frame = df.copy()
                if role == "SalesOrder":
                    frame["total_amount"] = pd.to_numeric(frame["total_amount"], errors="coerce")
                elif role == "SettlementBill":
                    frame["settled_amount"] = pd.to_numeric(frame["settled_amount"], errors="coerce")
                tables[role] = frame
                break
    missing = [role for role in L4_TABLE_COLUMNS if role not in tables]
    if missing:
        raise ValueError(
            "所选数据源不包含 L4 指标所需的订单/物流/结算字段，缺少表："
            + "、".join(missing)
            + "。请选择三张标准表，或一张同时包含 "
            + "、".join(L4_WIDE_COLUMNS)
            + " 的宽表；也可以加载 L4 示例。"
        )
    return tables


def compile_and_query_metric_yaml(yaml_text: str, db_path: str, metric_name: str, group_by: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    spec = yaml.safe_load(yaml_text)
    metrics = spec.get("metrics", {})
    if metric_name not in metrics:
        raise ValueError(f"未注册的标准指标 '{metric_name}'，可用指标: {list(metrics.keys())}")
    sm = spec["semantic_model"]
    m = metrics[metric_name]
    joins_sql = "\n".join(f"{j['type']} {j['table']} ON {j.get('join_on') or j.get('on')}" for j in sm.get("joins", []))
    if group_by:
        sql = f"SELECT {sm['base_table']}.{group_by} AS {group_by}, {m['sql_expr']} AS {metric_name}\nFROM {sm['base_table']}\n{joins_sql}\nWHERE {m['where']}\nGROUP BY {sm['base_table']}.{group_by}\nORDER BY {metric_name} DESC"
    else:
        sql = f"SELECT {m['sql_expr']} AS {metric_name}\nFROM {sm['base_table']}\n{joins_sql}\nWHERE {m['where']}"
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(sql).df().to_dict(orient="records")
    con.close()
    return sql, rows, m


def execute_l4(
    db: Session,
    project_id: str,
    job_id: str,
    yaml_text: Optional[str] = None,
    metric_name: str = "net_gmv",
    group_by: Optional[str] = "order_channel",
    progress_cb: Optional[Callable[[int, str], None]] = None,
    tables: Optional[Dict[str, pd.DataFrame]] = None,
    data_kind: str = "demo",
) -> Dict[str, Any]:
    if progress_cb:
        progress_cb(30, "正在准备标准业务库并编译 YAML 指标语义层...")

    final_yaml = yaml_text if yaml_text and yaml_text.strip() else DEFAULT_METRICS_YAML
    duck_key = f"projects/{project_id}/duckdb/unified.duckdb"
    with tempfile.TemporaryDirectory() as tmp:
        duck_path = os.path.join(tmp, "unified.duckdb")
        if tables is not None:
            con = duckdb.connect(duck_path)
            for role, frame in tables.items():
                con.register(f"{role}_source", frame)
                con.execute(f"CREATE TABLE {role} AS SELECT * FROM {role}_source")
            con.close()
        else:
            try:
                storage.materialize_to_local(duck_key, duck_path)
            except Exception:
                # 尚未跑过 L3 时自动初始化示范库
                execute_l3(db, project_id, job_id)
                storage.materialize_to_local(duck_key, duck_path)

        con = duckdb.connect(duck_path, read_only=True)
        op_val = con.execute("SELECT SUM(total_amount) FROM SalesOrder WHERE order_channel='CROSS_BORDER_DTC'").fetchone()[0]
        log_val = con.execute("SELECT SUM(o.total_amount) FROM SalesOrder o JOIN ShipmentOrder s ON o.order_id=s.order_id WHERE o.order_channel='CROSS_BORDER_DTC' AND s.delivery_status!='EXCEPTION'").fetchone()[0]
        fin_val = con.execute("SELECT COALESCE(SUM(b.settled_amount),0) FROM SalesOrder o JOIN ShipmentOrder s ON o.order_id=s.order_id JOIN SettlementBill b ON s.shipment_id=b.shipment_id WHERE o.order_channel='CROSS_BORDER_DTC' AND b.settlement_status='SETTLED'").fetchone()[0]
        con.close()

        sql, rows, meta = compile_and_query_metric_yaml(final_yaml, duck_path, metric_name=metric_name, group_by=group_by)

    art = _save_artifact(db, project_id, job_id, "l4_metrics", "semantic_metrics.yaml", final_yaml.encode("utf-8"))
    return {
        "stage": "L4",
        "metrics_artifact_id": art.id,
        "metrics_version": art.version_no,
        "selected_metric": metric_name,
        "metric_meta": meta,
        "data_source_kind": data_kind,
        "compiled_sql": sql,
        "result_rows": rows,
        "conflict_rows": [
            {"部门口径": "运营部门（裸查订单表）", "计算逻辑": "全量订单直接求和（含拒收异常单）", "算出金额": float(op_val)},
            {"部门口径": "物流/经营口径（剔除异常）", "计算逻辑": "排除 EXCEPTION 异常单", "算出金额": float(log_val)},
            {"部门口径": "财务部门（只认已核销）", "计算逻辑": "仅统计 SETTLED 实收款", "算出金额": float(fin_val)},
        ],
        "artifacts": [{"id": art.id, "type": art.artifact_type, "filename": art.filename, "version": art.version_no, "release_status": art.release_status}],
    }
