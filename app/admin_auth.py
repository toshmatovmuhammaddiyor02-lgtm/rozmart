"""Вход в панель по логину и паролю.

Раньше единственным способом попасть в панель был Telegram: подпись уже
проверялась для витрины, и отдельных паролей заводить не требовалось. Вход по
паролю добавлен по требованию заказчика — с ним панель открывается в обычном
браузере, с компьютера, а не только с телефона.

Оба способа работают одновременно: администратор из ADMIN_TELEGRAM_IDS входит
подписью Telegram, остальные — логином.

Что здесь важно:

  * пароль не хранится. В базе лежит PBKDF2-HMAC-SHA256 со случайной солью;
    по такому значению исходный пароль не восстановить;
  * сравнение хешей и токенов идёт compare_digest, а не «==»: обычное сравнение
    прекращается на первом несовпавшем байте и по времени ответа выдаёт,
    сколько символов угадано;
  * сессия — случайный токен в базе, а не подписанная кука. Так её можно
    отозвать: смена пароля закрывает все открытые сессии этого пользователя;
  * кука HttpOnly (недоступна скриптам на странице), Secure (не уходит по HTTP)
    и SameSite=Lax (не отправляется с чужих сайтов).
"""
import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AdminSession, AdminUser

COOKIE = "rozmart_admin"
SESSION_HOURS = 12

# PBKDF2 с таким числом итераций считается доли секунды на вход и делает
# перебор украденной базы дорогим. Число хранится в самой строке хеша,
# поэтому его можно поднять позже, не ломая уже заведённые пароли.
ITERATIONS = 240_000
# Короткий пароль подбирать долго не дают: после FAIL_LIMIT неудач вход
# по этому логину закрыт на FAIL_WINDOW
MIN_PASSWORD = 5

# Защита от перебора. Счётчик в памяти процесса: переживать перезапуск ему
# незачем, а общей базы на один процесс не требуется.
FAIL_LIMIT = 7
FAIL_WINDOW = 15 * 60
_failures: dict[str, list[float]] = {}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), digest_hex)


def _too_many_failures(login: str) -> bool:
    now = time.time()
    attempts = [t for t in _failures.get(login, []) if now - t < FAIL_WINDOW]
    _failures[login] = attempts
    return len(attempts) >= FAIL_LIMIT


def _note_failure(login: str) -> None:
    _failures.setdefault(login, []).append(time.time())


def login(db: Session, response: Response, username: str, password: str) -> AdminUser:
    """Проверяет пару и заводит сессию. При неудаче не уточняет, что именно
    не сошлось: иначе по ответу подбирают существующие логины."""
    username = (username or "").strip().lower()
    if _too_many_failures(username):
        raise HTTPException(
            status_code=429,
            detail="Слишком много попыток. Подождите пятнадцать минут.",
        )

    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    ok = user is not None and user.is_active and verify_password(password, user.password_hash)
    if not ok:
        _note_failure(username)
        # ответ одинаков и по времени, и по тексту: PBKDF2 считается в любом
        # случае, иначе несуществующий логин отвечал бы заметно быстрее
        if user is None:
            hash_password(password)
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    _failures.pop(username, None)
    token = secrets.token_urlsafe(32)
    db.add(AdminSession(
        token=token,
        username=user.username,
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(hours=SESSION_HOURS),
    ))
    db.commit()

    response.set_cookie(
        COOKIE, token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        # на разработке сайт открыт по http, и кука с Secure не сохранится
        secure=os.getenv("TELEGRAM_AUTH_DISABLED") != "1",
        samesite="lax",
        path="/",
    )
    return user


def logout(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        session = db.get(AdminSession, token)
        if session is not None:
            db.delete(session)
            db.commit()
    response.delete_cookie(COOKIE, path="/")


def session_user(
    request: Request,
    db: Session = Depends(get_db),
) -> AdminUser | None:
    """Администратор, вошедший по паролю. None — куки нет или она протухла."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    session = db.get(AdminSession, token)
    if session is None:
        return None
    if session.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        db.delete(session)
        db.commit()
        return None
    user = db.scalar(select(AdminUser).where(AdminUser.username == session.username))
    return user if user is not None and user.is_active else None


def drop_sessions(db: Session, username: str) -> int:
    """Закрывает все сессии пользователя. Вызывается при смене пароля:
    иначе украденная кука продолжает работать после смены."""
    sessions = list(db.scalars(select(AdminSession).where(AdminSession.username == username)))
    for session in sessions:
        db.delete(session)
    db.commit()
    return len(sessions)


def purge_expired(db: Session) -> int:
    """Чистка просроченных сессий — таблица иначе растёт без конца."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = list(db.scalars(select(AdminSession).where(AdminSession.expires_at < now)))
    for row in rows:
        db.delete(row)
    db.commit()
    return len(rows)


# ---------------------------------------------------------------- роли

# Разделы панели. Каждый адрес /api/admin/... относится к одному из них
# (section_of), и роль пользователя решает, в какие разделы ему можно.
SECTIONS = {
    "products": "Товары",
    "orders": "Заказы",
    "settings": "Состав каталога",
    "payments": "Онлайн оплата",
    "report": "Онлайн-платежи",
    "users": "Пользователи",
}

# «Онлайн оплата» (ключи касс) и «Пользователи» — только администратору:
# ключ кассы — это деньги, а учётки — это доступ ко всему остальному
ROLES = {
    "admin": {"title": "Администратор", "sections": list(SECTIONS)},
    "manager": {"title": "Товаровед", "sections": ["products", "orders", "settings", "report"]},
    "accountant": {"title": "Бухгалтер", "sections": ["orders", "report"]},
    "operator": {"title": "Оператор заказов", "sections": ["orders"]},
}

# Адрес -> раздел. Порядок важен: /online-payments раньше /payments.
# None — доступно любому вошедшему (кто я, справочники для отрисовки).
_RULES = [
    ("/online-payments", "report"),
    ("/payments", "payments"),
    ("/users", "users"),
    ("/me", None),
    ("/whoami", None),
    ("/statuses", None),
    ("/orders", "orders"),
    ("/products", "products"),
    ("/variants", "products"),
    ("/regos", "settings"),
    ("/notify", "settings"),
    ("/couriers", "settings"),
    ("/delivery-price", "settings"),
    ("/delivery-zones", "settings"),
    ("/cleanup", "settings"),
]


def section_of(method: str, path: str) -> str | None:
    """Раздел, к которому относится запрос. Незнакомый адрес — «users»:
    то есть только администратору. Новый метод, который забыли сюда
    вписать, закрыт, а не открыт всем."""
    rest = path.split("/api/admin", 1)[-1] or "/"
    if rest.startswith("/categories"):
        # список категорий нужен и товарам, и составу каталога; менять — каталог
        return None if method == "GET" else "settings"
    for prefix, section in _RULES:
        if rest == prefix or rest.startswith(prefix + "/") or rest.startswith(prefix + "."):
            return section
    return "users"


def sections_of(role: str | None) -> list[str]:
    return list((ROLES.get(role or "") or {"sections": []})["sections"])


def allowed(role: str | None, method: str, path: str) -> bool:
    section = section_of(method, path)
    return section is None or section in sections_of(role)
