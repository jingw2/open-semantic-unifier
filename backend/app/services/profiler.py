"""数据体检引擎（Data Profiler）与 Smart Router 智能路由决策器"""

import re
from typing import Any, Dict, List, Optional

import pandas as pd

_JARGON_RE = re.compile(r"(?:DIN\d+|ISO\d+|M\d+[*xX]\d+|A2-70|304|316|鹅厂|Tencent)", re.IGNORECASE)

STAGE_META = {
    "L1": {"title": "L1 · 小表清洗 + 标准花名册对齐", "short": "L1 小表清洗", "badge": "< 5 万行存量表"},
    "L2": {"title": "L2 · DuckDB + Splink 5.0 概率消歧 + 5% LLM 终审", "short": "L2 大表+AI认亲", "badge": "跨库大表 / 黑话"},
    "L3": {"title": "L3 · Schemasheets + LinkML 源头契约与防幻觉", "short": "L3 源头契约", "badge": "建表规范 / 锁枚举"},
    "L4": {"title": "L4 · 声明式 YAML 指标语义层（Metric Layer）", "short": "L4 指标语义层", "badge": "统一 GMV / KPI"},
}


def _detect_keys(columns: List[str]) -> Dict[str, Optional[str]]:
    lower_map = {c.lower(): c for c in columns}

    def find(kws):
        for k in kws:
            for lc, orig in lower_map.items():
                if k in lc:
                    return orig
        return None

    return {
        "name_col": find(["company_name", "supplier_raw_name", "supplier", "customer_name", "name", "公司", "供应商"]),
        "tax_col": find(["tax_id", "tax", "uscc", "credit_code", "税号"]),
        "phone_col": find(["phone", "mobile", "tel", "手机", "电话"]),
        "city_col": find(["city", "region", "城市", "地区"]),
        "amount_col": find(["order_amount", "total_amount", "amount", "gmv", "金额"]),
    }


def profile_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_rows = 0
    tables: List[Dict[str, Any]] = []
    has_tsv, has_yaml, has_jargon, has_ws = False, False, False, False
    keys: Dict[str, Optional[str]] = {"name_col": None, "tax_col": None, "phone_col": None, "city_col": None, "amount_col": None}

    for item in items:
        kind = item["kind"]
        if kind == "yaml":
            txt = item.get("text", "")
            if "metrics:" in txt or "semantic_model:" in txt:
                has_yaml = True
            tables.append({"name": item["name"], "kind": "yaml", "rows": len(txt.splitlines()), "cols": 1, "columns": ["YAML"], "missing_rate": 0.0, "preview": []})
            continue

        df: pd.DataFrame = item["df"]
        if kind == "tsv":
            lc = {c.lower() for c in df.columns}
            if {"class", "slot"}.issubset(lc) or "permissible_value" in lc:
                has_tsv = True

        n_rows, n_cols = int(df.shape[0]), int(df.shape[1])
        total_rows += n_rows
        miss = round(float(df.isna().mean().mean() * 100), 2) if n_rows and n_cols else 0.0
        cur_keys = _detect_keys(list(df.columns))
        for k, v in cur_keys.items():
            if v and not keys[k]:
                keys[k] = v

        text_col = cur_keys["name_col"] or next((c for c in df.columns if df[c].dtype == object), None)
        if text_col and n_rows:
            s = df[text_col].dropna().astype(str).head(2000)
            if len(s):
                has_ws = has_ws or bool((s != s.str.strip()).any() or s.str.contains(r"\s{2,}", regex=True).any())
                has_jargon = has_jargon or bool(s.str.contains(_JARGON_RE).any())

        tables.append(
            {
                "name": item["name"],
                "kind": kind,
                "rows": n_rows,
                "cols": n_cols,
                "columns": list(df.columns),
                "missing_rate": miss,
                "primary_text_col": text_col,
                "preview": df.head(5).fillna("").to_dict(orient="records"),
            }
        )

    return {
        "file_count": len(items),
        "table_count": sum(1 for x in items if x["kind"] in ("table", "tsv")),
        "total_rows": total_rows,
        "has_schemasheet_tsv": has_tsv,
        "has_metric_yaml": has_yaml,
        "has_jargon": has_jargon,
        "has_whitespace_noise": has_ws,
        "detected_keys": keys,
        "tables": tables,
    }


def recommend_pipeline(profile: Dict[str, Any], user_goal: Optional[str] = None) -> Dict[str, Any]:
    reasons: List[str] = []
    goal = (user_goal or "").strip().upper()
    keys = profile.get("detected_keys", {})
    total_rows = profile.get("total_rows", 0)
    table_count = profile.get("table_count", 0)

    if goal in STAGE_META:
        stage, conf = goal, 0.96
        reasons.append(f"已按你指定的业务目标优先锁定【{STAGE_META[stage]['short']}】。")
    elif profile.get("has_metric_yaml"):
        stage, conf = "L4", 0.98
        reasons.append("检测到声明式 YAML 指标定义文件，自动路由至 L4 指标语义层。")
    elif profile.get("has_schemasheet_tsv"):
        stage, conf = "L3", 0.98
        reasons.append("检测到 LinkML Schemasheets 词表结构，自动路由至 L3 源头契约编译。")
    elif table_count >= 2 or total_rows >= 50_000 or (keys.get("tax_col") and keys.get("phone_col")) or profile.get("has_jargon"):
        stage, conf = "L2", 0.94
        if table_count >= 2 or total_rows >= 50_000:
            reasons.append(f"检测到大规模/跨库数据（{table_count} 表，{total_rows:,} 行），自动启用 DuckDB + Splink 5.0。")
        if keys.get("tax_col") or keys.get("phone_col"):
            reasons.append(f"探测到多维消歧键（税号: {keys.get('tax_col')} / 手机: {keys.get('phone_col')}），适合 Blocking 分桶与 EM 无监督训练。")
        if profile.get("has_jargon"):
            reasons.append("探测到工业物料型号或跨语言黑话，自动开启 5% 灰色样本 LLM 终审漏斗。")
    else:
        stage, conf = "L1", 0.91
        reasons.append(f"中小体量单表（{total_rows:,} 行 < 5 万行），适合指纹聚类 + 标准花名册对齐。")
        if profile.get("has_whitespace_noise"):
            reasons.append("检测到空格/大小写/标点噪声，Key Collision 指纹归一化收益极高。")

    return {
        "recommended_stage": stage,
        "meta": STAGE_META[stage],
        "confidence": conf,
        "reasons": reasons,
        "suggested_params": {
            "name_col": keys.get("name_col") or (profile["tables"][0].get("primary_text_col") if profile.get("tables") else None),
            "amount_col": keys.get("amount_col"),
            "tax_col": keys.get("tax_col"),
            "phone_col": keys.get("phone_col"),
            "city_col": keys.get("city_col"),
            "enable_hybrid_llm": True,
        },
    }
