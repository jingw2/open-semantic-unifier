"""Versioned, project-owned configuration for semantic entity unification."""
from dataclasses import dataclass
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.entities import LLMProvider, Project
from app.models.unification import CatalogEntityVersion, EntityCatalogVersion, SourceRevision, UnificationSchemeVersion


class AttributePolicy(BaseModel):
    source_priority: list[str] = Field(default_factory=list)
    on_conflict: Literal["retain_provenance", "REVIEW", "priority"] = "retain_provenance"


class MatchScheme(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str
    identity_granularity: str
    field_map: dict[str, str]
    normalizers: list[str] = Field(default_factory=list)
    strong_identifiers: list[str] = Field(default_factory=list)
    conflict_constraints: list[dict[str, Any]] = Field(default_factory=list)
    candidate_rules: list[dict[str, Any]] = Field(default_factory=list)
    attribute_policies: dict[str, AttributePolicy] = Field(default_factory=dict)
    revision: int = 1
    identifier_authority: str | None = None
    nulls_never_equal: bool = True
    conflict_precedence: str | None = None
    weak_evidence_action: str | None = None
    golden_pair_discovery: str | None = None
    real_world_identifier_validation: str | None = None
    model_judgment_enabled: bool = False
    model_provider_id: str | None = None
    model_failure_policy: Literal["REVIEW", "FAIL"] = "REVIEW"
    prompt_version: str = "entity-pair-v1"

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_scheme_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        normalized = dict(values)
        if "strong_identifier" in normalized and "strong_identifiers" not in normalized:
            identifier = normalized["strong_identifier"]
            normalized["strong_identifiers"] = [identifier] if identifier else []
        normalized.pop("strong_identifier", None)
        if "normalization" in normalized and "normalizers" not in normalized:
            normalized["normalizers"] = normalized["normalization"]
        normalized.pop("normalization", None)
        if "conflict_fields" in normalized and "conflict_constraints" not in normalized:
            action = normalized.get("conflict_precedence", "veto_auto_merge")
            normalized["conflict_constraints"] = [
                {"field": field, "action": action} for field in normalized["conflict_fields"]
            ]
        normalized.pop("conflict_fields", None)
        if "scheme_version" in normalized and "revision" not in normalized:
            normalized["revision"] = int(normalized["scheme_version"])
        normalized.pop("scheme_version", None)
        if "canonical_attribute_policy" in normalized and "attribute_policies" not in normalized:
            normalized["attribute_policies"] = normalized["canonical_attribute_policy"]
        normalized.pop("canonical_attribute_policy", None)
        return normalized

    @field_validator("entity_type", "identity_granularity")
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be non-empty")
        return value.strip()

    @field_validator("field_map")
    @classmethod
    def validate_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("field_map must contain at least one canonical field")
        if any(not canonical.strip() or not source.strip() for canonical, source in value.items()):
            raise ValueError("field_map keys and source fields must be non-empty")
        return value

    @field_validator("normalizers")
    @classmethod
    def validate_normalizers(cls, value: list[str]) -> list[str]:
        supported = {"unicode_nfkc", "trim", "casefold", "digits_only", "strip_punctuation"}
        unknown = set(value) - supported
        if unknown:
            raise ValueError(f"unsupported normalizers: {sorted(unknown)}")
        return value

    @field_validator("candidate_rules")
    @classmethod
    def validate_candidate_rule_shapes(cls, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for rule in rules:
            kind = rule.get("kind")
            if kind in {"exact", "splink_blocking"}:
                fields = rule.get("fields")
                if not isinstance(fields, list) or not fields or any(not isinstance(field, str) or not field.strip() for field in fields):
                    raise ValueError(f"{kind} candidate rules require a non-empty fields list")
            elif kind == "name_similarity":
                field = rule.get("field")
                threshold = rule.get("threshold", 0.82)
                if not isinstance(field, str) or not field.strip():
                    raise ValueError("name_similarity candidate rules require a field")
                if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold <= 1:
                    raise ValueError("name_similarity threshold must be greater than 0 and at most 1")
            else:
                raise ValueError(f"unsupported candidate rule kind: {kind}")
        return rules

    @model_validator(mode="after")
    def validate_references(self) -> "MatchScheme":
        known_fields = set(self.field_map)
        if not set(self.strong_identifiers) <= known_fields:
            raise ValueError("strong_identifiers must refer to canonical fields in field_map")
        for constraint in self.conflict_constraints:
            if constraint.get("field") not in known_fields:
                raise ValueError("conflict constraint fields must refer to field_map")
            if constraint.get("action", "veto_auto_merge") != "veto_auto_merge":
                raise ValueError("conflict constraint action must be veto_auto_merge")
        for rule in self.candidate_rules:
            fields = rule.get("fields", []) if rule.get("kind") in {"exact", "splink_blocking"} else [rule.get("field")]
            if not set(fields) <= known_fields:
                raise ValueError("candidate rule fields must refer to field_map")
        if not set(self.attribute_policies) <= known_fields | {"other_attributes"}:
            raise ValueError("attribute_policies must refer to canonical fields")
        if self.model_judgment_enabled and not (self.model_provider_id or "").strip():
            raise ValueError("model_judgment_enabled requires model_provider_id")
        if not self.prompt_version.strip():
            raise ValueError("prompt_version must be non-empty")
        return self


class EntityFieldDefinition(BaseModel):
    type: Literal["string", "integer", "number", "boolean", "date", "datetime", "object", "array"]
    required: bool = False
    description: str = ""


class EntityDefinition(BaseModel):
    label: str
    description: str = ""
    identity_granularity: str
    attributes: dict[str, EntityFieldDefinition]
    revision: int = 1

    @field_validator("label", "identity_granularity")
    @classmethod
    def require_nonblank_definition(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be non-empty")
        return value.strip()

    @field_validator("attributes")
    @classmethod
    def require_attributes(cls, value: dict[str, EntityFieldDefinition]) -> dict[str, EntityFieldDefinition]:
        if not value or any(not name.strip() for name in value):
            raise ValueError("attributes must contain at least one named field")
        return value


class CatalogEntity(BaseModel):
    entity_type: str
    canonical_id: str
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    revision: int = 1

    @field_validator("entity_type", "canonical_id", "canonical_name")
    @classmethod
    def require_catalog_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be non-empty")
        return value.strip()

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, aliases: list[str]) -> list[str]:
        return list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))


