"""ИКПУ и код упаковки в карточке товара — через HTTP.

    .venv\\Scripts\\python.exe tests/fiscal_fields_scenarios.py

Проверка формата, сохранение, что синхронизация с REGOS пустым не затирает
вписанное, а своим — заменяет, и кому из ролей это доступно.
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
})

from fastapi.testclient import TestClient  # noqa: E402

from app import admin_auth  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser, Category, Product, Variant  # noqa: E402
from app.regos.catalog_sync import apply_fiscal  # noqa: E402

init_db()
db = SessionLocal()
cat = Category(name="Деликатесы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Куриный казы", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="004866", weight="400 г", price=43990,
                  regos_price=43990, is_active=True)
db.add(variant)
for name, role in (("tov", "manager"), ("buh", "accountant")):
    db.add(AdminUser(username=name, role=role, password_hash=admin_auth.hash_password("pass-12345")))
db.commit()
vid, pid = variant.id, prod.id
db.close()

results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    c = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, c.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


ADMIN = init_data(1)
client = TestClient(app)


def put(body, headers=ADMIN, c=client):
    return c.patch(f"/api/admin/variants/{vid}/fiscal", json=body, headers=headers)


def card(headers=ADMIN):
    return client.get(f"/api/admin/products/{pid}", headers=headers).json()["variants"][0]


v = card()
check(1, "В карточке фасовки есть ИКПУ и код упаковки (пока пусто)",
      "mxik" in v and "package_code" in v and v["mxik"] is None, v)
check(2, "ИКПУ не из 17 цифр — нельзя", put({"mxik": "123", "package_code": ""}).status_code == 400)
check(2, "Код упаковки с буквами — нельзя",
      put({"mxik": "", "package_code": "12a"}).status_code == 400)
r = put({"mxik": " 01601001001036059 ", "package_code": "1403931"})
v = r.json()["variants"][0] if r.status_code == 200 else {}
check(3, "Сохранено (пробелы по краям убраны)", r.status_code == 200
      and v["mxik"] == "01601001001036059" and v["package_code"] == "1403931", r.text[:200])

db = SessionLocal()
row = db.get(Variant, vid)
apply_fiscal(row, {"icps": "", "package_code": None, "vat": {}})
db.commit()
check(4, "Синхронизация с пустыми кодами из REGOS вписанное не стирает",
      row.mxik == "01601001001036059" and row.package_code == "1403931")
apply_fiscal(row, {"icps": "01601001001036060", "package_code": "1500000",
                   "vat": {"value": 12, "enabled": True}})
db.commit()
db.close()
v = card()
check(4, "Заполненное в REGOS заменяет вписанное в панели",
      v["mxik"] == "01601001001036060" and v["package_code"] == "1500000" and v["vat_percent"] == 12, v)
check(5, "Пустые поля — коды очищаются", put({"mxik": "", "package_code": ""}).json()["variants"][0][
    "package_code"] is None)

tov = TestClient(app, base_url="https://testserver")
tov.post("/api/admin/login", json={"username": "tov", "password": "pass-12345"})
check(6, "Товаровед может вписать коды", put({"mxik": "01601001001036059", "package_code": "1"},
                                           headers={}, c=tov).status_code == 200)
buh = TestClient(app, base_url="https://testserver")
buh.post("/api/admin/login", json={"username": "buh", "password": "pass-12345"})
check(6, "Бухгалтеру карточки товаров недоступны",
      put({"mxik": "", "package_code": ""}, headers={}, c=buh).status_code == 403)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
