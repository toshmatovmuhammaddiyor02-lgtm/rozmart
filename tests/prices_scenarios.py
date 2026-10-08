"""Своя цена товара и вид цены REGOS — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/prices_scenarios.py

REGOS подменён: цены по видам — таблица ниже. База — временный SQLite.
Выход с кодом 1, если хоть одна проверка не прошла.
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
    "REGOS_KEY": "test-key",
    "REGOS_STOCK_ID": "5",
    "REGOS_PRICE_TYPE_ID": "5",
    "REGOS_ORDER_FROM_ID": "2",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.client as regos_client  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Order, Product, Setting, Variant  # noqa: E402
from app.regos import catalog_sync, prices  # noqa: E402
from app.regos.orders_push import build_payload  # noqa: E402

# Цены по видам: вид -> код -> цена. 5 — RDB (сейчас на витрине), 1 — SAMPI.
# В SAMPI у 003107 цены нет — при переключении он должен пропасть с витрины.
TABLE = {
    5: {"003106": 65000, "003107": 65000, "003100": 36000},
    1: {"003106": 57000, "003100": 35000},
}
TYPES = [{"id": 1, "name": "Розничная цена (SAMPI)"}, {"id": 5, "name": "Розничная цена (RDB)"}]
regos_calls = []


class FakeRegos:
    def call(self, method, payload):
        regos_calls.append((method, payload))
        if method == "PriceType/Get":
            return {"result": TYPES}
        if method == "Item/GetExt":
            filters = {f["Field"]: f["Value"] for f in payload["filters"]}
            table = TABLE.get(int(filters["price_type_id"]), {})
            codes = [f"{int(c):06d}" for c in filters["code"].split(",")]
            return {"result": [{"item": {"code": int(c), "name": c}, "price": float(table[c])}
                               for c in codes if c in table]}
        raise AssertionError(f"неожиданный вызов {method}")


regos_client.RegosClient = FakeRegos

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
p1 = Product(category_id=cat.id, name="Барбекю Бараньи", is_active=True)
p2 = Product(category_id=cat.id, name="Барбекю Сырные", is_active=True)
p3 = Product(category_id=cat.id, name="Докторская", is_active=True)
db.add_all([p1, p2, p3]); db.flush()
v1 = Variant(product_id=p1.id, external_code="003106", weight="300 г",
             price=65000, regos_price=65000, is_active=True)
v2 = Variant(product_id=p2.id, external_code="003107", weight="300 г",
             price=65000, regos_price=65000, is_active=True)
v3 = Variant(product_id=p3.id, external_code="003100", weight="400 г",
             price=36000, regos_price=36000, is_active=True)
db.add_all([v1, v2, v3]); db.commit()

client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Тест"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


ADMIN, BUYER = init_data(1), init_data(900)
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + detail) if detail else ''}")


def fresh(variant_id):
    db.expire_all()
    return db.get(Variant, variant_id)


def storefront_price(product_id):
    rows = client.get("/api/products").json()
    return next((r["min_price"] for r in rows if r["id"] == product_id), None)


# ------------------------------------------------------------ своя цена
r = client.patch(f"/api/admin/variants/{v1.id}/price", json={"price": 59990}, headers=ADMIN)
v = fresh(v1.id)
check(1, "Своя цена сохраняется и сразу на витрине",
      r.status_code == 200 and v.price == 59990 and v.manual_price == 59990
      and v.regos_price == 65000 and storefront_price(p1.id) == 59990)

row = next(x for x in r.json()["variants"] if x["id"] == v1.id)
check(1, "Панель видит обе цены: свою и из REGOS",
      row["manual_price"] == 59990 and row["regos_price"] == 65000 and row["price"] == 59990)

# синхронизация приносит новую цену REGOS — своя остаётся
prices.apply_regos_price(fresh(v1.id), 67000); db.commit()
v = fresh(v1.id)
check(2, "Синхронизация обновила цену REGOS, но своя цена осталась",
      v.regos_price == 67000 and v.price == 59990)

r = client.patch(f"/api/admin/variants/{v1.id}/price", json={"price": None}, headers=ADMIN)
v = fresh(v1.id)
check(3, "«Вернуть цену REGOS» — на витрине снова цена REGOS",
      r.status_code == 200 and v.manual_price is None and v.price == 67000)

for bad in (0, -5, 100_000_000):
    r = client.patch(f"/api/admin/variants/{v1.id}/price", json={"price": bad}, headers=ADMIN)
    check(4, f"Цена {bad} отклонена", r.status_code == 422)

r = client.patch(f"/api/admin/variants/{v1.id}/price", json={"price": 50000}, headers=BUYER)
check(4, "Покупатель менять цену не может", r.status_code in (401, 404))

# своя цена делает продаваемой фасовку, у которой в REGOS цены нет
prices.apply_regos_price(fresh(v2.id), None); db.commit()
check(5, "Без цены в REGOS фасовка снята с продажи", fresh(v2.id).is_active is False)
client.patch(f"/api/admin/variants/{v2.id}/price", json={"price": 60000}, headers=ADMIN)
check(5, "Своя цена возвращает её на витрину",
      fresh(v2.id).is_active is True and storefront_price(p2.id) == 60000)
client.patch(f"/api/admin/variants/{v2.id}/price", json={"price": None}, headers=ADMIN)
prices.apply_regos_price(fresh(v2.id), 65000); db.commit()

# ------------------------------------------------------------ заказ по своей цене
client.patch(f"/api/admin/variants/{v3.id}/price", json={"price": 33000}, headers=ADMIN)
body = {"customer_name": "Жавохир", "phone": "+998901234567", "address": "Чиланзар 9",
        "delivery_slot": "Как можно скорее", "payment_method": "cash", "consent": True,
        "client_key": "price-key-001", "items": [{"variant_id": v3.id, "quantity": 2}]}
r = client.post("/api/orders", json=body, headers=BUYER)
o = r.json()
check(6, "Заказ считается по своей цене", r.status_code == 201
      and o["items"][0]["price"] == 33000 and o["goods_total"] == 66000)
order = db.query(Order).filter_by(number=o["number"]).one()
check(6, "Заказ запомнил вид цены, по которому считался", order.price_type_id == 5)

# ------------------------------------------------------------ вид цены
r = client.get("/api/admin/regos/price-types", headers=ADMIN)
check(7, "Панель получает виды цен и текущий",
      r.status_code == 200 and r.json()["current"] == 5 and len(r.json()["types"]) == 2)

regos_calls.clear()
r = client.post("/api/admin/regos/price-type?apply=false", json={"price_type_id": 1},
                headers=ADMIN)
st = r.json()["stats"]
check(8, "Предпросмотр: что подешевеет, что пропадёт, своя цена не меняется",
      r.status_code == 200 and st == {"total": 3, "same": 1, "up": 0, "down": 1,
                                      "lost": 1, "manual": 1}, str(st))
check(8, "Предпросмотр ничего не меняет",
      fresh(v1.id).price == 67000 and db.get(Setting, prices.SETTING) is None)
check(8, "Цены спрашиваются только по товарам витрины, одним запросом",
      sum(1 for m, _ in regos_calls if m == "Item/GetExt") == 1)

r = client.post("/api/admin/regos/price-type?apply=true", json={"price_type_id": 1},
                headers=ADMIN)
check(9, "Применение: цены SAMPI на витрине",
      r.status_code == 200 and fresh(v1.id).price == 57000 and storefront_price(p1.id) == 57000)
check(9, "Товар без цены в SAMPI пропал с витрины",
      fresh(v2.id).is_active is False and storefront_price(p2.id) is None)
check(9, "Своя цена пережила переключение",
      fresh(v3.id).price == 33000 and fresh(v3.id).regos_price == 35000)
check(9, "Выбор сохранён", prices.current(SessionLocal()) == 1)

r = client.post("/api/admin/regos/price-type?apply=true", json={"price_type_id": 99},
                headers=ADMIN)
check(10, "Несуществующий вид цены отклонён", r.status_code == 400)

# ------------------------------------------------------------ документ REGOS
payload = build_payload(order, {v3.id: "003100"})["document"]
check(11, "Старый заказ уходит в REGOS со своим видом цены, а не с новым",
      payload["price_type_id"] == 5)
r = client.post("/api/orders", json={**body, "client_key": "price-key-002"}, headers=BUYER)
new_order = db.query(Order).filter_by(number=r.json()["number"]).one()
check(11, "Новый заказ — с новым видом цены", new_order.price_type_id == 1
      and build_payload(new_order, {v3.id: "003100"})["document"]["price_type_id"] == 1)

# ------------------------------------------------------------ синхронизация
seen = []
real_fetch = catalog_sync.fetch_items


def spy(client_, groups=None, price_type_id=None, stock_id=None):
    seen.append(price_type_id)
    return []


catalog_sync.fetch_items = spy
catalog_sync.sync_catalog(SessionLocal(), FakeRegos(), with_barcodes=False)
catalog_sync.fetch_items = real_fetch
check(12, "Синхронизация каталога берёт выбранный в панели вид цены", seen == [1], str(seen))

# ------------------------------------------------------------ миграция
with engine.begin() as conn:
    from sqlalchemy import text
    conn.execute(text("UPDATE variants SET regos_price = NULL WHERE id = :id"), {"id": v1.id})
from app import migrate  # noqa: E402
migrate.apply()
check(13, "Миграция переносит старую цену в «цену REGOS»",
      fresh(v1.id).regos_price == fresh(v1.id).price == 57000)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
