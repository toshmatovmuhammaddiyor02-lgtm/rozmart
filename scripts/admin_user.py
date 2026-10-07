"""Заведение и изменение учётных записей панели.

Пароль вводится в терминале и не отображается, в аргументы командной строки
не принимается намеренно: аргументы видны в списке процессов любому
пользователю сервера и оседают в истории оболочки.

    .venv\\Scripts\\python.exe -m scripts.admin_user add ivan
    .venv\\Scripts\\python.exe -m scripts.admin_user passwd ivan
    .venv\\Scripts\\python.exe -m scripts.admin_user list
    .venv\\Scripts\\python.exe -m scripts.admin_user disable ivan
"""
import argparse
import getpass
import sys

from sqlalchemy import select

from app.admin_auth import MIN_PASSWORD, ROLES, drop_sessions, hash_password
from app.db import SessionLocal, init_db
from app.models import AdminUser


def ask_password() -> str | None:
    first = getpass.getpass("Пароль: ")
    if len(first) < MIN_PASSWORD:
        print(f"Пароль короче {MIN_PASSWORD} знаков — так нельзя.", file=sys.stderr)
        return None
    if first != getpass.getpass("Ещё раз: "):
        print("Пароли не совпали.", file=sys.stderr)
        return None
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="Учётные записи панели ROZMART")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("add", "passwd", "disable", "enable"):
        p = sub.add_parser(name)
        p.add_argument("username")
        if name == "add":
            p.add_argument("--name", default=None, help="имя для показа в панели")
            p.add_argument("--role", default="admin", choices=sorted(ROLES),
                           help="роль: что видно в панели (по умолчанию admin)")
    sub.add_parser("list")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.cmd == "list":
            rows = list(db.scalars(select(AdminUser).order_by(AdminUser.username)))
            if not rows:
                print("Учётных записей нет.")
            for u in rows:
                print(f"  {u.username:20} {'активна' if u.is_active else 'отключена':10} "
                      f"{u.role:11} {u.full_name or ''}")
            return 0

        username = args.username.strip().lower()
        user = db.scalar(select(AdminUser).where(AdminUser.username == username))

        if args.cmd == "add":
            if user is not None:
                print(f"Учётная запись {username} уже есть.", file=sys.stderr)
                return 1
            password = ask_password()
            if password is None:
                return 1
            db.add(AdminUser(username=username, password_hash=hash_password(password),
                             full_name=args.name, role=args.role))
            db.commit()
            print(f"Заведена учётная запись {username}.")
            return 0

        if user is None:
            print(f"Нет такой учётной записи: {username}", file=sys.stderr)
            return 1

        if args.cmd == "passwd":
            password = ask_password()
            if password is None:
                return 1
            user.password_hash = hash_password(password)
            db.commit()
            # старые сессии закрываем: иначе украденная кука работает и после
            # смены пароля, а меняют пароль обычно как раз поэтому
            closed = drop_sessions(db, username)
            print(f"Пароль изменён. Закрыто открытых сессий: {closed}.")
            return 0

        user.is_active = args.cmd == "enable"
        db.commit()
        if not user.is_active:
            drop_sessions(db, username)
        print(f"{username}: {'включена' if user.is_active else 'отключена'}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
