"""Immutable contract model versions, the published pointer, audit, and exports.

ContractModelVersion rows are written once and never updated in their
canonical/artifact/provenance fields. publish_contract_version and
rollback_contract_version own their transaction and commit on success; the API
layer must not commit again after calling them. create_contract_version only
flushes and leaves the commit to the caller.
"""
import hashlib
import time

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models.contracts import ContractAuditRecord, ContractModelVersion, ContractReleaseState, ContractSource
from app.models.entities import DataSource
from app.models.unification import UnificationSnapshot
from app.services.contracts.compiler import ContractCompilerError, compile_contract
from app.services.contracts.sources import ContractSourceNotFound
from app.services.contracts.validation import ContractIssue, ValidationReport, validate_contract_model
from app.services.storage import StorageError, storage
from app.services.unification.locking import lock_project_identity_state
from app.services.unification.telemetry import current_trace_id

VERSION_ALLOCATION_ATTEMPTS = 3

EXPORT_KINDS = ("model", "json_schema", "sql_ddl", "er_diagram")


class ContractReleasesError(Exception):
    """Base class for contract release domain failures."""


class ContractVersionNotFound(ContractReleasesError):
    """The referenced contract model version does not exist in this project."""


class ContractSnapshotNotFound(ContractReleasesError):
    """The referenced entity snapshot does not exist in this project."""


class ContractVersionInvalid(ContractReleasesError):
    """Compilation or validation rejected the contract input."""

    def __init__(self, message: str, *, report: ValidationReport | None = None,
                 issues: tuple[ContractIssue, ...] = ()):
        super().__init__(message)
        self.report = report
        self.issues = tuple(report.issues if report is not None else issues)


class ContractReleaseConflict(ContractReleasesError):
    """The release pointer moved, or the target cannot be published/rolled back."""


class ContractSourceUnavailable(ContractReleasesError):
    """Stored contract content could not be read."""


class ContractSourceInvalid(ContractReleasesError):
    """Stored contract content no longer matches its source record."""


class ContractSourceHashMismatch(ContractSourceInvalid):
    """Stored bytes no longer hash to the selected source's sha256."""


def create_contract_version(
    db: Session, *, project_id: str, source_id: str, snapshot_id: str,
    allowed_new_entity_types: list[str] | None, actor_id: str,
) -> ContractModelVersion:
    """Compile and validate a contract source against an entity snapshot as a DRAFT version."""
    source = db.scalar(select(ContractSource).where(
        ContractSource.id == source_id,
        ContractSource.project_id == project_id,
    ))
    if source is None:
        raise ContractSourceNotFound("contract source not found in this project")
    data_source = db.scalar(select(DataSource).where(
        DataSource.id == source.data_source_id,
        DataSource.project_id == project_id,
    ))
    if data_source is None:
        raise ContractSourceInvalid("contract source data source is no longer available")
    try:
        content = storage.get_bytes(data_source.object_key)
    except StorageError as exc:
        raise ContractSourceUnavailable("stored contract content is unreadable") from exc
    if hashlib.sha256(content).hexdigest() != source.sha256:
        raise ContractSourceHashMismatch("stored content no longer matches the contract source hash")
    snapshot = db.scalar(select(UnificationSnapshot).where(
        UnificationSnapshot.id == snapshot_id,
        UnificationSnapshot.project_id == project_id,
    ))
    if snapshot is None:
        raise ContractSnapshotNotFound("entity snapshot not found in this project")
    known_entity_types = {
        details["entity_type"]
        for details in (snapshot.snapshot_json or {}).get("entities", {}).values()
        if isinstance(details, dict) and details.get("entity_type")
    }
    try:
        compiled = compile_contract(filename=source.filename, content=content)
    except ContractCompilerError as exc:
        raise ContractVersionInvalid(
            f"contract compilation failed: {exc}",
            issues=(ContractIssue(path="", code="COMPILE_FAILED", message=str(exc)),),
        ) from exc
    report = validate_contract_model(
        linkml_yaml=compiled.linkml_yaml,
        known_entity_types=known_entity_types,
        allowed_new_entity_types=set(allowed_new_entity_types or []),
    )
    if not report.accepted:
        raise ContractVersionInvalid("contract model validation failed", report=report)

    for _attempt in range(VERSION_ALLOCATION_ATTEMPTS):
        next_version = (db.scalar(select(func.max(ContractModelVersion.version)).where(
            ContractModelVersion.project_id == project_id,
        )) or 0) + 1
        version = ContractModelVersion(
            project_id=project_id, source_id=source_id, version=next_version, status="DRAFT",
            source_sha256=source.sha256, canonical_model_json=report.canonical_model_json,
            artifacts_json={
                "linkml_yaml": compiled.linkml_yaml,
                "sql_ddl": compiled.sql_ddl,
                "json_schema": compiled.json_schema,
                "er_diagram": compiled.er_diagram,
            },
            compiler_versions_json=dict(compiled.compiler_versions),
            validation_json={"counts": dict(report.counts), "issues": []},
            entity_snapshot_id=snapshot.id, snapshot_version=snapshot.version,
            created_by=actor_id,
        )
        db.add(version)
        try:
            db.flush()
            return version
        except (IntegrityError, OperationalError):
            db.rollback()
            time.sleep(0.01 * (_attempt + 1))
    raise ContractReleasesError("could not allocate a unique contract version after concurrent updates")


