"""认证登录与私有化团队 RBAC 成员管理（默认关闭公开自助注册）"""

from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_role
from app.core.config import settings
from app.core.db import get_db
from app.core.security import create_access_token, hash_password, verify_password
from app.models.entities import User, Workspace, WorkspaceMember

router = APIRouter(prefix="/api/v1/auth", tags=["Auth & Team RBAC"])


class LoginRequest(BaseModel):
    email: str
    password: str


class CreateMemberRequest(BaseModel):
    email: str
    password: str
    display_name: str
    role: str = "editor"  # admin | editor | viewer


class UpdateRoleRequest(BaseModel):
    role: str


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.strip().lower()).first()
    if not user or not user.is_active or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="邮箱或密码错误，或账号已被禁用。")
    member = db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user.id).first()
    if not member:
        raise HTTPException(status_code=403, detail="当前账号尚未加入任何工作空间。")
    ws = db.query(Workspace).filter(Workspace.id == member.workspace_id).first()
    token = create_access_token(user.id, user.email, member.role, member.workspace_id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": member.role,
            "workspace_id": member.workspace_id,
            "workspace_name": ws.name if ws else "默认工作空间",
        },
    }


@router.post("/register")
def register_disabled():
    if not settings.allow_public_signup:
        raise HTTPException(status_code=403, detail="私有化部署默认关闭公开注册，请联系管理员在团队管理中创建账号。")
    raise HTTPException(status_code=501, detail="请由管理员分配账号。")


@router.get("/me")
def me(ctx: Dict[str, str] = Depends(get_current_user)):
    return ctx


@router.get("/members")
def list_members(ctx: Dict[str, str] = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(WorkspaceMember, User)
        .join(User, WorkspaceMember.user_id == User.id)
        .filter(WorkspaceMember.workspace_id == ctx["workspace_id"])
        .all()
    )
    return [
        {
            "member_id": m.id,
            "user_id": u.id,
            "email": u.email,
            "display_name": u.display_name,
            "role": m.role,
            "is_active": u.is_active,
        }
        for m, u in rows
    ]


@router.post("/members")
def create_member(
    req: CreateMemberRequest,
    ctx: Dict[str, str] = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    if req.role not in ("admin", "editor", "viewer"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin / editor / viewer")
    email = req.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail=f"邮箱已存在: {email}")
    u = User(email=email, password_hash=hash_password(req.password), display_name=req.display_name.strip(), is_active=True)
    db.add(u)
    db.flush()
    m = WorkspaceMember(workspace_id=ctx["workspace_id"], user_id=u.id, role=req.role)
    db.add(m)
    db.commit()
    return {"user_id": u.id, "email": u.email, "display_name": u.display_name, "role": m.role}


@router.patch("/members/{member_id}")
def update_member_role(
    member_id: str,
    req: UpdateRoleRequest,
    ctx: Dict[str, str] = Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    if req.role not in ("admin", "editor", "viewer"):
        raise HTTPException(status_code=400, detail="角色仅支持 admin / editor / viewer")
    m = db.query(WorkspaceMember).filter(WorkspaceMember.id == member_id, WorkspaceMember.workspace_id == ctx["workspace_id"]).first()
    if not m:
        raise HTTPException(status_code=404, detail="成员不存在")
    m.role = req.role
    db.commit()
    return {"member_id": m.id, "role": m.role}
