"""Read-only, versioned OpenRefine recipes selected from a data profile."""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECIPE_DIR = Path(__file__).with_name("recipes")

MAX_ROWS = 50_000
MAX_BYTES = 10 * 1024 * 1024
MAX_COLUMNS = 256

_REQUIRED_KEYS = ("id", "name", "description", "version", "priority", "match", "limits", "operations")
_NAME_TERMS = ("company", "supplier", "customer", "name", "公司", "供应商", "客户", "名称", "姓名")
_CONTACT_TERMS = ("contact", "phone", "mobile", "email", "contact_info", "联系方式", "电话", "邮箱")
_PLACEHOLDER = re.compile(r"\$\{([a-z_]+)\}")
_SEPARATORS = (";", "；", ",", "，", "、", "|")


class RecipeCatalogError(Exception):
    """A built-in recipe file is invalid or a placeholder cannot be resolved."""


class RecipeColumnMissing(RecipeCatalogError):
    """The profile does not contain a column the recipe needs."""


@dataclass(frozen=True)
class Recipe:
    id: str
    name: str
    description: str
    version: str
    priority: int
    match: dict[str, Any]
    limits: dict[str, int]
    operations: tuple[dict[str, Any], ...]


def load_recipes() -> list[Recipe]:
    recipes: list[Recipe] = []
    for path in sorted(RECIPE_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipeCatalogError(f"recipe {path.name} is not valid JSON") from exc
        missing = [key for key in _REQUIRED_KEYS if key not in payload]
        if missing:
            raise RecipeCatalogError(f"recipe {path.name} is missing {', '.join(missing)}")
        operations = payload["operations"]
        if not isinstance(operations, list) or not operations or not all(isinstance(op, dict) for op in operations):
            raise RecipeCatalogError(f"recipe {path.name} requires a non-empty operations list")
        recipes.append(Recipe(
            id=str(payload["id"]),
            name=str(payload["name"]),
            description=str(payload["description"]),
            version=str(payload["version"]),
            priority=int(payload["priority"]),
            match=dict(payload["match"]),
            limits=dict(payload["limits"]),
            operations=tuple(copy.deepcopy(operations)),
        ))
    ids = [recipe.id for recipe in recipes]
    if len(ids) != len(set(ids)):
        raise RecipeCatalogError("recipe ids must be unique")
    return sorted(recipes, key=lambda recipe: (recipe.priority, recipe.id))


def recipe_payload(recipe: Recipe) -> dict[str, Any]:
    return {
        "id": recipe.id,
        "name": recipe.name,
        "description": recipe.description,
        "version": recipe.version,
        "priority": recipe.priority,
        "limits": dict(recipe.limits),
    }


def _columns(profile: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    for table in profile.get("tables") or []:
        for column in table.get("columns") or []:
            if isinstance(column, str) and column not in columns:
                columns.append(column)
    return columns


def _find_column(profile: dict[str, Any], terms: tuple[str, ...]) -> str | None:
    for column in _columns(profile):
        lowered = column.casefold()
        if any(term in lowered for term in terms):
            return column
    return None


def _name_column(profile: dict[str, Any]) -> str | None:
    keys = profile.get("detected_keys") or {}
    candidate = keys.get("name_col")
    if isinstance(candidate, str) and candidate:
        return candidate
    return _find_column(profile, _NAME_TERMS)


def _contact_column(profile: dict[str, Any]) -> str | None:
    for table in profile.get("tables") or []:
        preview = table.get("preview") or []
        for column in table.get("columns") or []:
            if not any(term in column.casefold() for term in _CONTACT_TERMS):
                continue
            if any(isinstance(row.get(column), str) and any(sep in row[column] for sep in _SEPARATORS) for row in preview if isinstance(row, dict)):
                return column
    return None


def _column_values(profile: dict[str, Any], column: str) -> list[str]:
    values: list[str] = []
    for table in profile.get("tables") or []:
        for row in table.get("preview") or []:
            if isinstance(row, dict) and isinstance(row.get(column), str):
                values.append(row[column])
    return values


def _split_capable(profile: dict[str, Any]) -> bool:
    contact = _contact_column(profile)
    return bool(contact and any(any(sep in value for sep in _SEPARATORS) for value in _column_values(profile, contact)))


def _merge_capable(profile: dict[str, Any]) -> bool:
    lowered = {column.casefold() for column in _columns(profile)}
    return "first_name" in lowered and "last_name" in lowered


def _renderable(recipe: Recipe, profile: dict[str, Any]) -> bool:
    try:
        render_operations(recipe, profile)
    except RecipeColumnMissing:
        return False
    return True


def select_recipe(profile: dict[str, Any], user_goal: str | None = None) -> Recipe | None:
    recipes = load_recipes()
    by_id = {recipe.id: recipe for recipe in recipes}
    goal = (user_goal or "").casefold()

    if any(term in goal for term in ("拆分", "split")) and _split_capable(profile):
        return by_id.get("split_contact_column")
    if any(term in goal for term in ("合并", "merge", "拼接")) and _merge_capable(profile):
        return by_id.get("merge_name_columns")
    if any(term in goal for term in ("后缀", "suffix")) and _name_column(profile):
        return by_id.get("remove_legal_suffix")
    if any(term in goal for term in ("标点", "punctuation")) and _name_column(profile):
        return by_id.get("normalize_punctuation")
    if any(term in goal for term in ("空格", "空白", "whitespace")) and _name_column(profile):
        return by_id.get("normalize_whitespace")

    if _split_capable(profile):
        return by_id.get("split_contact_column")
    if _merge_capable(profile):
        return by_id.get("merge_name_columns")
    if _name_column(profile):
        return by_id.get("normalize_company_name")
    return None


def _context(profile: dict[str, Any]) -> dict[str, str]:
    name = _name_column(profile)
    contact = _contact_column(profile)
    lowered = {column.casefold(): column for column in _columns(profile)}
    context = {
        "name_col": name or "",
        "contact_col": contact or "",
        "first_name_col": lowered.get("first_name", ""),
        "last_name_col": lowered.get("last_name", ""),
    }
    return context


def _substitute(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            key = match.group(1)
            resolved = context.get(key, "")
            if not resolved:
                raise RecipeColumnMissing(f"recipe requires column {key}")
            return resolved

        return _PLACEHOLDER.sub(replace, value)
    if isinstance(value, list):
        return [_substitute(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, context) for key, item in value.items()}
    return value


def render_operations(recipe: Recipe, profile: dict[str, Any]) -> list[dict[str, Any]]:
    context = _context(profile)
    operations = _substitute([copy.deepcopy(op) for op in recipe.operations], context)
    if not all(isinstance(op, dict) for op in operations):
        raise RecipeCatalogError("rendered operations must be objects")
    return operations


def openrefine_limits_ok(profile: dict[str, Any], size_bytes: int) -> tuple[bool, str | None]:
    total_rows = int(profile.get("total_rows") or 0)
    table_count = int(profile.get("table_count") or 0)
    if total_rows > MAX_ROWS:
        return False, f"OpenRefine accepts at most {MAX_ROWS:,} rows; this source has {total_rows:,}."
    if table_count > 1:
        return False, "OpenRefine only accepts a single table; this source has more than one."
    widest = max((int(table.get("cols") or len(table.get("columns") or [])) for table in profile.get("tables") or []), default=0)
    if widest > MAX_COLUMNS:
        return False, f"OpenRefine accepts at most {MAX_COLUMNS} columns; this table has {widest}."
    if size_bytes > MAX_BYTES:
        return False, "OpenRefine accepts files up to 10 MiB; this file is larger."
    return True, None
