"""Typed import boundaries and result contracts for source records."""
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class IngestMode(StrEnum):
    SYNC = "sync"
    EXPLORATION = "exploration"


@dataclass(frozen=True, slots=True)
class SourceKey:
    project_id: str
    source_system: str
    dataset: str
    source_record_id: str

    def __post_init__(self) -> None:
        for field in ("project_id", "source_system", "dataset", "source_record_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
            normalized = value.strip()
            if field in {"source_system", "dataset"}:
                normalized = normalized.casefold()
            object.__setattr__(self, field, normalized)


@dataclass(frozen=True, slots=True)
class SourceRecordInput:
    project_id: str
    source_system: str
    dataset: str
    source_record_id: str | None
    attributes: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class IngestIssue:
    index: int
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class IngestIdentity:
    source_key: SourceKey
    source_record_id: str
    entity_id: str
    source_revision_id: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    input_version: str
    identities: tuple[IngestIdentity, ...]
    source_revision_ids: tuple[str, ...]
    identity_guarantee: str
    limitations: tuple[str, ...]
    created_records: int
    created_entities: int
    created_revisions: int
    errors: tuple[IngestIssue, ...]