@dataclass(frozen=True, slots=True)
class EntityTypeDefinition:
    key: str
    label: str
    default_granularity: str
    description: str


@dataclass(frozen=True, slots=True)
class AttributeOverride:
    field: str
    value: Any
    actor_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class AttributeSource:
    source_system: str
    source_revision_id: str
    value: Any
    actor_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AttributeConflict:
    field: str
    values: tuple[AttributeSource, ...]


@dataclass(frozen=True, slots=True)
class CanonicalAttributes:
    values: dict[str, Any]
    selected: dict[str, AttributeSource]
    provenance: dict[str, tuple[AttributeSource, ...]]
    conflicts: tuple[AttributeConflict, ...]
    requires_review: tuple[str, ...]


ENTITY_TYPE_CATALOG = (
    EntityTypeDefinition("organization", "Organization", "legal_entity", "An organization or business entity."),
    EntityTypeDefinition("person", "Person", "natural_person", "A natural person."),
    EntityTypeDefinition("product", "Product", "sellable_variant", "A product at a configurable identity grain."),
    EntityTypeDefinition("custom", "Custom entity", "user_defined", "A user-defined entity type and identity grain."),
)


def get_entity_type_catalog() -> tuple[EntityTypeDefinition, ...]:
    return ENTITY_TYPE_CATALOG


