"""Uploaded standard roster: column detection, config validation, and runtime catalog build."""

import os
import re
import tempfile
from typing import Any, Optional

import pandas as pd
from pydantic import BaseModel

from app.models.entities import DataSource
from app.services.storage import storage

ID_TERMS = ["id", "code", "编号", "编码", "supplier_id", "customer_id", "unique_id"]
NAME_TERMS = ["name", "名称", "公司名称", "company_name", "supplier_name", "customer_name", "full_name"]
ALIAS_TERMS = ["alias", "aliases", "别名", "曾用名"]
TAX_TERMS = ["tax_id", "税号", "统一社会信用代码", "uscc"]
PHONE_TERMS = ["phone", "mobile", "电话", "手机"]

ROSTER_CAP = 50_000
ALIAS_SEPARATORS = re.compile(r"[;；,，、|]")


class RosterError(Exception):
    """A roster is misconfigured or its file cannot supply a catalog."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RosterConfig(BaseModel):
    id_column: str = ""  # empty => generated ids
    name_column: str
    alias_columns: list[str] = []
    tax_id_column: Optional[str] = None
    phone_column: Optional[str] = None


def _normalized(column: str) -> str:
    return str(column).strip().lstrip("﻿").lower()


def _match_first(columns: list[str], terms: list[str]) -> Optional[str]:
    wanted = {term.lower() for term in terms}
    for col in columns:
        if _normalized(col) in wanted:
            return col
    return None


def detect_roster_columns(columns: list[str]) -> dict:
    """Map a profile's columns onto id/name/alias/tax/phone using the built-in terms."""
    alias_wanted = {term.lower() for term in ALIAS_TERMS}
    alias_columns = [col for col in columns if _normalized(col) in alias_wanted]
    return {
        "id_column": _match_first(columns, ID_TERMS) or "",
        "name_column": _match_first(columns, NAME_TERMS) or "",
        "alias_columns": alias_columns,
        "tax_id_column": _match_first(columns, TAX_TERMS) or "",
        "phone_column": _match_first(columns, PHONE_TERMS) or "",
    }


def normalize_roster_config(raw: Optional[dict], columns: list[str]) -> RosterConfig:
    """Validate a roster config against a profile's columns, auto-detecting when empty."""
    raw = raw or {}
    if not raw:
        raw = detect_roster_columns(columns)

    id_col = str(raw.get("id_column") or "").strip()
    name_col = str(raw.get("name_column") or "").strip()
    alias_cols = [str(c).strip() for c in (raw.get("alias_columns") or []) if str(c).strip()]
    tax_col = str(raw.get("tax_id_column") or "").strip()
    phone_col = str(raw.get("phone_column") or "").strip()

    if not name_col:
        raise RosterError("ROSTER_NAME_COLUMN_MISSING", "未指定名称列；标准花名册必须有一个名称列。")
    for col in [id_col, name_col, tax_col, phone_col]:
        if col and col not in columns:
            raise RosterError("ROSTER_COLUMN_NOT_FOUND", f"花名册列不存在于数据源：{col}")
    for col in alias_cols:
        if col not in columns:
            raise RosterError("ROSTER_COLUMN_NOT_FOUND", f"花名册列不存在于数据源：{col}")

    return RosterConfig(
        id_column=id_col,
        name_column=name_col,
        alias_columns=alias_cols,
        tax_id_column=tax_col or None,
        phone_column=phone_col or None,
    )


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return str(value).strip()


def _split_aliases(value: str) -> list[str]:
    return [part.strip() for part in ALIAS_SEPARATORS.split(value) if part.strip()]


def _read_roster_df(object_key: str, name: str) -> pd.DataFrame:
    ext = os.path.splitext(name or object_key)[1].lower()
    if ext not in (".csv", ".tsv"):
        raise RosterError("ROSTER_FILE_UNSUPPORTED", f"标准花名册仅支持 CSV/TSV 文件，收到：{ext or '未知扩展名'}")
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, os.path.basename(object_key))
        storage.materialize_to_local(object_key, local)
        if ext == ".tsv":
            return pd.read_csv(local, sep="\t", dtype=str, keep_default_na=False, encoding="utf-8-sig")
        return pd.read_csv(local, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _build_catalog(df: pd.DataFrame, config: RosterConfig) -> list[dict]:
    required = [config.name_column]
    if config.id_column:
        required.append(config.id_column)
    for col in required + config.alias_columns + [c for c in (config.tax_id_column, config.phone_column) if c]:
        if col not in df.columns:
            raise RosterError("ROSTER_COLUMN_NOT_FOUND", f"花名册文件缺少列：{col}")

    entries: list[dict] = []
    for index, row in df.iterrows():
        name = _as_text(row.get(config.name_column))
        if not name:
            continue
        entry_id = _as_text(row.get(config.id_column)) if config.id_column else ""
        if not entry_id:
            entry_id = f"ROSTER-{index + 1:06d}"

        aliases: list[str] = []
        for col in config.alias_columns:
            aliases.extend(_split_aliases(_as_text(row.get(col))))
        if config.tax_id_column:
            tax = _as_text(row.get(config.tax_id_column))
            if tax:
                aliases.append(tax)
        if config.phone_column:
            phone = _as_text(row.get(config.phone_column))
            if phone:
                aliases.append(phone)

        seen: set[str] = set()
        deduped: list[str] = []
        for alias in aliases:
            if alias and alias != name and alias not in seen:
                seen.add(alias)
                deduped.append(alias)

        entries.append({"id": entry_id, "name": name, "aliases": deduped})
        if len(entries) >= ROSTER_CAP:
            break

    if not entries:
        raise RosterError("ROSTER_EMPTY", "标准花名册文件没有可用的名称行，无法用于对齐。")
    return entries


def roster_catalog_for_project(db, project_id: str):
    """Return (catalog, source) for the latest standard roster, or (None, None) when absent."""
    sources = (
        db.query(DataSource)
        .filter(DataSource.project_id == project_id)
        .order_by(DataSource.created_at.desc())
        .all()
    )
    source = next((s for s in sources if (s.routing_json or {}).get("role") == "standard_roster"), None)
    if source is None:
        return None, None

    roster = (source.routing_json or {}).get("roster") or {}
    config = RosterConfig(
        id_column=str(roster.get("id_column") or ""),
        name_column=str(roster.get("name_column") or ""),
        alias_columns=[str(c) for c in (roster.get("alias_columns") or [])],
        tax_id_column=roster.get("tax_id_column") or None,
        phone_column=roster.get("phone_column") or None,
    )
    if not config.name_column:
        raise RosterError("ROSTER_NAME_COLUMN_MISSING", "该标准花名册未配置名称列，请重新保存字段映射。")
    if not source.object_key:
        raise RosterError("ROSTER_FILE_MISSING", "标准花名册文件缺失，无法加载对齐目录。")

    df = _read_roster_df(source.object_key, source.name)
    return _build_catalog(df, config), source
