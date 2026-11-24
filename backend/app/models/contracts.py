"""Contract sources selected from stored file data sources for L3 model work."""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.entities import _uuid


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ContractSource(Base):
    __tablename__ = "contract_sources"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_contract_source_project_sha256"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False)
    data_source_id: Mapped[str] = mapped_column(String(32), ForeignKey("data_sources.id", ondelete="RESTRICT"), index=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(240), nullable=False)
    format: Mapped[str] = mapped_column(String(24), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