def publish_contract_version(
    db: Session, *, project_id: str, version_id: str,
    expected_current_version: int | None, reason: str, actor_id: str,
) -> ContractReleaseState:
    """Move the published pointer to version_id and audit the transition.

    Owns the transaction and commits on success; the caller must not commit.
    Idempotent when version_id is already current.
    """
    lock_project_identity_state(db, project_id)
    version = db.scalar(select(ContractModelVersion).where(
        ContractModelVersion.id == version_id,
        ContractModelVersion.project_id == project_id,
    ))
    if version is None:
        raise ContractVersionNotFound("contract version not found in this project")
    state = db.scalar(select(ContractReleaseState).where(
        ContractReleaseState.project_id == project_id,
    ).with_for_update())
    current_version = state.current_version if state else 0
    if state is not None and state.current_version_id == version_id:
        return state
    if version.status == "ARCHIVED":
        raise ContractReleaseConflict("cannot publish an archived contract version; use rollback")
    expected = 0 if expected_current_version is None else expected_current_version
    if current_version != expected:
        raise ContractReleaseConflict(f"current contract version is {current_version}, expected {expected}")
    if state is None:
        state = ContractReleaseState(
            project_id=project_id, current_version_id=version_id, current_version=version.version,
        )
        db.add(state)
        try:
            db.flush()
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            raise ContractReleaseConflict("contract publication conflicted with a concurrent publish") from exc
        from_version = None
    else:
        from_version = state.current_version
        previous = db.get(ContractModelVersion, state.current_version_id)
        if previous is not None:
            previous.status = "ARCHIVED"
        updated = db.execute(update(ContractReleaseState).where(
            ContractReleaseState.project_id == project_id,
            ContractReleaseState.current_version == from_version,
        ).values(current_version_id=version_id, current_version=version.version))
        if updated.rowcount != 1:
            db.rollback()
            raise ContractReleaseConflict("contract publication conflicted with a concurrent publish")
    version.status = "PUBLISHED"
    db.add(ContractAuditRecord(
        project_id=project_id, version_id=version_id, action="PUBLISH",
        actor_id=actor_id, reason=reason, from_version=from_version,
        to_version=version.version, trace_id=current_trace_id(),
    ))
    db.commit()
    db.refresh(state)
    return state


