"""Serialize writes that change a project's published identity view."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Project


def lock_project_identity_state(db: Session, project_id: str) -> None:
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        raw_connection = connection.connection.driver_connection
        if not raw_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        return
    db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
