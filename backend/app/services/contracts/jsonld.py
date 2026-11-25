"""JSON-LD instance generation from a stored LinkML contract model."""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Iterable
from urllib.parse import quote

import yaml


class JsonLdInvalid(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class JsonLdNoContract(JsonLdInvalid):
    def __init__(self, message: str = "contract model is missing"):
        super().__init__("NO_CONTRACT", message)


_NORMALIZE = re.compile(r"[^0-9A-Za-z一-鿿]+")


def _normalize(value: str) -> str:
    return _NORMALIZE.sub("", value).casefold()


def _base_uri(linkml_yaml: str) -> tuple[str, dict[str, str]]:
    try:
        document = yaml.safe_load(linkml_yaml) or {}
    except yaml.YAMLError:
        document = {}
    if not isinstance(document, dict):
        document = {}
    prefixes = {str(key): str(value) for key, value in (document.get("prefixes") or {}).items() if isinstance(value, str)}
    base = document.get("id")
    if not isinstance(base, str) or not base.strip():
        default_prefix = document.get("default_prefix")
        base = prefixes.get(default_prefix) if isinstance(default_prefix, str) else None
    base = (base or "https://w3id.org/linkml/").strip()
    if not base.endswith(("/", "#")):
        base = f"{base}/"
    return base, prefixes


def iter_instance_rows(text: str, max_rows: int | None = None) -> tuple[list[str], list[dict[str, str]], bool]:
    """Read a CSV instance sheet without materializing more rows than asked for."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = [str(cell) for cell in next(reader)]
    except StopIteration:
        return [], [], False
    rows: list[dict[str, str]] = []
    has_more = False
    for row in reader:
        if max_rows is not None and len(rows) >= max_rows:
            has_more = True
            break
        rows.append(dict(zip(header, [str(cell) for cell in row])))
    return header, rows, has_more


def jsonld_model(version) -> dict[str, Any]:
    try:
        canonical = json.loads(version.canonical_model_json)
    except (TypeError, ValueError) as exc:
        raise JsonLdNoContract("contract model JSON is invalid") from exc
    classes = canonical.get("classes")
    if not isinstance(classes, dict) or not classes:
        raise JsonLdNoContract("contract model has no classes")
    base_uri, prefixes = _base_uri(version.artifacts_json.get("linkml_yaml", ""))

    model_classes: dict[str, Any] = {}
    for class_name in sorted(classes):
        attributes = classes[class_name].get("attributes") or {}
        slots: dict[str, Any] = {}
        identifier = None
        for slot_name in sorted(attributes):
            definition = attributes[slot_name] or {}
            slots[slot_name] = {
                "range": definition.get("range", "string"),
                "identifier": definition.get("identifier") is True,
                "required": definition.get("required") is True,
            }
            if definition.get("identifier") is True and identifier is None:
                identifier = slot_name
        if identifier is None:
            identifier = next((name for name, slot in slots.items() if slot["required"]), None)
        if identifier is None and slots:
            identifier = next(iter(slots))
        model_classes[class_name] = {
            "slots": slots,
            "id_slot": identifier,
            "is_a": list(classes[class_name].get("is_a") or []),
        }
    return {"classes": model_classes, "prefixes": prefixes, "base_uri": base_uri}


def infer_jsonld_mapping(model: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    classes = model.get("classes") or {}
    if not classes:
        raise JsonLdNoContract()
    class_name = next(
        (name for name in sorted(classes) if classes[name].get("id_slot")),
        sorted(classes)[0],
    )
    class_model = classes[class_name]
    normalized_columns = {_normalize(column): column for column in columns}
    mapping: dict[str, str] = {}
    for slot_name in class_model["slots"]:
        column = normalized_columns.get(_normalize(slot_name))
        if column:
            mapping[slot_name] = column
    return {
        "class_name": class_name,
        "id_slot": class_model.get("id_slot") or "",
        "columns": mapping,
    }


def _coerce(value: str, range_name: str) -> Any:
    if range_name in ("integer", "negativeinteger", "nonnegativeinteger", "positiveinteger"):
        try:
            return int(value)
        except ValueError:
            return value
    if range_name in ("float", "double", "decimal"):
        try:
            return float(value)
        except ValueError:
            return value
    if range_name == "boolean":
        lowered = value.strip().casefold()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return value


def build_jsonld(
    model: dict[str, Any],
    rows: Iterable[dict[str, str]],
    mapping: dict[str, Any],
    *,
    limit: int = 5000,
) -> dict[str, Any]:
    class_name = mapping.get("class_name")
    classes = model.get("classes") or {}
    if class_name not in classes:
        raise JsonLdInvalid("UNKNOWN_CLASS", f"unknown class: {class_name}")
    class_model = classes[class_name]
    slots = class_model["slots"]
    id_slot = mapping.get("id_slot")
    if not isinstance(id_slot, str) or not id_slot:
        raise JsonLdInvalid("MISSING_ID_SLOT", "choose an ID slot")
    if id_slot not in slots:
        raise JsonLdInvalid("UNKNOWN_SLOT", f"unknown ID slot: {id_slot}")

    columns = mapping.get("columns") or {}
    if not isinstance(columns, dict):
        raise JsonLdInvalid("INVALID_COLUMNS", "columns must map slot names to column names")
    if id_slot not in columns or not columns[id_slot]:
        raise JsonLdInvalid("MISSING_ID_COLUMN", f"map a data column to the ID slot {id_slot}")
    used_columns: set[str] = set()
    for slot_name, column in columns.items():
        if slot_name not in slots:
            raise JsonLdInvalid("UNKNOWN_SLOT", f"unknown slot: {slot_name}")
        if not isinstance(column, str) or not column:
            raise JsonLdInvalid("INVALID_COLUMNS", f"slot {slot_name} has no column")
        if column in used_columns:
            raise JsonLdInvalid("DUPLICATE_COLUMN_MAPPING", f"column {column} is mapped more than once")
        used_columns.add(column)

    base_uri = model.get("base_uri") or "https://w3id.org/linkml/"
    context: dict[str, Any] = {"@vocab": base_uri, "@base": base_uri}
    for slot_name in slots:
        context[slot_name] = f"{base_uri}{slot_name}"

    graph = []
    truncated = False
    for index, row in enumerate(rows):
        if index >= limit:
            truncated = True
            break
        id_value = row.get(columns[id_slot], "")
        node: dict[str, Any] = {
            "@id": f"urn:osu:{quote(str(id_value), safe='')}" if id_value else f"urn:osu:autogen:{index + 1}",
            "@type": class_name,
        }
        for slot_name, column in columns.items():
            raw = row.get(column, "")
            if raw is None or str(raw).strip() == "":
                continue
            node[slot_name] = _coerce(str(raw), slots[slot_name]["range"])
        graph.append(node)

    return {"@context": context, "@graph": graph, "count": len(graph), "truncated": truncated}