def rollback_contract_version(
    db: Session, *, project_id: str, version_id: str,
    expected_current_version: int, reason: str, actor_id: str,
) -> ContractReleaseState:
    """Move the published pointer back to an existing historical version.

    Owns the transaction and commits on success; the caller must not commit.
    Historical canonical and artifact fields are never updated. Idempotent when
    version_id is already current.
    """
    lock_project_identity_state(db, project_id)
    target = db.scalar(select(ContractModelVersion).where(
        ContractModelVersion.id == version_id,
        ContractModelVersion.project_id == project_id,
    ))
    if target is None:
        raise ContractVersionNotFound("contract version not found in this project")
    state = db.scalar(select(ContractReleaseState).where(
        ContractReleaseState.project_id == project_id,
    ).with_for_update())
    if state is None or state.current_version_id is None:
        raise ContractReleaseConflict("project has no current contract version")
    if state.current_version_id == version_id:
        return state
    if state.current_version != expected_current_version:
        raise ContractReleaseConflict(
            f"current contract version is {state.current_version}, expected {expected_current_version}"
        )
    if target.status == "DRAFT":
        raise ContractReleaseConflict("cannot roll back to a draft contract version")
    from_version = state.current_version
    previous = db.get(ContractModelVersion, state.current_version_id)
    if previous is not None:
        previous.status = "ARCHIVED"
    updated = db.execute(update(ContractReleaseState).where(
        ContractReleaseState.project_id == project_id,
        ContractReleaseState.current_version == expected_current_version,
    ).values(current_version_id=version_id, current_version=target.version))
    if updated.rowcount != 1:
        db.rollback()
        raise ContractReleaseConflict("contract rollback conflicted with a concurrent publish")
    target.status = "PUBLISHED"
    db.add(ContractAuditRecord(
        project_id=project_id, version_id=version_id, action="ROLLBACK",
        actor_id=actor_id, reason=reason, from_version=from_version,
        to_version=target.version, trace_id=current_trace_id(),
    ))
    db.commit()
    db.refresh(state)
    return state


def get_contract_release(db: Session, *, project_id: str) -> ContractReleaseState | None:
    return db.get(ContractReleaseState, project_id)


def list_contract_audit(
    db: Session, *, project_id: str, limit: int, offset: int,
) -> tuple[list[ContractAuditRecord], int]:
    total = db.scalar(select(func.count()).select_from(ContractAuditRecord).where(
        ContractAuditRecord.project_id == project_id,
    )) or 0
    rows = list(db.scalars(select(ContractAuditRecord).where(
        ContractAuditRecord.project_id == project_id,
    ).order_by(
        ContractAuditRecord.created_at.desc(), ContractAuditRecord.id.desc(),
    ).offset(offset).limit(limit)).all())
    return rows, total


def get_contract_version(db: Session, *, project_id: str, version_id: str) -> ContractModelVersion | None:
    return db.scalar(select(ContractModelVersion).where(
        ContractModelVersion.id == version_id,
        ContractModelVersion.project_id == project_id,
    ))


def list_contract_versions(
    db: Session, *, project_id: str, limit: int, offset: int,
) -> tuple[list[ContractModelVersion], int]:
    total = db.scalar(select(func.count()).select_from(ContractModelVersion).where(
        ContractModelVersion.project_id == project_id,
    )) or 0
    rows = list(db.scalars(select(ContractModelVersion).where(
        ContractModelVersion.project_id == project_id,
    ).order_by(
        ContractModelVersion.version.desc(), ContractModelVersion.id.desc(),
    ).offset(offset).limit(limit)).all())
    return rows, total


def export_contract_version(version: ContractModelVersion, kind: str) -> tuple[str, str]:
    """Return (content, media_type) for one of the version's stored outputs.

    Content always comes from the requested version row, never from the current
    release pointer.
    """
    if kind == "model":
        return version.canonical_model_json, "application/json"
    if kind == "json_schema":
        return version.artifacts_json["json_schema"], "application/json"
    if kind == "sql_ddl":
        return version.artifacts_json["sql_ddl"], "text/plain"
    if kind == "er_diagram":
        return version.artifacts_json["er_diagram"], "text/plain"
    raise ContractVersionInvalid(f"unknown export kind: {kind}")
