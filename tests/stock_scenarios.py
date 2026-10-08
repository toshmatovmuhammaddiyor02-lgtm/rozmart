"""Склад витрины: выбор в панели, предпросмотр и куда после этого уходят заказы.

    .venv\\Scripts\\python.exe tests/stock_scenarios.py

REGOS подменён: склады RDB (5) и SAMPI (1), на SAMPI товар в наличии,
на RDB — нет. База — временный SQLite. Выход с кодом 1, если хоть одна
проверка не прошла.
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
import app.regos.orders_push as orders_push  # noqa: E402
from app import admin_auth  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser, Category, Order, OrderItem, Product, Variant  # noqa: E402
from app.regos import catalog_sync, config  # noqa: E402

calls = []
next_doc = [100]
# остаток по складам: на RDB (5) «Докторской» нет, на SAMPI (1) — есть
QTY = {"5": 0.0, "1": 12.0}


def filters_of(params):
    return {f["Field"]: f["Value"] for f in (params or {}).get("filters", [])}


class FakeRegos:
    def call(self, method, params=None, **kw):
        calls.append((method, params))
        if method == "Stock/Get":
            return {"result": [{"id": 1, "name": "СКЛАД SAMPI", "deleted_mark": False},
                               {"id": 5, "name": "СКЛАД RDB", "deleted_mark": False},
                               {"id": 3, "name": "СКЛАД BOCHKA", "deleted_mark": True}]}
        if method == "OperatingCash/Get":
            return {"result": [{"id": 2, "stock": {"id": 1}}, {"id": 1, "stock": {"id": 1}},
                               {"id": 13, "stock": {"id": 5}}, {"id": 7, "stock": {"id": 5}}]}
        if method == "DocCashSession/Get":
            return {"result": []}
        if method == "Item/GetExt":
            f = filters_of(params)
            return {"result": [{"item": {"code": 3100, "name": "RZ DOKTORSKAYA 400GR", "icps": "",
                                         "vat": {}},
                                "price": 39000.0,
                                "quantity": {"common": QTY.get(f["stock_id"], 0.0)}}]}
        if method == "DocOrderDelivery/Get":
            return []
        if method == "RetailCustomer/Get":
            return [{"id": 77}]
        if method == "DocOrderDelivery/AddFull":
            next_doc[0] += 1
            return {"new_id": next_doc[0]}
        return {"row_affected": 1}

    def paginate(self, method, params):
        calls.append((method, params))
        if method == "Item/GetExt":
            yield from self.call(method, params)["result"]
        else:
            yield {"code": 3100, "name": "RZ DOKTORSKAYA 400GR"}


orders_push.RegosClient = FakeRegos
regos_client.RegosClient = FakeRegos

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True)
db.add(variant); db.flush()
db.add(AdminUser(username="buh", role="accountant", password_hash=admin_auth.hash_password("pass-12345")))
db.commit()


def new_order(number):
    order = Order(number=number, status="CONFIRMED", telegram_id=5, customer_name="Азиз",
                  phone="+998901234567", address="Ташкент", delivery_slot="Как можно скорее",
                  payment_method="cash", goods_total=39000, delivery_price=0, total=39000)
    db.add(order); db.flush()
    db.add(OrderItem(order_id=order.id, variant_id=variant.id, product_name="Докторская",
                     weight="400 г", price=39000, quantity=1))
    db.commit()
    return order.id


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


def stock_of_push(order_id):
    calls.clear()
    r = client.post(f"/api/admin/orders/{order_id}/push", headers=ADMIN)
    docs = [p["document"]["stock_id"] for m, p in calls if m == "DocOrderDelivery/AddFull"]
    return r, docs


# ------------------------------------------------------------ список складов
r = client.get("/api/admin/regos/stocks", headers=ADMIN).json()
check(1, "Пока не выбран — склад из настроек сервера (RDB, 5)", r["current"] == 5, r)
check(1, "Склады с числом касс, удалённых нет",
      {s["id"]: s["cashes"] for s in r["stocks"]} == {1: 2, 5: 2}, r["stocks"])

r, docs = stock_of_push(new_order("RB-9101"))
check(2, "До переключения заказ уходит на RDB", r.status_code == 200 and docs == [5], docs)

# ------------------------------------------------------------ предпросмотр
client.put("/api/admin/regos/cash", json={"operating_cash_id": 13}, headers=ADMIN)
r = client.post("/api/admin/regos/stock?apply=false", json={"stock_id": 1}, headers=ADMIN).json()
check(3, "Предпросмотр: в наличии на RDB 0, на SAMPI 1 из 1",
      r["current"]["in_stock"] == 0 and r["new"]["in_stock"] == 1 and r["total"] == 1
      and r["new"]["name"] == "СКЛАД SAMPI", r)
check(3, "Предпросмотр ничего не меняет",
      r["applied"] is False and client.get("/api/admin/regos/stocks", headers=ADMIN).json()["current"] == 5)
r = client.post("/api/admin/regos/stock?apply=false", json={"stock_id": 5}, headers=ADMIN).json()
check(3, "Чего нет в наличии — перечислено (на RDB)", r["missing"] == ["Докторская 400 г"], r["missing"])
check(3, "Неизвестный склад — нельзя", client.post(
    "/api/admin/regos/stock?apply=true", json={"stock_id": 99}, headers=ADMIN).status_code == 400)
check(3, "Удалённый склад — нельзя", client.post(
    "/api/admin/regos/stock?apply=true", json={"stock_id": 3}, headers=ADMIN).status_code == 400)

# ------------------------------------------------------------ применение
r = client.post("/api/admin/regos/stock?apply=true", json={"stock_id": 1}, headers=ADMIN).json()
check(4, "Переключено на SAMPI", r["applied"] is True
      and client.get("/api/admin/regos/stocks", headers=ADMIN).json()["current"] == 1, r)
cash = client.get("/api/admin/regos/cash", headers=ADMIN).json()
check(4, "Касса RDB сброшена на общую очередь, в списке — кассы SAMPI",
      r["cash_reset"] is True and cash["selected"] == 0
      and sorted(c["id"] for c in cash["cashes"]) == [1, 2], cash)

r, docs = stock_of_push(new_order("RB-9102"))
check(5, "Заказ уходит на склад SAMPI", r.status_code == 200 and docs == [1], docs)
check(5, "Касса RDB (13) для SAMPI не принимается", client.put(
    "/api/admin/regos/cash", json={"operating_cash_id": 13}, headers=ADMIN).status_code == 400)
client.put("/api/admin/regos/cash", json={"operating_cash_id": 2}, headers=ADMIN)
r, docs = stock_of_push(new_order("RB-9103"))
check(5, "Касса SAMPI (2) выбрана — заказ на неё",
      [p["operating_cash_id"] for m, p in calls if m == "DocOrderDelivery/SetOperatingCash"] == [2]
      and docs == [1])

# ------------------------------------------------------------ синхронизация и цены
calls.clear()
db2 = SessionLocal()
catalog_sync.sync_catalog(db2, FakeRegos())
db2.close()
stocks_asked = {filters_of(p).get("stock_id") for m, p in calls if m == "Item/GetExt"}
check(6, "Обновление каталога спрашивает остатки и цены склада SAMPI", stocks_asked == {"1"}, stocks_asked)
calls.clear()
client.post("/api/admin/regos/price-type?apply=false", json={"price_type_id": 5}, headers=ADMIN)
asked = {filters_of(p).get("stock_id") for m, p in calls if m == "Item/GetExt"}
check(6, "Предпросмотр вида цены — тоже по складу SAMPI", asked <= {"1"}, asked)

db3 = SessionLocal()
check(7, "config.stock_id читает выбор из базы", config.stock_id(db3) == 1)
db3.close()

# ------------------------------------------------------------ группа без товаров
# Отмечена родительская группа, в выборку не попадает ничего — а цены
# витрины всё равно обновляются запросом по кодам
client.put("/api/admin/regos/groups", json={"group_ids": [109]}, headers=ADMIN)
real_paginate = FakeRegos.paginate


def empty_group(self, method, params):
    if method == "Item/GetExt" and "group_id" in filters_of(params):
        return iter([])
    return real_paginate(self, method, params)


FakeRegos.paginate = empty_group
real_call = FakeRegos.call


def new_price(self, method, params=None, **kw):
    r = real_call(self, method, params, **kw)
    if method == "Item/GetExt":
        for row in r["result"]:
            row["price"] = 41000.0
    return r


FakeRegos.call = new_price
db4 = SessionLocal()
report = catalog_sync.sync_catalog(db4, FakeRegos())
v = db4.get(Variant, variant.id)
check(9, "Группа без товаров: цена витрины всё равно обновилась",
      v.price == 41000 and report.updated == 1 and len(report.price_changes) == 1,
      (v.price, report.summary()))
check(9, "…и товар остался на витрине", v.is_active)
db4.close()
FakeRegos.paginate, FakeRegos.call = real_paginate, real_call

buh = TestClient(app, base_url="https://testserver")
buh.post("/api/admin/login", json={"username": "buh", "password": "pass-12345"})
check(8, "Бухгалтеру склад менять нельзя", buh.post(
    "/api/admin/regos/stock?apply=true", json={"stock_id": 5}).status_code == 403)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
