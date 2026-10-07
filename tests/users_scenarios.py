"""Пользователи панели: роли, доступ к разделам, пароли — через HTTP.

    .venv\\Scripts\\python.exe tests/users_scenarios.py

База — временный SQLite. Выход с кодом 1, если хоть одна проверка не прошла.
"""
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import admin_auth  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser  # noqa: E402

init_db()
db = SessionLocal()
db.add(AdminUser(username="boss", full_name="Шохрух", password_hash=admin_auth.hash_password("boss-pass-123"),
                 is_active=True))
db.commit()
db.close()

results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def browser():
    # кука сессии — Secure: ходим по https, иначе клиент её не вернёт
    return TestClient(app, base_url="https://testserver")


def login(c, username, password):
    return c.post("/api/admin/login", json={"username": username, "password": password})


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check_ = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


# ------------------------------------------------------------ все адреса панели — в разделах
unmapped = []
for route in app.routes:
    path = getattr(route, "path", "")
    if not path.startswith("/api/admin") or path in ("/api/admin/login", "/api/admin/logout"):
        continue
    for method in getattr(route, "methods", []) or []:
        if admin_auth.section_of(method, path) == "users" and not path.startswith("/api/admin/users"):
            unmapped.append(f"{method} {path}")
check(0, "Каждый адрес панели отнесён к разделу (нет «забытых»)", not unmapped, unmapped)

boss = browser()
check(1, "Администратор входит", login(boss, "boss", "boss-pass-123").status_code == 200)
me = boss.get("/api/admin/whoami").json()
check(1, "Заведённый до ролей — администратор, видит все разделы",
      me["role"] == "admin" and set(me["sections"]) == set(admin_auth.SECTIONS), me)
r = boss.get("/api/admin/users").json()
check(1, "Список пользователей и ролей", [u["username"] for u in r["users"]] == ["boss"]
      and {x["role"] for x in r["roles"]} == {"admin", "manager", "accountant", "operator"})

# ------------------------------------------------------------ завести пользователя
new = {"username": "Tovaroved", "full_name": "Товаровед", "role": "manager", "password": "1234"}
check(2, "Пароль короче 5 символов — нельзя", boss.post("/api/admin/users", json=new).status_code == 400)
check(2, "Кривой логин — нельзя", boss.post("/api/admin/users", json={
    **new, "username": "то вар", "password": "x" * 12}).status_code == 400)
check(2, "Неизвестная роль — нельзя", boss.post("/api/admin/users", json={
    **new, "role": "god", "password": "x" * 12}).status_code == 400)
check(2, "Пароль из 5 символов — можно", boss.post("/api/admin/users", json={
    **new, "username": "five", "password": "12345", "role": "operator"}).status_code == 200)
r = boss.post("/api/admin/users", json={**new, "password": "Tovar-pass-2026"})
check(2, "Товаровед заведён (логин — строчными)", r.status_code == 200 and any(
    u["username"] == "tovaroved" and u["role"] == "manager" for u in r.json()["users"]), r.text[:200])
check(2, "Повтор логина — 409", boss.post("/api/admin/users", json={
    **new, "password": "Tovar-pass-2026"}).status_code == 409)
check(2, "Пароль в ответе не возвращается", "Tovar-pass-2026" not in r.text and "hash" not in r.text)

# ------------------------------------------------------------ доступ по роли
tv = browser()
check(3, "Товаровед входит", login(tv, "tovaroved", "Tovar-pass-2026").status_code == 200)
me = tv.get("/api/admin/whoami").json()
check(3, "Товаровед: товары, заказы, состав каталога, онлайн-платежи",
      me["sections"] == ["products", "orders", "settings", "report"] and me["role_title"] == "Товаровед", me)
check(3, "Товаровед видит заказы и отчёт", tv.get("/api/admin/orders").status_code == 200
      and tv.get("/api/admin/online-payments").status_code == 200)
check(3, "Товаровед меняет доставку (состав каталога)", tv.get("/api/admin/delivery-price").status_code == 200)
check(3, "Ключи касс товароведу недоступны", tv.get("/api/admin/payments").status_code == 403
      and tv.put("/api/admin/payments/config", json={"values": {"PAYME_KEY": "x" * 20}}).status_code == 403)
check(3, "Пользователи товароведу недоступны", tv.get("/api/admin/users").status_code == 403
      and tv.post("/api/admin/users", json={**new, "username": "hacker",
                                            "password": "x" * 12, "role": "admin"}).status_code == 403)

