"""L2 match field selection accepts one or many fields and rejects empty selections."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.engines import resolve_l2_match_fields


def test_multiple_fields_split_into_fuzzy_and_exact():
    selected, fuzzy, exact = resolve_l2_match_fields(
        {"match_fields": ["company_name", "tax_id", "phone"]},
        {"company_name", "tax_id", "phone", "city"},
    )
    assert selected == ["company_name", "tax_id", "phone"]
    assert fuzzy == ["company_name"]
    assert exact == ["tax_id", "phone"]


def test_one_field_is_enough():
    selected, fuzzy, exact = resolve_l2_match_fields({"match_fields": ["tax_id"]}, {"tax_id"})
    assert selected == ["tax_id"]
    assert fuzzy == []
    assert exact == ["tax_id"]


def test_empty_or_unknown_fields_are_rejected():
    with pytest.raises(ValueError, match="至少需要一个匹配字段"):
        resolve_l2_match_fields({}, {"amount"})
    with pytest.raises(ValueError, match="不在数据表中"):
        resolve_l2_match_fields({"match_fields": ["nope"]}, {"company_name"})


def test_legacy_role_params_still_work():
    selected, fuzzy, exact = resolve_l2_match_fields(
        {"name_col": "name", "phone_col": "phone"},
        {"name", "phone"},
    )
    assert selected == ["name", "phone"]
    assert fuzzy == ["name"]
    assert exact == ["phone"]
