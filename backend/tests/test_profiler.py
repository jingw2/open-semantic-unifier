"""Method recommendation and LinkML detection for the data profiler."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.profiler import method_for_routing, profile_items, recommend_pipeline


def test_small_dirty_table_uses_rule_clean():
    routing = recommend_pipeline({
        "total_rows": 250, "table_count": 1, "has_whitespace_noise": True,
        "has_metric_yaml": False, "has_schemasheet_tsv": False,
        "has_jargon": False, "detected_keys": {"name_col": "supplier_raw_name"},
    })
    assert routing["method"] == {
        "key": "rule_clean_w3c",
        "label": "规则清洗 + W3C 契约",
        "stage": "L1",
        "description": "先清洗小表，再用 LinkML 契约固化标准字段。",
        "reason": "检测到 1 张 250 行的小表，字段有空格和大小写噪声。",
        "available": True,
    }


def test_two_tables_use_splink_duckdb():
    routing = recommend_pipeline({"total_rows": 35000, "table_count": 2, "detected_keys": {}})
    assert routing["method"]["key"] == "splink_duckdb"
    assert routing["method"]["stage"] == "L2"
    assert "Splink" in routing["method"]["label"]


def test_metric_yaml_uses_metrics_method():
    routing = recommend_pipeline({"total_rows": 0, "table_count": 0, "has_metric_yaml": True, "detected_keys": {}})
    assert routing["method"]["key"] == "metrics_yaml"
    assert routing["method"]["stage"] == "L4"


def test_linkml_yaml_is_detected_and_routes_to_contract():
    text = "id: https://example.org/supplier\nclasses:\n  Supplier:\n    attributes:\n      name:\n        required: true\ndefault_range: string\n"
    profile = profile_items([{"kind": "yaml", "name": "supplier.yaml", "text": text}])
    assert profile["has_linkml_contract"] is True
    routing = recommend_pipeline(profile)
    assert routing["method"]["key"] == "contract_w3c"
    assert routing["method"]["stage"] == "L3"


def test_method_for_routing_backfills_legacy_rows():
    assert method_for_routing({"recommended_stage": "L2"})["key"] == "splink_duckdb"
    existing = {"recommended_stage": "L1", "method": {"key": "custom", "label": "Custom"}}
    assert method_for_routing(existing)["key"] == "custom"
    assert method_for_routing(None)["key"] == "rule_clean_w3c"
