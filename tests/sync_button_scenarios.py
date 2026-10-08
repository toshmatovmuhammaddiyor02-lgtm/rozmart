"""Кнопка «Обновить из REGOS» в панели — через HTTP.

    .venv\\Scripts\\python.exe tests/sync_button_scenarios.py

REGOS подменён; пока «шлагбаум» закрыт, обновление висит — так видно,
что второе не запускается поверх первого. База — временный SQLite.
Выход с кодом 1, если хоть одна проверка не прошла.
"""
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "test-key",
    "REGOS_STOCK_ID": "5",
    "REGOS_PRICE_TYPE_ID": "5",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import admin_auth  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser, Category, Product, Variant  # noqa: E402
from app.regos import sync_run  # noqa: E402
from app.regos.client import RegosError  # noqa: E402

gate = threading.Event()
fail = {"on": False}


class FakeRegos:
    """В REGOS: 003106 подорожал, 003200 — новинка, 003107 удалён."""

    def paginate(self, method, payload):
        gate.wait(10)
        if fail["on"]:
            raise RegosError(1220, "тариф не оплачен", method)
        if method == "Item/GetExt":
            yield {"item": {"code": 3106, "name": "RZ BARBEKU 300GR", "icps": "", "vat": {}},
                   "price": 69000.0}
            yield {"item": {"code": 3200, "name": "RZ NOVINKA 400GR", "icps": "", "vat": {}},
                   "price": 41000.0}
        elif method == "Item/Get":
            yield {"code": 3106, "name": "RZ BARBEKU 300GR"}
            yield {"code": 3200, "name": "RZ NOVINKA 400GR"}
        else:
            raise AssertionError(method)

    def call(self, method, payload=None, **kw):
        return {"result": []}


sync_run.RegosClient = FakeRegos

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
for name, code in (("Барбекю", "003106"), ("Снятая в REGOS", "003107")):
    p = Product(category_id=cat.id, name=name, is_active=True); db.add(p); db.flush()
    db.add(Variant(product_id=p.id, external_code=code, weight="300 г",
                   price=65000, regos_price=65000, is_active=True))
db.add(AdminUser(username="buh", role="accountant", password_hash=admin_auth.hash_password("pass-12345")))
db.commit()
db.close()

results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Шохрух"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    c = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, c.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


ADMIN = init_data(1)
client = TestClient(app)


def state():
    return client.get("/api/admin/regos/sync", headers=ADMIN).json()


def wait_done():
    for _ in range(100):
        s = state()
        if not s["running"]:
            return s
        time.sleep(0.05)
    return state()


s = state()
check(1, "До первого запуска: не идёт, итога нет, «каждые 30 минут»",
      s == {"running": False, "last": None, "every_minutes": 30}, s)

r = client.post("/api/admin/regos/sync", headers=ADMIN)
check(2, "Кнопка: запуск принят (202)", r.status_code == 202, r.text)
time.sleep(0.1)
check(2, "Пока REGOS отвечает — «идёт»", state()["running"] is True)
r = client.post("/api/admin/regos/sync", headers=ADMIN)
check(3, "Второе нажатие во время обновления — 409, поверх не запускается",
      r.status_code == 409 and "уже идёт" in r.json()["detail"], r.text)
try:
    with sync_run.exclusive():
        pass
    busy = False
except sync_run.Busy:
    busy = True
check(3, "Таймер в это время тоже не пройдёт (Busy)", busy)

gate.set()
s = wait_done()
last = s["last"] or {}
check(4, "Итог записан: кто запустил и что изменилось",
      last.get("ok") is True and last.get("source") == "вручную · Шохрух"
      and last.get("price_changed") == 1 and last.get("created") == 1 and last.get("hidden") == 1, last)
check(4, "Список изменений цены — для подсказки в панели",
      last.get("price_changes") == [{"name": "RZ BARBEKU 300GR", "was": 65000, "now": 69000}],
      last.get("price_changes"))

db = SessionLocal()
by_code = {v.external_code: v for v in db.query(Variant)}
check(5, "Цена в базе обновилась", by_code["003106"].price == 69000)
check(5, "Новинка заведена скрытой", "003200" in by_code and not by_code["003200"].is_active
      and not by_code["003200"].product.is_active)
check(5, "Удалённая в REGOS снята с витрины", not by_code["003107"].is_active)
db.close()

fail["on"] = True
check(6, "Сбой REGOS: запуск принят", client.post("/api/admin/regos/sync", headers=ADMIN).status_code == 202)
last = wait_done()["last"]
check(6, "…и в итоге — текст ошибки, а не тишина",
      last["ok"] is False and "тариф не оплачен" in last["error"], last)
check(6, "После сбоя кнопку можно нажать снова", state()["running"] is False)
fail["on"] = False

result = sync_run.run("по расписанию")
check(7, "Запуск таймера пишет итог «по расписанию»",
      state()["last"]["source"] == "по расписанию" and result["ok"])

buh = TestClient(app, base_url="https://testserver")
buh.post("/api/admin/login", json={"username": "buh", "password": "pass-12345"})
check(8, "Бухгалтеру обновлять каталог нельзя",
      buh.post("/api/admin/regos/sync").status_code == 403
      and buh.get("/api/admin/regos/sync").status_code == 403)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