def save_scheme(db: Session, *, project_id: str, scheme_key: str, scheme: MatchScheme) -> MatchScheme:
    scheme_key = scheme_key.strip()
    if not scheme_key:
        raise ValueError("scheme_key must be non-empty")
    if scheme.model_provider_id:
        provider = db.scalar(select(LLMProvider).join(
            Project, Project.workspace_id == LLMProvider.workspace_id,
        ).where(
            Project.id == project_id,
            LLMProvider.id == scheme.model_provider_id,
        ))
        if provider is None:
            raise ValueError("model provider is not available in this project's workspace")
    for _attempt in range(3):
        latest_record = db.scalar(select(UnificationSchemeVersion).where(
            UnificationSchemeVersion.project_id == project_id,
            UnificationSchemeVersion.scheme_key == scheme_key,
        ).order_by(UnificationSchemeVersion.revision.desc()).limit(1))
        if latest_record:
            latest_scheme = MatchScheme.model_validate(latest_record.scheme_json)
            if latest_scheme.model_dump(exclude={"revision"}) == scheme.model_dump(exclude={"revision"}):
                return latest_scheme
            next_revision = latest_record.revision + 1
        else:
            next_revision = 1
        versioned = scheme.model_copy(update={"revision": next_revision})
        db.add(UnificationSchemeVersion(
            project_id=project_id,
            scheme_key=scheme_key,
            revision=versioned.revision,
            scheme_json=versioned.model_dump(mode="json"),
        ))
        try:
            db.commit()
            return versioned
        except (IntegrityError, OperationalError):
            db.rollback()
            time.sleep(0.01 * (_attempt + 1))
    raise RuntimeError("could not allocate a unique scheme revision after concurrent updates")


def list_schemes(db: Session, *, project_id: str) -> list[dict[str, Any]]:
    records = db.scalars(select(UnificationSchemeVersion).where(
        UnificationSchemeVersion.project_id == project_id
    ).order_by(UnificationSchemeVersion.scheme_key, UnificationSchemeVersion.revision.desc())).all()
    latest_by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.scheme_key not in latest_by_key:
            latest_by_key[record.scheme_key] = {**record.scheme_json, "scheme_key": record.scheme_key}
    return list(latest_by_key.values())


def get_scheme(db: Session, *, project_id: str, scheme_key: str, revision: int | None = None) -> MatchScheme | None:
    statement = select(UnificationSchemeVersion).where(
        UnificationSchemeVersion.project_id == project_id,
        UnificationSchemeVersion.scheme_key == scheme_key,
    )
    if revision is not None:
        statement = statement.where(UnificationSchemeVersion.revision == revision)
    else:
        statement = statement.order_by(UnificationSchemeVersion.revision.desc())
    record = db.scalar(statement.limit(1))
    return MatchScheme.model_validate(record.scheme_json) if record else None


def save_entity_definition(
    db: Session, *, project_id: str, catalog_key: str, definition: EntityDefinition
) -> EntityDefinition:
    catalog_key = catalog_key.strip()
    if not catalog_key:
        raise ValueError("catalog_key must be non-empty")
    for _attempt in range(3):
        latest = db.scalar(select(EntityCatalogVersion).where(
            EntityCatalogVersion.project_id == project_id,
            EntityCatalogVersion.catalog_key == catalog_key,
        ).order_by(EntityCatalogVersion.revision.desc()).limit(1))
        if latest:
            current = EntityDefinition.model_validate(latest.definition_json)
            if current.model_dump(exclude={"revision"}) == definition.model_dump(exclude={"revision"}):
                return current
            revision = latest.revision + 1
        else:
            revision = 1
        versioned = definition.model_copy(update={"revision": revision})
        db.add(EntityCatalogVersion(
            project_id=project_id,
            catalog_key=catalog_key,
            revision=revision,
            definition_json=versioned.model_dump(mode="json"),
        ))
        try:
            db.commit()
            return versioned
        except (IntegrityError, OperationalError):
            db.rollback()
            time.sleep(0.01 * (_attempt + 1))
    raise RuntimeError("could not allocate a unique entity catalog revision after concurrent updates")


