"""Built-in OpenRefine recipe registry: selection, limits, and column rendering."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.openrefine.recipes import (
    RecipeCatalogError,
    load_recipes,
    openrefine_limits_ok,
    recipe_payload,
    render_operations,
    select_recipe,
)


def supplier_profile(**overrides):
    profile = {
        "total_rows": 250,
        "table_count": 1,
        "has_whitespace_noise": True,
        "detected_keys": {"name_col": "supplier_raw_name", "phone_col": None},
        "tables": [{
            "name": "suppliers.csv",
            "rows": 250,
            "cols": 3,
            "columns": ["supplier_raw_name", "city", "tax_id"],
            "preview": [{"supplier_raw_name": "  Acme  Ltd ", "city": "Shenzhen", "tax_id": "913100"}],
        }],
    }
    profile.update(overrides)
    return profile


def test_recipe_catalog_is_read_only_and_versioned():
    recipes = load_recipes()
    assert {r.id for r in recipes} >= {
        "normalize_company_name", "normalize_whitespace", "normalize_punctuation",
        "remove_legal_suffix", "split_contact_column", "merge_name_columns",
    }
    assert all(r.version == "1.0.0" and r.operations for r in recipes)
    payload = recipe_payload(recipes[0])
    assert payload["id"] == recipes[0].id
    assert "operations" not in payload


def test_select_recipe_prefers_company_normalization_for_dirty_name_column():
    assert select_recipe(supplier_profile()).id == "normalize_company_name"


def test_select_recipe_uses_contact_split_when_preview_has_separators():
    profile = supplier_profile()
    profile["tables"][0]["columns"] = ["company_name", "contact"]
    profile["tables"][0]["preview"] = [{"company_name": "Acme", "contact": "alice;bob"}]
    profile["detected_keys"] = {"name_col": "company_name"}
    assert select_recipe(profile).id == "split_contact_column"


def test_select_recipe_merges_first_and_last_name_columns():
    profile = supplier_profile()
    profile["tables"][0]["columns"] = ["first_name", "last_name", "city"]
    profile["tables"][0]["preview"] = [{"first_name": "Ada", "last_name": "Lovelace", "city": "London"}]
    profile["detected_keys"] = {"name_col": None}
    profile["has_whitespace_noise"] = False
    assert select_recipe(profile).id == "merge_name_columns"


def test_select_recipe_honours_an_explicit_goal():
    assert select_recipe(supplier_profile(), "去掉公司名后缀").id == "remove_legal_suffix"
    assert select_recipe(supplier_profile(), "统一空格").id == "normalize_whitespace"


def test_select_recipe_returns_none_without_a_text_column():
    profile = supplier_profile()
    profile["tables"][0]["columns"] = ["amount"]
    profile["tables"][0]["preview"] = [{"amount": "1"}]
    profile["detected_keys"] = {"name_col": None}
    profile["has_whitespace_noise"] = False
    assert select_recipe(profile) is None


def test_render_operations_substitutes_the_detected_column():
    recipe = next(r for r in load_recipes() if r.id == "normalize_whitespace")
    operations = render_operations(recipe, supplier_profile())
    assert operations[0]["columnName"] == "supplier_raw_name"
    assert "${" not in repr(operations)


def test_limits_reject_large_multitable_or_wide_sources():
    ok, reason = openrefine_limits_ok({"total_rows": 50_001, "table_count": 1}, 1024)
    assert not ok and "50,000" in reason
    ok, reason = openrefine_limits_ok({"total_rows": 10, "table_count": 2}, 1024)
    assert not ok and "single table" in reason
    ok, reason = openrefine_limits_ok(
        {"total_rows": 10, "table_count": 1, "tables": [{"cols": 257}]}, 1024,
    )
    assert not ok and "256" in reason
    ok, reason = openrefine_limits_ok({"total_rows": 10, "table_count": 1}, 10 * 1024 * 1024 + 1)
    assert not ok and "10 MiB" in reason
    assert openrefine_limits_ok({"total_rows": 10, "table_count": 1}, 10 * 1024 * 1024)[0] is True


def test_recipe_expressions_only_use_grel_supported_syntax():
    for recipe in load_recipes():
        rendered = repr(recipe.operations)
        assert "/g'" not in rendered, f"{recipe.id} uses a JavaScript regex flag OpenRefine GREL rejects"


def test_invalid_recipe_file_is_rejected(tmp_path, monkeypatch):
    import app.services.openrefine.recipes as recipes_module

    bad = tmp_path / "bad.json"
    bad.write_text('{"id": "bad"}', encoding="utf-8")
    monkeypatch.setattr(recipes_module, "RECIPE_DIR", tmp_path)
    with pytest.raises(RecipeCatalogError, match="missing"):
        load_recipes()
