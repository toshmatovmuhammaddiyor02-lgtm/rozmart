"""Покупатель сам выбирает способ оплаты: Payme или Paynet.

    .venv\\Scripts\\python.exe tests/providers_scenarios.py

Оба способа включены разом (PAYMENT_PROVIDERS=payme,paynet). База —
временный SQLite, REGOS не вызывается. Выход с кодом 1, если хоть одна
проверка не прошла.
"""
import base64
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
    "PAYMENT_PROVIDERS": "payme,paynet",
    "PAYME_MERCHANT_ID": "65f0c0ffee0000000000abcd",
    "PAYME_KEY": "sandbox-key-123",
    "PAYNET_LOGIN": "rozmart",
    "PAYNET_PASSWORD": "s3cret-Pass",
    "PAYNET_SERVICE_ID": "155",
    "PAYMENT_TEST_USERS": "900",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Order, Product, Variant  # noqa: E402

orders_push.push_one = lambda order_id: None

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=36000,
                  regos_price=36000, is_active=True, mxik="01601002002011001",
                  package_code="1403931", vat_percent=12)
db.add(variant); db.commit()
client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


TESTER, STRANGER = init_data(900), init_data(901)
PAYME = {"Authorization": "Basic " + base64.b64encode(b"Paycom:sandbox-key-123").decode()}
PAYNET = {"Authorization": "Basic " + base64.b64encode(b"rozmart:s3cret-Pass").decode()}
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def order(key, provider=None, headers=TESTER, payment="online"):
    body = {"customer_name": "Javohir", "phone": "+998901234567", "address": "Chilonzor 9",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": 1}]}
    if provider:
        body["pay_provider"] = provider
    return client.post("/api/orders", json=body, headers=headers)


def row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


opts = client.get("/api/payment-options", headers=TESTER).json()
check(1, "Тестировщику — оба способа, по порядку", opts["providers"] == ["payme", "paynet"]
      and opts["card"] is True, opts)
opts = client.get("/api/payment-options", headers=STRANGER).json()
check(1, "Остальным, пока оба тестовые, — ни одного", opts["providers"] == [] and opts["card"] is False)

r = order("prov-key-0001", provider="paynet")
o = r.json()
check(2, "Выбрал Paynet — запомнено у заказа", r.status_code == 201 and o["pay_provider"] == "paynet"
      and row(o["number"]).pay_provider == "paynet", o)
inv = client.post(f"/api/orders/{o['number']}/invoice", headers=TESTER).json()
check(2, "«Оплатить» открывает Paynet", inv["kind"] == "paynet", inv)
inv = client.post(f"/api/orders/{o['number']}/invoice?provider=payme", headers=TESTER).json()
check(3, "Передумал — можно через Payme", inv["kind"] == "payme"
      and row(o["number"]).pay_provider == "payme", inv)
r = client.post(f"/api/orders/{o['number']}/invoice?provider=telegram", headers=TESTER)
check(3, "Невключённый способ — отказ", r.status_code == 409)

r = order("prov-key-0002")
check(4, "Не выбрал — первый включённый (Payme)", r.json()["pay_provider"] == "payme")
check(4, "Невключённый способ при оформлении — отказ", order("prov-key-0003", provider="telegram")
      .status_code == 400)
check(4, "Не тестировщику онлайн-оплата недоступна", order("prov-key-0004", provider="payme",
                                                          headers=STRANGER).status_code == 400)
cash = order("prov-key-0005", payment="cash").json()
check(4, "Наличные — без способа онлайн-оплаты", cash["pay_provider"] is None)

# оплатил через Paynet — Payme тот же заказ не примет
number, amount = o["number"], o["total"] * 100
r = client.post("/paynet", headers=PAYNET, json={"jsonrpc": "2.0", "id": 1,
    "method": "PerformTransaction", "params": {"serviceId": 155, "amount": amount,
    "transactionId": "1646338021999", "fields": {"order_id": number.split("-")[1]}}})
check(5, "Оплачено через Paynet, хотя последним выбирал Payme", r.json().get("result")
      and row(number).paid_at is not None, r.json())
r = client.post("/payme", headers=PAYME, json={"jsonrpc": "2.0", "id": 2,
    "method": "CheckPerformTransaction", "params": {"amount": amount, "account": {"order_id": number}}})
check(5, "Второй раз через Payme не оплатить — заказ уже оплачен",
      (r.json().get("error") or {}).get("code") == -31052, r.json())
r = client.post(f"/api/orders/{number}/invoice", headers=TESTER)
check(5, "И кнопки «Оплатить» у оплаченного больше нет", r.status_code == 409)

# ------------------------------------------------------------ пилот на боевых деньгах
from app import payments  # noqa: E402

payments.PAYME_TEST = False                 # боевой ключ
opts = client.get("/api/payment-options", headers=STRANGER).json()
check(6, "Боевой Payme без пилота — виден всем", opts["providers"] == ["payme"])
payments.TESTERS_ONLY = True
opts = client.get("/api/payment-options", headers=STRANGER).json()
check(6, "Пилот: боевой Payme остальным не виден", opts["providers"] == [])
opts = client.get("/api/payment-options", headers=TESTER).json()
check(6, "Пилот: тестировщику — виден", "payme" in opts["providers"])
check(6, "Пилот: не тестировщик заказ картой не оформит",
      order("prov-key-0010", provider="payme", headers=STRANGER).status_code == 400)
payments.TESTERS_ONLY, payments.PAYME_TEST = False, True

# ------------------------------------------------------------ переключатели в панели
ADMIN = init_data(1)
r = client.get("/api/admin/payments", headers=ADMIN).json()
check(7, "Панель видит оба способа включёнными", [(p["provider"], p["on"]) for p in r["providers"]]
      == [("payme", True), ("paynet", True)], r)
check(7, "Покупатель переключатели не трогает",
      client.put("/api/admin/payments/payme", json={"on": False}, headers=TESTER).status_code in (401, 404))
r = client.put("/api/admin/payments/payme", json={"on": False}, headers=ADMIN)
check(7, "Выключили Payme", r.status_code == 200 and r.json()["providers"][0]["on"] is False)
opts = client.get("/api/payment-options", headers=TESTER).json()
check(7, "Покупатель Payme больше не видит", opts["providers"] == ["paynet"], opts)
check(7, "И заказ через Payme не оформить", order("prov-key-0020", provider="payme").status_code == 400)
o20 = order("prov-key-0021", provider="paynet").json()
s20 = SessionLocal(); s20.query(Order).filter_by(number=o20["number"]).update({"pay_provider": "payme"})
s20.commit(); s20.close()
r = client.post("/payme", headers=PAYME, json={"jsonrpc": "2.0", "id": 3,
    "method": "CheckPerformTransaction", "params": {"amount": o20["total"] * 100,
                                                   "account": {"order_id": o20["number"]}}})
check(7, "Начатые оплаты Payme сервер всё равно принимает", (r.json().get("result") or {}).get("allow")
      is True, r.json())
client.put("/api/admin/payments/payme", json={"on": True}, headers=ADMIN)
opts = client.get("/api/payment-options", headers=TESTER).json()
check(7, "Включили обратно — снова виден", opts["providers"] == ["payme", "paynet"])
check(7, "Неподключённый способ — 404",
      client.put("/api/admin/payments/telegram", json={"on": True}, headers=ADMIN).status_code == 404)

# ------------------------------------------------------------ ключи из панели
r = client.get("/api/admin/payments", headers=ADMIN).json()
cfg = r["config"]
check(8, "Панель видит ключи с сервера, секрет — только хвостом",
      cfg["PAYME_MERCHANT_ID"] == {"set": True, "masked": "••••abcd", "source": "server"}
      and cfg["PAYME_KEY"]["masked"] == "••••-123" and "value" not in cfg["PAYME_KEY"]
      and cfg["PAYME_KEY"]["source"] == "server", cfg["PAYME_KEY"])
check(8, "И адреса для Payme и Paynet", r["endpoints"]["paynet"].endswith("/paynet"))

def put_cfg(values):
    return client.put("/api/admin/payments/config", json={"values": values}, headers=ADMIN)

check(8, "Кривой ID кассы — отказ", put_cfg({"PAYME_MERCHANT_ID": "xyz"}).status_code == 400)
check(8, "Буквы в номере сервиса — отказ", put_cfg({"PAYNET_SERVICE_ID": "15a"}).status_code == 400)
check(8, "Короткий пароль — отказ", put_cfg({"PAYNET_PASSWORD": "123"}).status_code == 400)
check(8, "ID кассы вместо ключа — отказ", put_cfg({"PAYME_KEY": "65f0c0ffee00000000aa11bb"})
      .status_code == 400 and put_cfg({"PAYME_KEY": "65f0c0ffee0000000000abcd"}).status_code == 400)
check(8, "После отказа ключ прежний", client.get("/api/admin/payments", headers=ADMIN)
      .json()["config"]["PAYME_KEY"]["source"] == "server")
check(8, "Покупатель ключи не меняет", client.put("/api/admin/payments/config", headers=TESTER,
      json={"values": {"PAYNET_SERVICE_ID": "1"}}).status_code in (401, 404))

r = put_cfg({"PAYNET_SERVICE_ID": "777", "PAYNET_LOGIN": "rozmart2", "PAYNET_PASSWORD": "NewPass-2026"})
check(9, "Сохранили ключи Paynet в панели", r.status_code == 200
      and r.json()["config"]["PAYNET_SERVICE_ID"] == {"value": "777", "source": "panel"}, r.json())
new_auth = {"Authorization": "Basic " + base64.b64encode(b"rozmart2:NewPass-2026").decode()}
r = client.post("/paynet", headers=new_auth, json={"jsonrpc": "2.0", "id": 1, "method": "CheckTransaction",
                                                   "params": {"serviceId": 777, "transactionId": "1"}})
check(9, "Paynet сразу пускает с новыми логином и паролем", r.status_code == 200
      and r.json()["result"]["transactionState"] == 3, r.json())
check(9, "Со старыми — уже нет", client.post("/paynet", headers=PAYNET, json={
    "jsonrpc": "2.0", "id": 1, "method": "CheckTransaction", "params": {}}).status_code == 401)
r = put_cfg({"PAYNET_PASSWORD": ""})
check(9, "Пустое поле пароля — пароль не стёрт", client.post("/paynet", headers=new_auth, json={
    "jsonrpc": "2.0", "id": 1, "method": "CheckTransaction",
    "params": {"serviceId": 777, "transactionId": "1"}}).status_code == 200)

r = client.post("/api/admin/payments/paynet-password", headers=ADMIN).json()
gen = {"Authorization": "Basic " + base64.b64encode(f"rozmart2:{r['password']}".encode()).decode()}
check(10, "Сгенерированный пароль сразу работает", len(r["password"]) >= 20 and client.post(
    "/paynet", headers=gen, json={"jsonrpc": "2.0", "id": 1, "method": "CheckTransaction",
                                  "params": {"serviceId": 777, "transactionId": "1"}}).status_code == 200)

put_cfg({"PAYNET_SERVICE_ID": "", "PAYNET_LOGIN": ""})
r = client.get("/api/admin/payments", headers=ADMIN).json()["config"]
check(10, "Очистили в панели — снова значение с сервера",
      r["PAYNET_SERVICE_ID"] == {"value": "155", "source": "server"}
      and r["PAYNET_LOGIN"]["value"] == "rozmart")

r = put_cfg({"TEST_USERS": "900, 905"})
opts = client.get("/api/payment-options", headers=init_data(905)).json()
check(11, "Тестировщика добавили в панели — он видит тестовую оплату", r.status_code == 200
      and opts["providers"] == ["payme", "paynet"], opts)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
