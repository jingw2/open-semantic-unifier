"""Contract model validation turns LinkML YAML into a per-issue report and a canonical model JSON."""
import json
import shutil
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.contracts import compiler, validation

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"

_MISSING_COMMANDS = sorted(name for name in compiler.COMMAND_NAMES if shutil.which(name) is None)

requires_commands = pytest.mark.skipif(
    bool(_MISSING_COMMANDS),
    reason=f"compiler commands missing from PATH: {', '.join(_MISSING_COMMANDS)}",
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _validate(yaml_text: str, **kwargs) -> validation.ValidationReport:
    return validation.validate_contract_model(linkml_yaml=yaml_text, **kwargs)


def _issue(report: validation.ValidationReport, code: str) -> validation.ContractIssue:
    return next(issue for issue in report.issues if issue.code == code)


def test_minimal_fixture_is_accepted():
    report = _validate((FIXTURES / "minimal_schema.yaml").read_text(encoding="utf-8"))

    assert report.accepted is True
    assert report.issues == ()
    assert report.counts == {"issues": 0, "classes": 1, "attributes": 3, "enums": 1, "relations": 0}
    canonical = json.loads(report.canonical_model_json)
    assert set(canonical) == {"classes", "enums", "relations"}
    task = canonical["classes"]["Task"]["attributes"]
    assert task["task_id"] == {"range": "string", "identifier": True, "required": True}
    assert task["title"] == {"range": "string", "required": True}
    assert task["status"] == {"range": "TaskStatus"}
    assert canonical["enums"]["TaskStatus"] == {"values": ["DONE", "OPEN"]}
    assert canonical["relations"] == {}


@requires_commands
def test_tsv_compiled_contract_validates():
    compiled = compiler.compile_contract(filename="minimal_schema.tsv", content=_fixture("minimal_schema.tsv"))

    report = _validate(compiled.linkml_yaml)
    assert report.accepted is True
    assert report.counts["issues"] == 0
    assert report.counts["classes"] == 1
    assert report.counts["attributes"] == 3
    canonical = json.loads(report.canonical_model_json)
    task = canonical["classes"]["Task"]["attributes"]
    assert task["task_id"]["identifier"] is True
    assert task["status"] == {"range": "TaskStatus", "description": "Current task state"}


def test_class_slots_list_with_slot_usage_is_supported():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    slots:\n"
        "      - task_id\n"
        "      - status\n"
        "    slot_usage:\n"
        "      task_id:\n"
        "        identifier: true\n"
        "        range: string\n"
        "      status:\n"
        "        range: TaskStatus\n"
        "enums:\n"
        "  TaskStatus:\n"
        "    permissible_values:\n"
        "      OPEN:\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is True
    canonical = json.loads(report.canonical_model_json)
    task = canonical["classes"]["Task"]["attributes"]
    assert task["status"] == {"range": "TaskStatus"}
    assert task["task_id"]["identifier"] is True


def test_duplicate_attribute_keys_reported_with_path():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      task_id:\n"
        "        identifier: true\n"
        "      title:\n"
        "        required: true\n"
        "      title:\n"
        "        required: false\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    assert report.canonical_model_json is None
    issue = _issue(report, "DUPLICATE_KEY")
    assert issue.path == "classes.Task.attributes.title"
    assert report.counts["DUPLICATE_KEY"] == 1


def test_unknown_range_is_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      title:\n"
        "        range: Bogus\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "UNKNOWN_RANGE")
    assert issue.path == "classes.Task.title"


def test_enum_without_permissible_values_is_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      status:\n"
        "        range: TaskStatus\n"
        "enums:\n"
        "  TaskStatus:\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "EMPTY_ENUM")
    assert issue.path == "enums.TaskStatus"


def test_enum_with_empty_permissible_values_is_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      status:\n"
        "        range: TaskStatus\n"
        "enums:\n"
        "  TaskStatus:\n"
        "    permissible_values: {}\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "EMPTY_ENUM")
    assert issue.path == "enums.TaskStatus"


def test_duplicate_enum_values_are_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      status:\n"
        "        range: TaskStatus\n"
        "enums:\n"
        "  TaskStatus:\n"
        "    permissible_values:\n"
        "      OPEN:\n"
        "      DONE:\n"
        "      DONE:\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "DUPLICATE_ENUM_VALUE")
    assert issue.path == "enums.TaskStatus.permissible_values.DONE"


def test_multiple_identifiers_in_one_class_are_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      task_id:\n"
        "        identifier: true\n"
        "      alternate_id:\n"
        "        identifier: true\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "MULTIPLE_IDENTIFIERS")
    assert issue.path == "classes.Task"


def test_is_a_cycle_is_rejected():
    yaml_text = (
        "classes:\n"
        "  A:\n"
        "    is_a: B\n"
        "  B:\n"
        "    is_a: A\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "CIRCULAR_INHERITANCE")
    assert issue.path == "classes.A"


def test_is_a_to_undefined_class_is_rejected():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    is_a: MissingBase\n"
        "    attributes:\n"
        "      title: {}\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    issue = _issue(report, "UNKNOWN_INHERITANCE_TARGET")
    assert issue.path == "classes.Task.is_a"


def test_canonical_json_includes_inheritance():
    yaml_text = (
        "classes:\n"
        "  Base:\n"
        "    attributes:\n"
        "      id: {identifier: true}\n"
        "  Child:\n"
        "    is_a: Base\n"
        "    attributes:\n"
        "      name: {}\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is True
    canonical = json.loads(report.canonical_model_json)
    assert canonical["classes"]["Child"]["is_a"] == ["Base"]
    assert "is_a" not in canonical["classes"]["Base"]


def test_self_reference_range_is_allowed():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      parent:\n"
        "        range: Task\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is True
    canonical = json.loads(report.canonical_model_json)
    assert canonical["relations"] == {"Task": ["Task"]}


def test_entity_type_check_uses_known_and_allowed_new_sets():
    yaml_text = (FIXTURES / "minimal_schema.yaml").read_text(encoding="utf-8")

    unknown = _validate(yaml_text, known_entity_types={"Other"})
    assert unknown.accepted is False
    issue = _issue(unknown, "UNKNOWN_ENTITY_TYPE")
    assert issue.path == "classes.Task"

    allowed = _validate(yaml_text, known_entity_types={"Other"}, allowed_new_entity_types={"Task"})
    assert allowed.accepted is True
    assert allowed.canonical_model_json is not None

    known = _validate(yaml_text, known_entity_types={"Task"})
    assert known.accepted is True

    no_sets = _validate(yaml_text)
    assert no_sets.accepted is True
    assert "UNKNOWN_ENTITY_TYPE" not in {issue.code for issue in no_sets.issues}


def test_canonical_json_is_deterministic_across_runs_and_insertion_order():
    fixture_text = (FIXTURES / "minimal_schema.yaml").read_text(encoding="utf-8")
    first = _validate(fixture_text)
    second = _validate(fixture_text)
    assert first.canonical_model_json is not None
    assert first.canonical_model_json == second.canonical_model_json

    ordered_a = (
        "classes:\n"
        "  Note:\n"
        "    attributes:\n"
        "      body: {required: true}\n"
        "      task:\n"
        "        range: Task\n"
        "  Task:\n"
        "    attributes:\n"
        "      title: {required: true}\n"
        "      task_id: {identifier: true}\n"
        "enums:\n"
        "  TaskStatus:\n"
        "    permissible_values: {DONE: null, OPEN: null}\n"
    )
    ordered_b = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      task_id: {identifier: true}\n"
        "      title: {required: true}\n"
        "  Note:\n"
        "    attributes:\n"
        "      task:\n"
        "        range: Task\n"
        "      body: {required: true}\n"
        "enums:\n"
        "  TaskStatus:\n"
        "    permissible_values: {OPEN: null, DONE: null}\n"
    )
    report_a = _validate(ordered_a)
    report_b = _validate(ordered_b)
    assert report_a.accepted and report_b.accepted
    assert report_a.canonical_model_json == report_b.canonical_model_json
    canonical = json.loads(report_a.canonical_model_json)
    assert canonical["relations"] == {"Note": ["Task"]}


def test_missing_range_falls_back_to_default_range_then_string():
    with_default = "default_range: integer\nclasses:\n  Task:\n    attributes:\n      count:\n"
    report = _validate(with_default)
    assert report.accepted is True
    canonical = json.loads(report.canonical_model_json)
    assert canonical["classes"]["Task"]["attributes"]["count"]["range"] == "integer"

    without_default = "classes:\n  Task:\n    attributes:\n      count:\n"
    report = _validate(without_default)
    assert report.accepted is True
    canonical = json.loads(report.canonical_model_json)
    assert canonical["classes"]["Task"]["attributes"]["count"]["range"] == "string"


def test_invalid_yaml_is_rejected():
    report = _validate("classes: [unclosed\n")
    assert report.accepted is False
    assert report.canonical_model_json is None
    issue = _issue(report, "INVALID_YAML")
    assert issue.code == "INVALID_YAML"


def test_non_mapping_top_level_is_rejected():
    report = _validate("- just\n- a\n- list\n")
    assert report.accepted is False
    assert _issue(report, "NOT_A_MAPPING").code == "NOT_A_MAPPING"


def test_missing_classes_variants_are_rejected():
    for yaml_text in (
        "id: https://example.org/x\nname: x\n",
        "id: https://example.org/x\nclasses: []\n",
        "id: https://example.org/x\nclasses: {}\n",
    ):
        report = _validate(yaml_text)
        assert report.accepted is False
        issue = _issue(report, "MISSING_CLASSES")
        assert issue.path == "classes"


def test_all_structural_issues_are_reported_in_one_pass():
    yaml_text = (
        "classes:\n"
        "  Task:\n"
        "    attributes:\n"
        "      status:\n"
        "        range: Bogus\n"
        "      task_id: {identifier: true}\n"
        "      alternate_id: {identifier: true}\n"
        "  A:\n"
        "    is_a: B\n"
        "  B:\n"
        "    is_a: A\n"
        "enums:\n"
        "  Empty:\n"
    )
    report = _validate(yaml_text)
    assert report.accepted is False
    assert report.canonical_model_json is None
    codes = {issue.code for issue in report.issues}
    assert codes == {
        "UNKNOWN_RANGE",
        "MULTIPLE_IDENTIFIERS",
        "CIRCULAR_INHERITANCE",
        "EMPTY_ENUM",
    }
    assert report.counts["issues"] == len(report.issues)
    assert report.counts["UNKNOWN_RANGE"] == 1
    assert report.counts["classes"] == 3
    assert report.counts["enums"] == 1


def test_validator_performs_no_database_or_storage_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("validator must not call database or storage")

    import app.core.db as db_module
    import app.services.storage as storage_module

    monkeypatch.setattr(db_module, "SessionLocal", forbidden)
    monkeypatch.setattr(storage_module.storage, "get_bytes", forbidden)
    monkeypatch.setattr(storage_module.storage, "put_bytes", forbidden)

    report = _validate((FIXTURES / "minimal_schema.yaml").read_text(encoding="utf-8"))
    assert report.accepted is True
