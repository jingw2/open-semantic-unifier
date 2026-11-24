"""Contract source ingestion: select a stored file DataSource as a contract input.

Only SourceRecord-level metadata is stored; no compiled model exists yet.
"""
import hashlib
import time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.contracts import ContractSource
from app.models.entities import DataSource
from app.services.storage import StorageError, storage

MAX_CONTRACT_SOURCE_BYTES = 5 * 1024 * 1024

CONTRACT_SOURCE_FORMATS = {
    ".yaml": "linkml_yaml",
    ".yml": "linkml_yaml",
    ".tsv": "schemasheet_tsv",
    ".csv": "schemasheet_csv",
}


class ContractSourceError(Exception):
    """Base class for contract source domain failures."""


class ContractSourceNotFound(ContractSourceError):
    """The referenced data source does not exist in this project."""


class ContractSourceTooLarge(ContractSourceError):
    """The stored content exceeds the size limit."""


class ContractSourceInvalid(ContractSourceError):
    """The stored content cannot be selected as a contract source."""


def _contract_format(filename: str) -> str | None:
    lowered = filename.casefold()
    for suffix, contract_format in CONTRACT_SOURCE_FORMATS.items():
        if lowered.endswith(suffix):
            return contract_format
    return None


def create_contract_source(db: Session, *, project_id: str, data_source_id: str, actor_id: str) -> ContractSource:
    data_source = db.scalar(select(DataSource).where(
        DataSource.id == data_source_id,
        DataSource.project_id == project_id,
    ))
    if data_source is None:
        raise ContractSourceNotFound("data source not found in this project")
    if data_source.source_type != "file" or not (data_source.object_key or "").strip():
        raise ContractSourceInvalid("contract sources require a file data source with stored content")
    filename = data_source.name
    contract_format = _contract_format(filename)
    if contract_format is None:
        raise ContractSourceInvalid("unsupported contract format; expected a .yaml, .yml, .tsv, or .csv file")
    try:
        content = storage.get_bytes(data_source.object_key)
    except StorageError as exc:
        raise ContractSourceInvalid("stored content is unreadable") from exc
    if not content:
        raise ContractSourceInvalid("stored content is empty")
    if len(content) > MAX_CONTRACT_SOURCE_BYTES:
        raise ContractSourceTooLarge(f"contract content exceeds the {MAX_CONTRACT_SOURCE_BYTES} byte limit")
    digest = hashlib.sha256(content).hexdigest()
    for attempt in range(3):
        existing = db.scalar(select(ContractSource).where(
            ContractSource.project_id == project_id,
            ContractSource.sha256 == digest,
        ))
        if existing is not None:
            return existing
        source = ContractSource(
            project_id=project_id,
            data_source_id=data_source_id,
            filename=filename,
            format=contract_format,
            sha256=digest,
            size_bytes=len(content),
            created_by=actor_id,
        )
        db.add(source)
        try:
            db.flush()
            return source
        except (IntegrityError, OperationalError):
            db.rollback()
            time.sleep(0.01 * (attempt + 1))
    raise ContractSourceError("could not create a unique contract source after concurrent updates")


def list_contract_sources(db: Session, *, project_id: str) -> list[ContractSource]:
    return list(db.scalars(select(ContractSource).where(
        ContractSource.project_id == project_id,
    ).order_by(ContractSource.created_at.desc(), ContractSource.id.desc())).all())
