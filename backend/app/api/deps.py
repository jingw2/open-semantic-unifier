"""FastAPI 认证、RBAC 角色权限与项目 Serving API Key 依赖注入"""

from typing import Dict

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import decode_access_token, hash_api_key
from app.models.entities import Project, ProjectApiKey, User, WorkspaceMember


def get_current_user(
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Dict[str, str]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token，请先登录。")
    token = authorization.split(" ", 1)[1].strip()
    payload = decode_access_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="登录状态已过期或无效，请重新登录。")

    user = db.query(User).filter(User.id == payload["sub"], User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=401, detail="账号不存在或已被禁用。")
    member = (
        db.query(WorkspaceMember)
        .filter(WorkspaceMember.user_id == user.id, WorkspaceMember.workspace_id == payload["workspace_id"])
        .first()
    )
    if not member:
        raise HTTPException(status_code=403, detail="无权访问当前工作空间。")
    return {
        "user_id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "workspace_id": member.workspace_id,
        "role": member.role,
    }


def require_role(*allowed_roles: str):
    def _dep(ctx: Dict[str, str] = Depends(get_current_user)) -> Dict[str, str]:
        if ctx["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail=f"权限不足：需要 {' / '.join(allowed_roles)} 角色。")
        return ctx

    return _dep


def verify_serving_api_key(
    project_slug: str,
    x_api_key: str = Header(default="", alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Project:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少请求头 X-API-Key。")
    proj = db.query(Project).filter(Project.slug == project_slug).first()
    if not proj:
        raise HTTPException(status_code=404, detail=f"项目不存在: {project_slug}")
    kh = hash_api_key(x_api_key.strip())
    rec = (
        db.query(ProjectApiKey)
        .filter(ProjectApiKey.project_id == proj.id, ProjectApiKey.key_hash == kh, ProjectApiKey.is_active.is_(True))
        .first()
    )
    if not rec:
        raise HTTPException(status_code=401, detail="无效或已吊销的项目 API Key。")
    return proj
