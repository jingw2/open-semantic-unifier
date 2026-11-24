"""Pure contract compilation: source bytes to LinkML YAML, SQL DDL, JSON Schema, and an ER diagram.

No database access, no storage calls, no version persistence. The same input bytes
always produce byte-identical artifacts and the same input_sha256.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Mapping

import yaml

from app.services.contracts.limits import MAX_CONTRACT_SOURCE_BYTES, MAX_GENERATED_ARTIFACT_BYTES

COMMAND_NAMES = ("sheets2linkml", "gen-sqlddl", "gen-json-schema", "gen-erdiagram")

COMMAND_TIMEOUT_SECONDS = 30

_YAML_SUFFIXES = (".yaml", ".yml")

_DIAGNOSTIC_TAIL_LINES = 10
_DIAGNOSTIC_MAX_CHARS = 2000

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|bearer|aws_access_key[_-]?id|private[_-]?key)\b\s*[:=]\s*\S+"
)

_BASE_SCHEMA_PATH = Path(__file__).with_name("base_schema.yaml")


class ContractCompilerError(Exception):
    """Base class for contract compiler domain failures."""


class ContractCompilerUnavailable(ContractCompilerError):
    """One or more compiler commands are missing from the environment."""


class ContractCompileTimeout(ContractCompilerError):
    """A compiler command exceeded the per-command time limit."""


class ContractCompileInvalid(ContractCompilerError):
    """The contract source cannot be compiled as provided."""


class ContractCompileOutputTooLarge(ContractCompilerError):
    """A generated artifact exceeds the size limit."""


@dataclass(frozen=True)
class CompiledContract:
    linkml_yaml: str
    sql_ddl: str
    json_schema: str
    er_diagram: str
    compiler_versions: Mapping[str, str]
    input_sha256: str


def compile_contract(*, filename: str, content: bytes) -> CompiledContract:
    """Compile contract source bytes into the four L3 artifacts without touching DB or storage."""
    if not content:
        raise ContractCompileInvalid("contract content is empty")
    if len(content) > MAX_CONTRACT_SOURCE_BYTES:
        raise ContractCompileInvalid(
            f"contract content exceeds the {MAX_CONTRACT_SOURCE_BYTES} byte limit"
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractCompileInvalid("contract content is not valid UTF-8") from exc

    lowered = filename.casefold()
    if lowered.endswith(_YAML_SUFFIXES):
        model_text = _validate_linkml_yaml(text)
        sheet_text = None
    elif lowered.endswith(".tsv"):
        model_text = None
        sheet_text = text
    elif lowered.endswith(".csv"):
        model_text = None
        sheet_text = _convert_csv_to_tsv(text)
    else:
        raise ContractCompileInvalid(
            "unsupported contract format; expected a .yaml, .yml, .tsv, or .csv file"
        )
    if sheet_text is not None and not sheet_text.strip():
        raise ContractCompileInvalid("contract content is empty")

    command_paths = _resolve_command_paths()
    versions = _compiler_versions()
    input_sha256 = hashlib.sha256(content).hexdigest()

    with tempfile.TemporaryDirectory() as tmpdir:
        if model_text is None:
            (Path(tmpdir) / "schema.tsv").write_text(sheet_text, encoding="utf-8")
            (Path(tmpdir) / "base.yaml").write_text(
                _BASE_SCHEMA_PATH.read_text(encoding="utf-8"), encoding="utf-8"
            )
            _run_command(
                command_paths["sheets2linkml"], "sheets2linkml",
                ["--base-schema-path", "base.yaml", "schema.tsv", "-o", "model.yaml"], tmpdir,
            )
            model_text = _read_model(tmpdir)
        else:
            (Path(tmpdir) / "model.yaml").write_text(model_text, encoding="utf-8")
            model_text = _check_artifact_size("linkml_yaml", model_text)

        sql_ddl = _check_artifact_size(
            "sql_ddl", _run_command(command_paths["gen-sqlddl"], "gen-sqlddl", ["model.yaml"], tmpdir)
        )
        json_schema = _check_artifact_size(
            "json_schema", _run_command(command_paths["gen-json-schema"], "gen-json-schema", ["model.yaml"], tmpdir)
        )
        er_diagram = _check_artifact_size(
            "er_diagram", _run_command(command_paths["gen-erdiagram"], "gen-erdiagram", ["model.yaml"], tmpdir)
        )

    return CompiledContract(
        linkml_yaml=model_text,
        sql_ddl=sql_ddl,
        json_schema=json_schema,
        er_diagram=er_diagram,
        compiler_versions=versions,
        input_sha256=input_sha256,
    )


def _validate_linkml_yaml(text: str) -> str:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractCompileInvalid("linkml input is not valid YAML") from exc
    if not isinstance(document, dict):
        raise ContractCompileInvalid("linkml input must be a mapping at the top level")
    classes = document.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise ContractCompileInvalid("linkml input requires a non-empty 'classes' mapping")
    return text


def _convert_csv_to_tsv(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    buffer = io.StringIO()
    csv.writer(buffer, delimiter="\t", lineterminator="\n").writerows(rows)
    return buffer.getvalue()


def _resolve_command_paths() -> dict[str, str]:
    paths = {name: shutil.which(name) for name in COMMAND_NAMES}
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        raise ContractCompilerUnavailable(
            "required compiler commands are missing from PATH: " + ", ".join(missing)
        )
    return paths


def _compiler_versions() -> dict[str, str]:
    versions = {}
    for package in ("linkml", "schemasheets"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unknown"
    for command in COMMAND_NAMES:
        versions[command] = _command_distribution_version(command)
    return versions


def _command_distribution_version(command: str) -> str:
    for entry_point in metadata.entry_points(group="console_scripts"):
        if entry_point.name == command and entry_point.dist is not None:
            return entry_point.dist.version
    return "unknown"


def _run_command(command_path: str, command_name: str, args: list[str], working_dir: str) -> str:
    try:
        completed = subprocess.run(
            [command_path, *args], cwd=working_dir, capture_output=True, text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ContractCompileTimeout(
            f"{command_name} exceeded the {COMMAND_TIMEOUT_SECONDS}s time limit"
        ) from exc
    if completed.returncode != 0:
        raise ContractCompileInvalid(
            f"{command_name} failed with exit code {completed.returncode}: "
            f"{_sanitize_diagnostic(completed.stderr, working_dir)}"
        )
    return completed.stdout


def _read_model(tmpdir: str) -> str:
    path = Path(tmpdir) / "model.yaml"
    if not path.exists():
        raise ContractCompileInvalid("sheets2linkml produced no model output")
    return _check_artifact_size("linkml_yaml", path.read_text(encoding="utf-8"))


def _check_artifact_size(label: str, text: str) -> str:
    if not text.strip():
        raise ContractCompileInvalid(f"generated {label} is empty")
    if len(text.encode("utf-8")) > MAX_GENERATED_ARTIFACT_BYTES:
        raise ContractCompileOutputTooLarge(
            f"generated {label} exceeds the {MAX_GENERATED_ARTIFACT_BYTES} byte limit"
        )
    return text


def _sanitize_diagnostic(text: str, working_dir: str) -> str:
    if working_dir:
        text = text.replace(working_dir, "<tmp>")
    text = _CREDENTIAL_PATTERN.sub(r"\1=<redacted>", text)
    lines = text.splitlines()
    if len(lines) > _DIAGNOSTIC_TAIL_LINES:
        text = "\n".join(lines[-_DIAGNOSTIC_TAIL_LINES:])
    if len(text) > _DIAGNOSTIC_MAX_CHARS:
        text = text[-_DIAGNOSTIC_MAX_CHARS:]
    return text
