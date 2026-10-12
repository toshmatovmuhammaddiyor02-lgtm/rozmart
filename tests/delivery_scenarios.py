"""Стоимость доставки из панели — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/delivery_scenarios.py

База — временный SQLite, REGOS не вызывается. Выход с кодом 1, если хоть
одна проверка не прошла.
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

import app.regos.orders_push as orders_push  # noqa: E402
from app import payments  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Order, Product, Variant  # noqa: E402
from app.payme import receipt  # noqa: E402

orders_push.push_one = lambda order_id: None   # в REGOS не ходим

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=36000,
                  regos_price=36000, is_active=True)
db.add(variant); db.commit()

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
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def order(key, expected=None):
    body = {"customer_name": "Жавохир", "phone": "+998901234567", "address": "Чиланзар 9",
            "delivery_slot": "Как можно скорее", "payment_method": "cash", "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": 2}]}
    if expected is not None:
        body["expected_total"] = expected
    return client.post("/api/orders", json=body, headers=BUYER)


def public_price():
    return client.get("/api/delivery-price").json()["delivery_price"]


check(1, "По умолчанию 12 000", public_price() == 12000)
old = order("delivery-001").json()
check(1, "Заказ считается с доставкой 12 000", old["delivery_price"] == 12000
      and old["total"] == 72000 + 12000)

r = client.put("/api/admin/delivery-price", json={"price": 15000}, headers=ADMIN)
check(2, "Администратор меняет стоимость", r.status_code == 200 and r.json()["price"] == 15000)
check(2, "Панель видит новую стоимость",
      client.get("/api/admin/delivery-price", headers=ADMIN).json()["price"] == 15000)
check(2, "Витрина сразу видит новую и не кэширует её", public_price() == 15000
      and client.get("/api/delivery-price").headers.get("cache-control") == "no-store")

new = order("delivery-002").json()
check(3, "Новый заказ — с новой доставкой", new["delivery_price"] == 15000
      and new["total"] == 72000 + 15000)
kept = SessionLocal().query(Order).filter_by(number=old["number"]).one()
check(3, "Оформленный раньше заказ не изменился", kept.delivery_price == 12000
      and kept.total == 84000)

r = order("delivery-003", expected=72000 + 12000)
check(4, "Корзина со старой доставкой — «цены изменились», а не молча другая сумма",
      r.status_code == 409)

check(5, "Покупатель стоимость не меняет",
      client.put("/api/admin/delivery-price", json={"price": 1}, headers=BUYER).status_code in (401, 404))
for bad in (-1, 2_000_000, "дорого", None):
    r = client.put("/api/admin/delivery-price", json={"price": bad}, headers=ADMIN)
    check(5, f"Недопустимое {bad!r} отклонено", r.status_code == 422)
check(5, "После отказов стоимость прежняя", public_price() == 15000)

r = client.put("/api/admin/delivery-price", json={"price": 0}, headers=ADMIN)
free = order("delivery-004").json()
check(6, "Бесплатная доставка: итог — только товары", r.status_code == 200
      and free["delivery_price"] == 0 and free["total"] == 72000)
row = SessionLocal().query(Order).filter_by(number=free["number"]).one()
_ = row.items
check(6, "В счёте Telegram нет строки доставки с нулём",
      all(p["label"] != "Доставка" for p in payments.prices(row))
      and sum(p["amount"] for p in payments.prices(row)) == 72000 * 100)
check(6, "В чеке Payme нет доставки", "shipping" not in receipt(row))

# ------------------------------------------------------------ листок кассы
from app.regos.orders_push import build_payload  # noqa: E402

s = SessionLocal()
paid = s.query(Order).filter_by(number=new["number"]).one()
desc = build_payload(paid, {variant.id: "003100"})["document"]["description"]
print(desc)
lines = desc.split("\n")
check(7, "Примечание начинается как раньше: номер, покупатель, срок",
      lines[0].startswith(f"ROZMART {new['number']} | Жавохир | Как можно скорее"))
check(7, "В примечании товар, количество, цена и сумма",
      "Докторская 400 г" in lines and "  2 x 36 000 = 72 000" in lines)
check(7, "В конце — товары, доставка и итог",
      lines[-3:] == ["Товары: 72 000 сум", "Доставка: 15 000 сум", "ИТОГО: 87 000 сум"], lines[-3:])
free_desc = build_payload(row, {variant.id: "003100"})["document"]["description"]
check(7, "Бесплатная доставка так и написана", "Доставка: бесплатно" in free_desc)

# Срок «Завтра до конца рабочего дня»: кассир видит в REGOS завтра 18:00 по Ташкенту
from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from app import notify  # noqa: E402
late = SimpleNamespace(delivery_slot="Завтра до конца рабочего дня",
                       created_at=datetime(2026, 10, 12, 16, 30))       # 21:30 по Ташкенту
ts = orders_push.delivery_timestamp(late)
local = datetime.fromtimestamp(ts, timezone.utc).astimezone(timezone.utc)
check(8, "Срок в REGOS — завтра 18:00 по Ташкенту (13:00 UTC)",
      local == datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc), local)
check(8, "Сотрудникам по-узбекски", notify.slot(late, "uz") == "Ertaga ish kuni oxirigacha")

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
