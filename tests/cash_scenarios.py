"""Касса для заказов: выбор в панели и постановка заказа на кассу REGOS.

    .venv\\Scripts\\python.exe tests/cash_scenarios.py

REGOS подменён: на складе магазина (5) две кассы — 13 со сменами и 7 без,
у чужого склада своя касса. База — временный SQLite.
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
import app.regos.orders_push as orders_push  # noqa: E402
from app import admin_auth  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser, Category, Order, OrderItem, Product, Variant  # noqa: E402
from app.regos.client import RegosError  # noqa: E402

calls, fail = [], {}
next_doc = [100]


class FakeRegos:
    def call(self, method, params=None, **kw):
        calls.append((method, params))
        if method in fail:
            raise fail[method]
        if method == "OperatingCash/Get":
            return {"result": [
                {"id": 7, "stock": {"id": 5}, "description": ""},
                {"id": 13, "stock": {"id": 5}, "description": ""},
                {"id": 2, "stock": {"id": 1}, "description": ""},
            ]}
        if method == "DocCashSession/Get":
            cash = params["filters"][0]["Value"]
            if cash == "13":
                return {"result": [{"code": "11-0000532", "start_date": 1791429744, "closed": False,
                                    "close_date": 0, "start_user": {"full_name": "RDB РДБ"}}]}
            return {"result": []}
        if method == "DocOrderDelivery/Get":
            return []
        if method == "RetailCustomer/Get":
            return [{"id": 77}]
        if method == "DocOrderDelivery/AddFull":
            next_doc[0] += 1
            return {"new_id": next_doc[0]}
        return {"row_affected": 1}


orders_push.RegosClient = FakeRegos
regos_client.RegosClient = FakeRegos

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True)
db.add(variant); db.flush()


def new_order(number):
    order = Order(number=number, status="CONFIRMED", telegram_id=5, customer_name="Азиз",
                  phone="+998901234567", address="Ташкент", delivery_slot="Как можно скорее",
                  payment_method="cash", goods_total=39000, delivery_price=0, total=39000)
    db.add(order); db.flush()
    db.add(OrderItem(order_id=order.id, variant_id=variant.id, product_name="Докторская",
                     weight="400 г", price=39000, quantity=1))
    db.commit()
    return order.id


db.add(AdminUser(username="buh", role="accountant", password_hash=admin_auth.hash_password("pass-12345")))
db.commit()

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


def push(order_id):
    calls.clear()
    return client.post(f"/api/admin/orders/{order_id}/push", headers=ADMIN)


def methods():
    return [m for m, _ in calls]


# ------------------------------------------------------------ список касс
r = client.get("/api/admin/regos/cash", headers=ADMIN).json()
check(1, "По умолчанию — общая очередь (0)", r["selected"] == 0, r)
ids = [c["id"] for c in r["cashes"]]
check(1, "Только кассы склада магазина, рабочая — первой", ids == [13, 7], ids)
k13 = r["cashes"][0]["last_session"] or {}
check(1, "У кассы — последняя смена: номер с экрана и кассир",
      k13.get("code") == "11-0000532" and k13.get("cashier") == "RDB РДБ" and k13.get("closed") is False, k13)
check(1, "У кассы без смен — пусто (панель предупредит)", r["cashes"][1]["last_session"] is None)

# ------------------------------------------------------------ общая очередь — как было
oid = new_order("RB-9001")
r = push(oid)
check(2, "Общая очередь: заказ ушёл", r.status_code == 200, r.text[:200])
check(2, "…утверждён и кассу не назначали", "DocOrderDelivery/SetStatus" in methods()
      and "DocOrderDelivery/SetOperatingCash" not in methods(), methods())

# ------------------------------------------------------------ выбор кассы
check(3, "Чужая касса (другой склад) — нельзя", client.put(
    "/api/admin/regos/cash", json={"operating_cash_id": 2}, headers=ADMIN).status_code == 400)
check(3, "Отрицательный id — нельзя", client.put(
    "/api/admin/regos/cash", json={"operating_cash_id": -1}, headers=ADMIN).status_code == 422)
r = client.put("/api/admin/regos/cash", json={"operating_cash_id": 13}, headers=ADMIN)
check(3, "Касса 13 выбрана", r.status_code == 200
      and client.get("/api/admin/regos/cash", headers=ADMIN).json()["selected"] == 13, r.text)

oid = new_order("RB-9002")
r = push(oid)
order_calls = methods()
check(4, "Заказ ушёл", r.status_code == 200, r.text[:200])
cash_call = [p for m, p in calls if m == "DocOrderDelivery/SetOperatingCash"]
check(4, "Поставлен на кассу 13 — тем документом, что создан",
      cash_call == [{"id": 102, "operating_cash_id": 13}], cash_call)
check(4, "Порядок: создать → утвердить → касса (к неутверждённому REGOS кассу не привяжет)",
      order_calls.index("DocOrderDelivery/AddFull") < order_calls.index("DocOrderDelivery/SetStatus")
      < order_calls.index("DocOrderDelivery/SetOperatingCash"), order_calls)
check(4, "«Оплата (продажа)» не ставится",
      all(p.get("status") != "Paying" for m, p in calls if m == "DocOrderDelivery/SetStatus"))

# ------------------------------------------------------------ касса не назначилась
fail["DocOrderDelivery/SetOperatingCash"] = RegosError(1064, "the order document has not been confirmed yet",
                                                       "DocOrderDelivery/SetOperatingCash")
oid = new_order("RB-9003")
r = push(oid)
db.expire_all()
o = db.get(Order, oid)
check(5, "Касса не назначилась: документ сохранён (повтор не заведёт дубль)",
      o.regos_document_id == 103, o.regos_document_id)
check(5, "…а оператор видит, что заказ в общей очереди",
      r.status_code == 502 and "общей очереди" in (o.regos_error or ""), o.regos_error)
fail.clear()

# ------------------------------------------------------------ автоотправка тоже по выбору
from app.models import Setting  # noqa: E402
db.add(Setting(name=orders_push.SETTING_AUTO, value="1")); db.commit()
oid = new_order("RB-9004")
calls.clear()
orders_push.push_one(oid)
check(6, "Автоотправка (включена) ставит на выбранную кассу",
      [p["operating_cash_id"] for m, p in calls if m == "DocOrderDelivery/SetOperatingCash"] == [13],
      methods())

# ------------------------------------------------------------ обратно в общую очередь
client.put("/api/admin/regos/cash", json={"operating_cash_id": 0}, headers=ADMIN)
oid = new_order("RB-9005")
push(oid)
check(7, "Снова общая очередь — кассу не назначаем", "DocOrderDelivery/SetOperatingCash" not in methods())

# ------------------------------------------------------------ REGOS недоступен, доступ
fail["OperatingCash/Get"] = RuntimeError("timeout")
r = client.get("/api/admin/regos/cash", headers=ADMIN).json()
check(8, "REGOS не ответил — текущий выбор виден, ошибка тоже",
      r["selected"] == 0 and r["cashes"] == [] and "timeout" in r["error"], r)
check(8, "…выбрать кассу в это время нельзя (502), общую очередь — можно",
      client.put("/api/admin/regos/cash", json={"operating_cash_id": 13}, headers=ADMIN).status_code == 502
      and client.put("/api/admin/regos/cash", json={"operating_cash_id": 0}, headers=ADMIN).status_code == 200)
fail.clear()

buh = TestClient(app, base_url="https://testserver")
buh.post("/api/admin/login", json={"username": "buh", "password": "pass-12345"})
check(9, "Бухгалтеру кассу менять нельзя",
      buh.put("/api/admin/regos/cash", json={"operating_cash_id": 13}).status_code == 403)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