boss.post("/api/admin/users", json={"username": "buh", "role": "accountant", "password": "Buh-pass-20261"})
bh = browser()
login(bh, "buh", "Buh-pass-20261")
check(4, "Бухгалтер: заказы и отчёт, Excel тоже", bh.get("/api/admin/orders").status_code == 200
      and bh.get("/api/admin/online-payments.xlsx").status_code == 200)
check(4, "Бухгалтеру товары и каталог недоступны", bh.get("/api/admin/products").status_code == 403
      and bh.get("/api/admin/regos/groups").status_code == 403
      and bh.post("/api/admin/categories", json={"name": "X"}).status_code == 403)
check(4, "Список категорий (справочник) доступен всем вошедшим",
      bh.get("/api/admin/categories").status_code == 200)

# ------------------------------------------------------------ смена роли, пароль, отключение
r = boss.patch("/api/admin/users/tovaroved", json={"role": "operator"})
check(5, "Роль сменили — сессия товароведа закрыта, надо войти заново",
      r.status_code == 200 and tv.get("/api/admin/orders").status_code == 401)
login(tv, "tovaroved", "Tovar-pass-2026")
check(5, "Новая роль: оператору только заказы", tv.get("/api/admin/whoami").json()["sections"] == ["orders"]
      and tv.get("/api/admin/online-payments").status_code == 403)

r = boss.post("/api/admin/users/tovaroved/password", json={"password": "New-tovar-pass-1"})
check(6, "Администратор задал новый пароль — сессии закрыты", r.status_code == 200
      and tv.get("/api/admin/orders").status_code == 401)
check(6, "Старый пароль не подходит, новый — да", login(tv, "tovaroved", "Tovar-pass-2026").status_code == 401
      and login(tv, "tovaroved", "New-tovar-pass-1").status_code == 200)

r = boss.patch("/api/admin/users/tovaroved", json={"is_active": False})
check(7, "Отключён — выкинут и войти не может", r.status_code == 200
      and tv.get("/api/admin/orders").status_code == 401
      and login(tv, "tovaroved", "New-tovar-pass-1").status_code == 401)
boss.patch("/api/admin/users/tovaroved", json={"is_active": True})
check(7, "Включён снова — входит", login(tv, "tovaroved", "New-tovar-pass-1").status_code == 200)

# ------------------------------------------------------------ защита от самоблокировки
check(8, "Себя отключить нельзя", boss.patch("/api/admin/users/boss", json={"is_active": False}).status_code == 400)
check(8, "Себя лишить роли администратора нельзя",
      boss.patch("/api/admin/users/boss", json={"role": "manager"}).status_code == 400)
boss.post("/api/admin/users", json={"username": "admin2", "role": "admin", "password": "Admin2-pass-1"})
a2 = browser()
login(a2, "admin2", "Admin2-pass-1")
check(8, "Второй администратор может понизить первого",
      a2.patch("/api/admin/users/boss", json={"role": "manager"}).status_code == 200)
last = TestClient(app).patch("/api/admin/users/admin2", json={"role": "operator"},
                             headers=init_data(1))
check(8, "…а последнего администратора не понизит никто (даже из Telegram)",
      last.status_code == 400 and "последний" in last.json()["detail"], last.text)
login(boss, "boss", "boss-pass-123")
a2.patch("/api/admin/users/boss", json={"role": "admin"})
login(boss, "boss", "boss-pass-123")

# ------------------------------------------------------------ свой пароль
other = browser()
login(other, "buh", "Buh-pass-20261")
check(9, "Свой пароль: неверный текущий — нельзя", bh.post("/api/admin/me/password", json={
    "current": "wrong", "password": "Buh-new-pass-2026"}).status_code == 400)
check(9, "Свой пароль: короткий новый — нельзя", bh.post("/api/admin/me/password", json={
    "current": "Buh-pass-20261", "password": "123"}).status_code == 400)
r = bh.post("/api/admin/me/password", json={"current": "Buh-pass-20261", "password": "Buh-new-pass-2026"})
check(9, "Свой пароль сменён: здесь остаётся, на другом устройстве — выход",
      r.status_code == 200 and bh.get("/api/admin/orders").status_code == 200
      and other.get("/api/admin/orders").status_code == 401)
check(9, "Вход с новым паролем", login(browser(), "buh", "Buh-new-pass-2026").status_code == 200)

# ------------------------------------------------------------ Telegram-администратор
tg = TestClient(app)
me = tg.get("/api/admin/whoami", headers=init_data(1)).json()
check(10, "Администратор из Telegram — все разделы", set(me["sections"]) == set(admin_auth.SECTIONS))
check(10, "…и управляет пользователями", tg.get("/api/admin/users", headers=init_data(1)).status_code == 200)
check(10, "Чужой Telegram — не пускает", tg.get("/api/admin/users", headers=init_data(2)).status_code == 401)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
