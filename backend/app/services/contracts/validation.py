"""Pure LinkML contract model validation.

Validates the compiled LinkML YAML against the supported contract subset and
returns one issue per problem plus a canonical model JSON for hashing and diff.
No database access, no storage calls, no filesystem access. The same input
always produces the same report and the same canonical model JSON.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping

import yaml

SUPPORTED_BUILTIN_RANGES = frozenset({
    "string", "integer", "float", "double", "boolean", "date", "datetime",
    "time", "decimal", "uri", "curie", "uuid", "objectidentifier", "ncname",
    "negativeinteger", "nonnegativeinteger", "positiveinteger",
})

_CLASS_SLOT_KEYS = ("attributes", "slots", "slot_usage")


@dataclass(frozen=True)
class ContractIssue:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ValidationReport:
    accepted: bool
    issues: tuple[ContractIssue, ...]
    canonical_model_json: str | None
    counts: Mapping[str, int]


class _InvalidYaml(Exception):
    """The input is not a parseable YAML document."""


class _DuplicateTrackingLoader(yaml.SafeLoader):
    """SafeLoader that records every duplicate mapping key instead of overwriting it."""

    def __init__(self, stream):
        super().__init__(stream)
        self.duplicate_key_paths: list[str] = []
        self._key_path: list[str] = []


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    seen = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        path = ".".join([*loader._key_path, str(key)])
        if key in seen:
            loader.duplicate_key_paths.append(path)
        else:
            seen.add(key)
        loader._key_path.append(str(key))
        value = loader.construct_object(value_node, deep=deep)
        loader._key_path.pop()
        mapping[key] = value
    return mapping


_DuplicateTrackingLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def validate_contract_model(
    *,
    linkml_yaml: str,
    known_entity_types: set[str] | None = None,
    allowed_new_entity_types: set[str] | None = None,
) -> ValidationReport:
    """Validate a LinkML contract model and return per-issue results plus a canonical JSON."""
    issues: list[ContractIssue] = []
    try:
        document, duplicate_paths = _load_document(linkml_yaml)
    except _InvalidYaml as exc:
        issues.append(ContractIssue(
            path="", code="INVALID_YAML", message=f"linkml input is not valid YAML: {exc}",
        ))
        return _build_report(issues, None)

    if not isinstance(document, dict):
        issues.append(ContractIssue(
            path="", code="NOT_A_MAPPING",
            message="linkml input must be a mapping at the top level",
        ))
        return _build_report(issues, None)

    classes = document.get("classes")
    if not isinstance(classes, dict) or not classes:
        issues.append(ContractIssue(
            path="classes", code="MISSING_CLASSES",
            message="linkml input requires a non-empty 'classes' mapping",
        ))
        return _build_report(issues, None)

    for path in sorted(set(duplicate_paths)):
        if _is_permissible_value_path(path):
            issues.append(ContractIssue(
                path=path, code="DUPLICATE_ENUM_VALUE",
                message=f"duplicate permissible value '{path.rsplit('.', 1)[-1]}'",
            ))
        else:
            issues.append(ContractIssue(
                path=path, code="DUPLICATE_KEY",
                message=f"duplicate mapping key '{path.rsplit('.', 1)[-1]}'",
            ))

    model = _collect_model(document, classes)
    for class_name in sorted(model["classes"]):
        _check_class(issues, model, class_name)
    _check_enums(issues, model["enums"])
    _check_inheritance_targets(issues, model["classes"])
    _check_inheritance_cycles(issues, model["classes"])
    if known_entity_types is not None or allowed_new_entity_types is not None:
        _check_entity_types(
            issues, model["classes"],
            known_entity_types or set(), allowed_new_entity_types or set(),
        )

    return _build_report(issues, model)


def _load_document(text: str) -> tuple[object, list[str]]:
    loader = _DuplicateTrackingLoader(text)
    try:
        document = loader.get_single_data()
    except (yaml.YAMLError, TypeError) as exc:
        raise _InvalidYaml(str(exc)) from exc
    return document, loader.duplicate_key_paths


def _is_permissible_value_path(path: str) -> bool:
    parts = path.split(".")
    return len(parts) >= 3 and parts[0] == "enums" and parts[-2] == "permissible_values"


def _collect_model(document: dict, classes: dict) -> dict:
    default_range = document.get("default_range")
    if not isinstance(default_range, str):
        default_range = "string"

    enums_raw = document.get("enums")
    if not isinstance(enums_raw, dict):
        enums_raw = {}
    enum_names = {str(name) for name in enums_raw}

    classes_model = {}
    for class_name, class_def in classes.items():
        class_name = str(class_name)
        if not isinstance(class_def, dict):
            class_def = {}
        attributes = {}
        for key in _CLASS_SLOT_KEYS:
            raw = class_def.get(key)
            if isinstance(raw, dict):
                for attr_name, attr_def in raw.items():
                    slot = attributes.setdefault(str(attr_name), {})
                    if isinstance(attr_def, dict):
                        slot.update(attr_def)
            elif key == "slots" and isinstance(raw, list):
                for attr_name in raw:
                    if isinstance(attr_name, str):
                        attributes.setdefault(attr_name, {})
        classes_model[class_name] = {
            "attributes": attributes,
            "is_a": _parent_names(class_def.get("is_a")),
        }

    enums_model = {}
    for enum_name, enum_def in enums_raw.items():
        values = None
        if isinstance(enum_def, dict):
            permissible = enum_def.get("permissible_values")
            if isinstance(permissible, dict) and permissible:
                values = sorted(str(value) for value in permissible)
        enums_model[str(enum_name)] = values

    return {
        "classes": classes_model,
        "enums": enums_model,
        "enum_names": enum_names,
        "default_range": default_range,
    }


def _parent_names(is_a) -> list[str]:
    if isinstance(is_a, str):
        return [is_a]
    if isinstance(is_a, list):
        return [name for name in is_a if isinstance(name, str)]
    return []


def _check_class(issues: list[ContractIssue], model: dict, class_name: str) -> None:
    attributes = model["classes"][class_name]["attributes"]
    identifiers = [
        name for name, definition in attributes.items()
        if definition.get("identifier") is True
    ]
    if len(identifiers) > 1:
        issues.append(ContractIssue(
            path=f"classes.{class_name}", code="MULTIPLE_IDENTIFIERS",
            message=f"class '{class_name}' has {len(identifiers)} attributes with identifier: true",
        ))
    for attr_name, definition in sorted(attributes.items()):
        _check_attribute(issues, model, class_name, attr_name, definition)


def _check_attribute(
    issues: list[ContractIssue], model: dict,
    class_name: str, attr_name: str, definition: dict,
) -> None:
    path = f"classes.{class_name}.{attr_name}"
    raw_range = definition.get("range")
    resolved = model["default_range"] if raw_range is None else raw_range
    definition["_range"] = resolved
    if not (
        isinstance(resolved, str)
        and (
            resolved in SUPPORTED_BUILTIN_RANGES
            or resolved in model["classes"]
            or resolved in model["enum_names"]
        )
    ):
        issues.append(ContractIssue(
            path=path, code="UNKNOWN_RANGE",
            message=f"range '{resolved}' is not a supported built-in type, class, or enum",
        ))



def _check_enums(issues: list[ContractIssue], enums_model: dict) -> None:
    for enum_name, values in sorted(enums_model.items()):
        if values is None:
            issues.append(ContractIssue(
                path=f"enums.{enum_name}", code="EMPTY_ENUM",
                message=f"enum '{enum_name}' has no non-empty permissible_values mapping",
            ))


def _check_inheritance_targets(issues: list[ContractIssue], classes_model: dict) -> None:
    defined = set(classes_model)
    for class_name in sorted(classes_model):
        for parent in classes_model[class_name]["is_a"]:
            if parent not in defined:
                issues.append(ContractIssue(
                    path=f"classes.{class_name}.is_a", code="UNKNOWN_INHERITANCE_TARGET",
                    message=f"class '{class_name}' inherits from undefined class '{parent}'",
                ))


def _check_inheritance_cycles(issues: list[ContractIssue], classes_model: dict) -> None:
    defined = set(classes_model)
    graph = {
        name: [parent for parent in class_model["is_a"] if parent in defined]
        for name, class_model in classes_model.items()
    }
    for cycle in _inheritance_cycles(graph):
        start = cycle[0]
        message = "is_a inheritance cycle: " + " -> ".join([*cycle, start])
        issues.append(ContractIssue(
            path=f"classes.{start}", code="CIRCULAR_INHERITANCE", message=message,
        ))


def _inheritance_cycles(graph: dict[str, list[str]]) -> set[tuple[str, ...]]:
    white, gray, black = 0, 1, 2
    color = {name: white for name in graph}
    cycles: set[tuple[str, ...]] = set()
    for start in graph:
        if color[start] != white:
            continue
        color[start] = gray
        path = [start]
        stack = [(start, iter(graph[start]))]
        while stack:
            node, neighbors = stack[-1]
            advanced = False
            for neighbor in neighbors:
                if color[neighbor] == gray:
                    body = path[path.index(neighbor):]
                    pivot = min(body)
                    cycles.add(tuple(body[body.index(pivot):] + body[:body.index(pivot)]))
                elif color[neighbor] == white:
                    color[neighbor] = gray
                    path.append(neighbor)
                    stack.append((neighbor, iter(graph[neighbor])))
                    advanced = True
                    break
            if not advanced:
                color[node] = black
                path.pop()
                stack.pop()
    return cycles


def _check_entity_types(
    issues: list[ContractIssue], classes_model: dict,
    known: set[str], allowed_new: set[str],
) -> None:
    for class_name in sorted(classes_model):
        if class_name in known or class_name in allowed_new:
            continue
        issues.append(ContractIssue(
            path=f"classes.{class_name}", code="UNKNOWN_ENTITY_TYPE",
            message=f"class '{class_name}' is not a known or allowed new entity type",
        ))


def _canonical_model(model: dict) -> dict:
    classes = {}
    for class_name in sorted(model["classes"]):
        attributes = {}
        for attr_name in sorted(model["classes"][class_name]["attributes"]):
            definition = model["classes"][class_name]["attributes"][attr_name]
            entry = {"range": definition["_range"]}
            if definition.get("identifier") is True:
                entry["identifier"] = True
            if definition.get("required") is True:
                entry["required"] = True
            if definition.get("multivalued") is True:
                entry["multivalued"] = True
            description = definition.get("description")
            if isinstance(description, str):
                entry["description"] = description
            attributes[attr_name] = entry
        class_entry = {"attributes": attributes}
        parents = sorted(model["classes"][class_name]["is_a"])
        if parents:
            class_entry["is_a"] = parents
        classes[class_name] = class_entry

    enums = {
        enum_name: {"values": values}
        for enum_name, values in model["enums"].items() if values is not None
    }

    relations = {}
    for class_name in sorted(model["classes"]):
        targets = sorted({
            definition["_range"]
            for definition in model["classes"][class_name]["attributes"].values()
            if definition["_range"] in model["classes"]
        })
        if targets:
            relations[class_name] = targets

    return {"classes": classes, "enums": enums, "relations": relations}


def _build_report(issues: list[ContractIssue], model: dict | None) -> ValidationReport:
    issues = tuple(sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message)))
    counts: dict[str, int] = {"issues": len(issues)}
    if model is not None:
        counts["classes"] = len(model["classes"])
        counts["attributes"] = sum(len(class_model["attributes"]) for class_model in model["classes"].values())
        counts["enums"] = len(model["enums"])
        counts["relations"] = sum(
            1 for class_model in model["classes"].values()
            for definition in class_model["attributes"].values()
            if definition.get("_range") in model["classes"]
        )
    for issue in issues:
        counts[issue.code] = counts.get(issue.code, 0) + 1
    if issues or model is None:
        return ValidationReport(
            accepted=False, issues=issues, canonical_model_json=None, counts=counts,
        )
    canonical = json.dumps(
        _canonical_model(model), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    return ValidationReport(
        accepted=True, issues=issues, canonical_model_json=canonical, counts=counts,
    )
