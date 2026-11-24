"""Contract compiler turns source bytes into LinkML YAML, SQL DDL, JSON Schema, and an ER diagram."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.contracts import compiler

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"

_MISSING_COMMANDS = sorted(name for name in compiler.COMMAND_NAMES if shutil.which(name) is None)

requires_commands = pytest.mark.skipif(
    bool(_MISSING_COMMANDS),
    reason=f"compiler commands missing from PATH: {', '.join(_MISSING_COMMANDS)}",
)


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@requires_commands
@pytest.mark.parametrize("filename", ["minimal_schema.yaml", "minimal_schema.yml"])
def test_linkml_yaml_compiles_non_empty_artifacts_and_records_versions(filename):
    content = _fixture("minimal_schema.yaml")
    result = compiler.compile_contract(filename=filename, content=content)

    assert result.linkml_yaml
    assert result.sql_ddl
    assert result.json_schema
    assert result.er_diagram
    assert result.input_sha256 == hashlib.sha256(content).hexdigest()
    assert len(result.input_sha256) == 64
    assert set(result.compiler_versions) == {
        "linkml", "schemasheets", "sheets2linkml", "gen-sqlddl", "gen-json-schema", "gen-erdiagram",
    }
    assert all(value and "/" not in value for value in result.compiler_versions.values())


@requires_commands
def test_compiled_artifacts_contain_no_legacy_demo_names():
    result = compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))
    for artifact in (result.linkml_yaml, result.sql_ddl, result.json_schema, result.er_diagram):
        for demo_name in ("SalesOrder", "ShipmentOrder", "SettlementBill"):
            assert demo_name not in artifact


@requires_commands
def test_tsv_compile_produces_non_empty_artifacts():
    content = _fixture("minimal_schema.tsv")
    result = compiler.compile_contract(filename="minimal_schema.tsv", content=content)

    assert result.linkml_yaml
    assert result.sql_ddl
    assert result.json_schema
    assert result.er_diagram
    assert result.input_sha256 == hashlib.sha256(content).hexdigest()


@requires_commands
def test_csv_compile_matches_tsv_compile():
    tsv_content = _fixture("minimal_schema.tsv")
    csv_content = _fixture("minimal_schema.csv")
    tsv_result = compiler.compile_contract(filename="minimal_schema.tsv", content=tsv_content)
    csv_result = compiler.compile_contract(filename="minimal_schema.csv", content=csv_content)

    assert csv_result.linkml_yaml == tsv_result.linkml_yaml
    assert csv_result.sql_ddl == tsv_result.sql_ddl
    assert csv_result.json_schema == tsv_result.json_schema
    assert csv_result.er_diagram == tsv_result.er_diagram
    assert csv_result.input_sha256 == hashlib.sha256(csv_content).hexdigest()
    assert csv_result.input_sha256 != tsv_result.input_sha256


@requires_commands
def test_same_input_twice_produces_identical_artifacts():
    content = _fixture("minimal_schema.yaml")
    first = compiler.compile_contract(filename="minimal_schema.yaml", content=content)
    second = compiler.compile_contract(filename="minimal_schema.yaml", content=content)

    assert first == second
    assert first.input_sha256 == hashlib.sha256(content).hexdigest()


@requires_commands
def test_compiler_performs_no_database_or_storage_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("compiler must not call database or storage")

    import app.core.db as db_module
    import app.services.storage as storage_module

    monkeypatch.setattr(db_module, "SessionLocal", forbidden)
    monkeypatch.setattr(storage_module.storage, "get_bytes", forbidden)
    monkeypatch.setattr(storage_module.storage, "put_bytes", forbidden)

    result = compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))
    assert result.linkml_yaml and result.sql_ddl and result.json_schema and result.er_diagram


def test_empty_content_is_rejected():
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="minimal_schema.yaml", content=b"")


def test_unsupported_suffix_is_rejected():
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="contracts.txt", content=b"anything")


def test_yaml_that_is_not_a_mapping_is_rejected():
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="minimal_schema.yaml", content=b"- just\n- a\n- list\n")


def test_yaml_without_nonempty_classes_mapping_is_rejected():
    no_classes = b"id: https://example.org/x\nname: x\n"
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="minimal_schema.yaml", content=no_classes)

    empty_classes = b"id: https://example.org/x\nname: x\nclasses: {}\n"
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="minimal_schema.yaml", content=empty_classes)


def test_input_over_five_mib_is_rejected():
    content = b"x" * (compiler.MAX_CONTRACT_SOURCE_BYTES + 1)
    with pytest.raises(compiler.ContractCompileInvalid):
        compiler.compile_contract(filename="minimal_schema.yaml", content=content)


def test_missing_compiler_commands_raise_unavailable(monkeypatch):
    real_which = shutil.which

    def fake_which(name):
        if name == "gen-erdiagram":
            return None
        return real_which(name)

    monkeypatch.setattr(compiler.shutil, "which", fake_which)
    with pytest.raises(compiler.ContractCompilerUnavailable) as excinfo:
        compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))
    assert "gen-erdiagram" in str(excinfo.value)


def test_command_timeout_raises_contract_compile_timeout(monkeypatch):
    def slow_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["gen-sqlddl"], timeout=30)

    monkeypatch.setattr(compiler.subprocess, "run", slow_run)
    with pytest.raises(compiler.ContractCompileTimeout):
        compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))


def test_command_failure_maps_to_invalid_with_sanitized_stderr(monkeypatch):
    def failing_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="SECRET=do-not-leak\nEARLY_UNIQUE_LINE\n" + "mid line\n" * 40 + "boom: tail marker\n",
        )

    monkeypatch.setattr(compiler.subprocess, "run", failing_run)
    with pytest.raises(compiler.ContractCompileInvalid) as excinfo:
        compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))
    message = str(excinfo.value)
    assert "do-not-leak" not in message
    assert "EARLY_UNIQUE_LINE" not in message
    assert "boom: tail marker" in message


def test_oversized_generated_artifact_raises(monkeypatch):
    monkeypatch.setattr(compiler, "MAX_GENERATED_ARTIFACT_BYTES", 10)
    with pytest.raises(compiler.ContractCompileOutputTooLarge):
        compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))


def test_empty_generated_artifact_is_rejected(monkeypatch):
    def empty_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compiler.subprocess, "run", empty_run)
    with pytest.raises(compiler.ContractCompileInvalid, match="generated sql_ddl is empty"):
        compiler.compile_contract(filename="minimal_schema.yaml", content=_fixture("minimal_schema.yaml"))


def test_missing_model_output_is_rejected(monkeypatch):
    def successful_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(compiler.subprocess, "run", successful_run)
    with pytest.raises(compiler.ContractCompileInvalid, match="produced no model output"):
        compiler.compile_contract(filename="minimal_schema.tsv", content=_fixture("minimal_schema.tsv"))


@requires_commands
def test_utf8_bom_is_accepted():
    content = b"\xef\xbb\xbf" + _fixture("minimal_schema.yaml")
    result = compiler.compile_contract(filename="minimal_schema.yaml", content=content)
    assert result.linkml_yaml.startswith("id:")
    assert result.sql_ddl and result.json_schema and result.er_diagram
    assert result.input_sha256 == hashlib.sha256(content).hexdigest()
