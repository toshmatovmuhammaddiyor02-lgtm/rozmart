"""Пользователи панели: учётки, роли, пароли (раздел «Пользователи»).

Заводит и меняет учётки администратор — раздел «Пользователи» есть только
у роли «Администратор». Свой пароль меняет каждый сам (/me/password): для
этого нужен текущий пароль, чтобы оставленный открытым браузер не позволил
чужому человеку перехватить учётку.

Что важно:
  * пароль не хранится и не возвращается — только хеш (admin_auth);
  * смена пароля и отключение закрывают сессии человека: украденная или
    забытая на чужом компьютере кука перестаёт работать сразу. Своя текущая
    сессия при смене своего пароля остаётся — выкидывать человека незачем;
  * себя нельзя отключить или лишить роли администратора, и нельзя оставить
    панель без единого действующего администратора — иначе раздел
    «Пользователи» станет недоступен всем, а чинить это — только на сервере.
"""
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import admin_auth
from app.admin import require_admin
from app.db import get_db
from app.models import AdminSession, AdminUser
from app.telegram import TelegramUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Пользователи панели"])

LOGIN = re.compile(r"[a-z0-9][a-z0-9._-]{2,59}")


def _me(request: Request, db: Session) -> AdminUser | None:
    return admin_auth.session_user(request, db)


def _out(user: AdminUser, sessions: dict[str, int]) -> dict:
    role = user.role if user.role in admin_auth.ROLES else ""
    return {
        "username": user.username,
        "full_name": user.full_name or "",
        "role": role,
        "role_title": admin_auth.ROLES.get(role, {}).get("title", "нет роли"),
        "is_active": user.is_active,
        "created_at": user.created_at.strftime("%d.%m.%Y") if user.created_at else "",
        "sessions": sessions.get(user.username, 0),
    }


def _check_password(password: str) -> str:
    password = password or ""
    if len(password) < admin_auth.MIN_PASSWORD:
        raise HTTPException(status_code=400,
                            detail=f"Пароль — не короче {admin_auth.MIN_PASSWORD} символов")
    if len(password) > 200:
        raise HTTPException(status_code=400, detail="Слишком длинный пароль")
    return password


def _check_role(role: str) -> str:
    if role not in admin_auth.ROLES:
        raise HTTPException(status_code=400, detail="Неизвестная роль")
    return role


def _active_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(AdminUser).where(
        AdminUser.role == "admin", AdminUser.is_active.is_(True))) or 0


def _get(db: Session, username: str) -> AdminUser:
    user = db.get(AdminUser, (username or "").strip().lower())
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Учётки панели и роли — что каждая роль видит."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sessions = dict(db.execute(select(AdminSession.username, func.count()).where(
        AdminSession.expires_at > now).group_by(AdminSession.username)).all())
    users = db.scalars(select(AdminUser).order_by(AdminUser.is_active.desc(),
                                                  AdminUser.username)).all()
    return {
        "users": [_out(u, sessions) for u in users],
        "roles": [{"role": key, "title": r["title"],
                   "sections": [admin_auth.SECTIONS[s] for s in r["sections"]]}
                  for key, r in admin_auth.ROLES.items()],
        "min_password": admin_auth.MIN_PASSWORD,
    }


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=60)
    full_name: str = Field(default="", max_length=120)
    role: str
    password: str


@router.post("/users")
def create_user(data: UserIn, request: Request, db: Session = Depends(get_db),
                _: TelegramUser = Depends(require_admin)):
    username = data.username.strip().lower()
    if not LOGIN.fullmatch(username):
        raise HTTPException(status_code=400, detail="Логин — латиница, цифры, точка, дефис "
                                                    "или подчёркивание, от 3 символов")
    if db.get(AdminUser, username) is not None:
        raise HTTPException(status_code=409, detail="Такой логин уже есть")
    user = AdminUser(username=username, full_name=data.full_name.strip() or None,
                     role=_check_role(data.role),
                     password_hash=admin_auth.hash_password(_check_password(data.password)),
                     is_active=True)
    db.add(user)
    db.commit()
    log.info("Панель: заведён пользователь %s, роль %s (%s)", username, user.role, _who(request, db))
    return list_users(db, _)


class UserPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=120)
    role: str | None = None
    is_active: bool | None = None


@router.patch("/users/{username}")
def update_user(username: str, data: UserPatch, request: Request, db: Session = Depends(get_db),
                _: TelegramUser = Depends(require_admin)):
    user = _get(db, username)
    me = _me(request, db)
    myself = me is not None and me.username == user.username
    losing_admin = user.role == "admin" and user.is_active and (
        (data.role is not None and data.role != "admin") or data.is_active is False)
    if myself and losing_admin:
        raise HTTPException(status_code=400, detail="Себя нельзя отключить или лишить роли "
                                                    "администратора — попросите другого администратора")
    if losing_admin and _active_admins(db) <= 1:
        raise HTTPException(status_code=400, detail="Это последний администратор — "
                                                    "сначала назначьте другого")
    if data.full_name is not None:
        user.full_name = data.full_name.strip() or None
    if data.role is not None:
        user.role = _check_role(data.role)
    if data.is_active is not None:
        user.is_active = data.is_active
    db.commit()
    if data.is_active is False or data.role is not None:
        # отключённый — сразу за дверь; сменившему роль — заново войти,
        # чтобы панель перерисовалась под новые разделы
        admin_auth.drop_sessions(db, user.username)
    log.info("Панель: пользователь %s изменён: %s (%s)", user.username,
             data.model_dump(exclude_none=True), _who(request, db))
    return list_users(db, _)


class PasswordIn(BaseModel):
    password: str


@router.post("/users/{username}/password")
def reset_password(username: str, data: PasswordIn, request: Request,
                   db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Новый пароль пользователю. Его сессии закрываются."""
    user = _get(db, username)
    user.password_hash = admin_auth.hash_password(_check_password(data.password))
    db.commit()
    admin_auth.drop_sessions(db, user.username)
    log.info("Панель: пароль пользователя %s сменён администратором (%s)",
             user.username, _who(request, db))
    return {"ok": True}


class MyPasswordIn(BaseModel):
    current: str
    password: str


@router.post("/me/password")
def change_my_password(data: MyPasswordIn, request: Request, db: Session = Depends(get_db),
                       _: TelegramUser = Depends(require_admin)):
    """Свой пароль. Другие сессии закрываются, текущая остаётся."""
    me = _me(request, db)
    if me is None:
        raise HTTPException(status_code=400, detail="Вы вошли через Telegram — пароля у вас нет")
    if not admin_auth.verify_password(data.current or "", me.password_hash):
        raise HTTPException(status_code=400, detail="Текущий пароль неверный")
    if data.password == data.current:
        raise HTTPException(status_code=400, detail="Новый пароль совпадает со старым")
    me.password_hash = admin_auth.hash_password(_check_password(data.password))
    keep = request.cookies.get(admin_auth.COOKIE)
    for session in db.scalars(select(AdminSession).where(AdminSession.username == me.username)):
        if session.token != keep:
            db.delete(session)
    db.commit()
    log.info("Панель: %s сменил свой пароль", me.username)
    return {"ok": True}


def _who(request: Request, db: Session) -> str:
    me = _me(request, db)
    return f"сделал {me.username}" if me else "сделано через Telegram"