def list_entity_definitions(db: Session, *, project_id: str) -> list[dict[str, Any]]:
    records = db.scalars(select(EntityCatalogVersion).where(
        EntityCatalogVersion.project_id == project_id
    ).order_by(EntityCatalogVersion.catalog_key, EntityCatalogVersion.revision.desc())).all()
    latest_by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.catalog_key not in latest_by_key:
            latest_by_key[record.catalog_key] = {**record.definition_json, "catalog_key": record.catalog_key}
    return list(latest_by_key.values())


def get_entity_definition(
    db: Session, *, project_id: str, catalog_key: str, revision: int | None = None
) -> EntityDefinition | None:
    statement = select(EntityCatalogVersion).where(
        EntityCatalogVersion.project_id == project_id,
        EntityCatalogVersion.catalog_key == catalog_key,
    )
    if revision is not None:
        statement = statement.where(EntityCatalogVersion.revision == revision)
    else:
        statement = statement.order_by(EntityCatalogVersion.revision.desc())
    record = db.scalar(statement.limit(1))
    return EntityDefinition.model_validate(record.definition_json) if record else None


def save_catalog_entity(
    db: Session, *, project_id: str, catalog_key: str, entity: CatalogEntity
) -> CatalogEntity:
    catalog_key = catalog_key.strip()
    if not catalog_key:
        raise ValueError("catalog_key must be non-empty")
    for _attempt in range(3):
        latest = db.scalar(select(CatalogEntityVersion).where(
            CatalogEntityVersion.project_id == project_id,
            CatalogEntityVersion.catalog_key == catalog_key,
        ).order_by(CatalogEntityVersion.revision.desc()).limit(1))
        if latest:
            current = CatalogEntity.model_validate({"attributes": latest.attributes, "entity_type": latest.entity_type,
                "canonical_id": latest.canonical_id, "canonical_name": latest.canonical_name,
                "aliases": latest.aliases, "revision": latest.revision})
            if current.model_dump(exclude={"revision"}) == entity.model_dump(exclude={"revision"}):
                return current
            revision = latest.revision + 1
        else:
            revision = 1
        versioned = entity.model_copy(update={"revision": revision})
        db.add(CatalogEntityVersion(
            project_id=project_id,
            catalog_key=catalog_key,
            revision=revision,
            entity_type=versioned.entity_type,
            canonical_id=versioned.canonical_id,
            canonical_name=versioned.canonical_name,
            aliases=versioned.aliases,
            attributes=versioned.attributes,
        ))
        try:
            db.commit()
            return versioned
        except (IntegrityError, OperationalError):
            db.rollback()
            time.sleep(0.01 * (_attempt + 1))
    raise RuntimeError("could not allocate a unique catalog entity revision after concurrent updates")


def list_catalog_entities(db: Session, *, project_id: str) -> list[dict[str, Any]]:
    records = db.scalars(select(CatalogEntityVersion).where(
        CatalogEntityVersion.project_id == project_id
    ).order_by(CatalogEntityVersion.catalog_key, CatalogEntityVersion.revision.desc())).all()
    latest_by_key: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.catalog_key not in latest_by_key:
            latest_by_key[record.catalog_key] = {
                "catalog_key": record.catalog_key,
                "entity_type": record.entity_type,
                "canonical_id": record.canonical_id,
                "canonical_name": record.canonical_name,
                "aliases": record.aliases,
                "attributes": record.attributes,
                "revision": record.revision,
            }
    return list(latest_by_key.values())


