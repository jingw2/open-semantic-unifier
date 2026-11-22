"""数据库会话管理与首次启动超管幂等初始化"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db_and_seed() -> None:
    """建表并幂等初始化默认超管、工作空间与默认示例项目"""
    from app.core.security import hash_password
    from app.models.entities import Project, User, Workspace, WorkspaceMember

    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == settings.init_admin_email).first()
        if not admin:
            admin = User(
                email=settings.init_admin_email,
                password_hash=hash_password(settings.init_admin_password),
                display_name=settings.init_admin_name,
                is_active=True,
            )
            db.add(admin)
            db.flush()

        ws = db.query(Workspace).filter(Workspace.slug == "default").first()
        if not ws:
            ws = Workspace(name=settings.init_workspace_name, slug="default")
            db.add(ws)
            db.flush()

        member = (
            db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == admin.id)
            .first()
        )
        if not member:
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=admin.id, role="admin"))

        proj = db.query(Project).filter(Project.workspace_id == ws.id, Project.slug == "enterprise-order-demo").first()
        if not proj:
            db.add(
                Project(
                    workspace_id=ws.id,
                    name="企业跨系统语义统一示范项目",
                    slug="enterprise-order-demo",
                    description="覆盖 ERP / WMS / OMS / BMS 四系统的端到端语义统一演示项目",
                )
            )
        db.commit()
    finally:
        db.close()