def get_catalog_entity(
    db: Session, *, project_id: str, catalog_key: str, revision: int | None = None
) -> CatalogEntity | None:
    statement = select(CatalogEntityVersion).where(
        CatalogEntityVersion.project_id == project_id,
        CatalogEntityVersion.catalog_key == catalog_key,
    )
    if revision is not None:
        statement = statement.where(CatalogEntityVersion.revision == revision)
    else:
        statement = statement.order_by(CatalogEntityVersion.revision.desc())
    record = db.scalar(statement.limit(1))
    if record is None:
        return None
    return CatalogEntity.model_validate({"attributes": record.attributes, "entity_type": record.entity_type,
        "canonical_id": record.canonical_id, "canonical_name": record.canonical_name,
        "aliases": record.aliases, "revision": record.revision})


def _value_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def select_attributes(
    members: list[SourceRevision],
    scheme: MatchScheme,
    overrides: list[AttributeOverride] | None = None,
) -> CanonicalAttributes:
    """Select values by configured source priority while retaining all source evidence."""
    overrides_by_field = {override.field: override for override in overrides or []}
    values: dict[str, Any] = {}
    selected: dict[str, AttributeSource] = {}
    provenance: dict[str, tuple[AttributeSource, ...]] = {}
    conflicts: list[AttributeConflict] = []
    requires_review: list[str] = []

    for canonical_field, source_field in scheme.field_map.items():
        candidates = [
            AttributeSource(
                source_system=member.source_record.source_system,
                source_revision_id=member.id,
                value=member.attributes[source_field],
            )
            for member in members
            if source_field in member.attributes and member.attributes[source_field] is not None
        ]
        policy = scheme.attribute_policies.get(canonical_field, scheme.attribute_policies.get("other_attributes", AttributePolicy()))
        priority = policy.source_priority
        candidates.sort(key=lambda candidate: (priority.index(candidate.source_system) if candidate.source_system in priority else len(priority), candidate.source_revision_id))
        provenance[canonical_field] = tuple(candidates)
        distinct = {_value_key(candidate.value) for candidate in candidates}
        if len(distinct) > 1:
            conflicts.append(AttributeConflict(canonical_field, tuple(candidates)))
        override = overrides_by_field.get(canonical_field)
        if override:
            chosen = AttributeSource("human_override", "", override.value, override.actor_id, override.reason)
        elif candidates and len(distinct) > 1 and policy.on_conflict == "REVIEW":
            requires_review.append(canonical_field)
            continue
        elif candidates:
            chosen = candidates[0]
        else:
            continue
        values[canonical_field] = chosen.value
        selected[canonical_field] = chosen

    mapped_source_fields = set(scheme.field_map.values())
    for source_field in sorted({key for member in members for key in member.attributes} - mapped_source_fields - set(scheme.field_map)):
        policy = scheme.attribute_policies.get(source_field, scheme.attribute_policies.get("other_attributes", AttributePolicy()))
        candidates = [
            AttributeSource(member.source_record.source_system, member.id, member.attributes[source_field])
            for member in members
            if source_field in member.attributes and member.attributes[source_field] is not None
        ]
        candidates.sort(key=lambda candidate: (policy.source_priority.index(candidate.source_system) if candidate.source_system in policy.source_priority else len(policy.source_priority), candidate.source_revision_id))
        provenance[source_field] = tuple(candidates)
        distinct = {_value_key(candidate.value) for candidate in candidates}
        if len(distinct) > 1:
            conflicts.append(AttributeConflict(source_field, tuple(candidates)))
        override = overrides_by_field.get(source_field)
        if override:
            chosen = AttributeSource("human_override", "", override.value, override.actor_id, override.reason)
        elif candidates and len(distinct) > 1 and policy.on_conflict == "REVIEW":
            requires_review.append(source_field)
            continue
        elif candidates:
            chosen = candidates[0]
        else:
            continue
        values[source_field] = chosen.value
        selected[source_field] = chosen

    return CanonicalAttributes(values, selected, provenance, tuple(conflicts), tuple(requires_review))
