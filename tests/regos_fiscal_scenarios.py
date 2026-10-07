"""Онлайн-оплата и касса REGOS: форма оплаты и фискальный чек.

    .venv\\Scripts\\python.exe tests/regos_fiscal_scenarios.py

Оплаченный онлайн заказ должен прийти на кассу так, чтобы кассир только
закрыл чек: со своей формой оплаты (задаётся в панели) и, если платёжная
система уже выдала фискальный чек, — со ссылкой на него (касса тогда второй
чек не печатает). База — временный SQLite, REGOS и Click подменены.
Выход с кодом 1, если хоть одна проверка не прошла.
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
    "NOTIFY_BOT_TOKEN": "222:staff-token",
    "PAYMENT_PROVIDERS": "payme,click",
    "PAYME_MERCHANT_ID": "65f0c0ffee0000000000abcd",
    "PAYME_KEY": "sandbox-key-123",
    "PAYME_TEST": "0",
    "CLICK_SERVICE_ID": "113234",
    "CLICK_MERCHANT_ID": "50001",
    "CLICK_MERCHANT_USER_ID": "70001",
    "CLICK_SECRET_KEY": "Cl1ck-Secret-Key",
    "CLICK_TIN": "312345678",
    "PAYMENT_TEST_USERS": "900",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app import click, notify  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Category, Order, Product, Setting, Variant  # noqa: E402
from app.regos.client import RegosError  # noqa: E402

notify.call = lambda method, payload, token=None: {"message_id": 1}

# REGOS: запоминаем вызовы, документам даём номера по порядку
calls, fail = [], {}


class FakeRegos:
    def call(self, method, params=None, **kw):
        calls.append((method, params))
        if method in fail:
            raise fail[method]
        if method == "DocOrderDelivery/Get":
            return []
        if method == "RetailCustomer/Get":
            return [{"id": 77}]
        if method == "DocOrderDelivery/AddFull":
            return {"new_id": 100 + sum(1 for m, _ in calls if m == method)}
        if method == "PaymentType/Get":
            return [{"id": 2, "name": "Пласт. карта", "is_cash": False, "enabled": "True"},
                    {"id": 1, "name": "Наличные", "is_cash": True, "enabled": "True"},
                    {"id": 12, "name": "ROZMART Payme", "is_cash": False, "enabled": "Frontoffice"},
                    {"id": 3, "name": "Перечисление", "is_cash": False, "enabled": "Backoffice"}]
        return {"row_affected": 1}


orders_push.RegosClient = FakeRegos
import app.admin as admin_module  # noqa: E402
import app.regos.client as regos_client  # noqa: E402
regos_client.RegosClient = FakeRegos

# Click Merchant API
click_answers = {}


def fake_click_api(method, path, body=None):
    if path.startswith("/payment/ofd_data/submit_items"):
        return {"error_code": 0, "error_note": "Success"}
    if path.startswith("/payment/ofd_data/"):
        return click_answers.get("qr", {"error_code": -1})
    return {"error_code": 0}


click.api = fake_click_api

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True, mxik="01601002002011001",
                  package_code="1403931", vat_percent=12)
db.add(variant)
db.add(Setting(name=orders_push.SETTING_AUTO, value="1"))
db.commit()
client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


BUYER, ADMIN = init_data(900), init_data(1)
PAYME = {"Authorization": "Basic " + base64.b64encode(b"Paycom:sandbox-key-123").decode()}
results = []
seq = [0]


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def new_order(key, provider):
    body = {"customer_name": "Javohir Rahmonov", "phone": "+998901234567", "address": "Chilonzor 9",
            "delivery_slot": "Как можно скорее", "payment_method": "online", "consent": True,
            "client_key": key, "pay_provider": provider,
            "items": [{"variant_id": variant.id, "quantity": 1}]}
    r = client.post("/api/orders", json=body, headers=BUYER)
    assert r.status_code == 201, r.text
    return r.json()


def rpc(method, params):
    seq[0] += 1
    return client.post("/payme", json={"jsonrpc": "2.0", "id": seq[0], "method": method,
                                       "params": params}, headers=PAYME).json()


def pay_payme(order, tx_id):
    acc = {"order_id": order["number"]}
    amount = order["total"] * 100
    rpc("CreateTransaction", {"id": tx_id, "time": int(time.time() * 1000), "amount": amount,
                              "account": acc})
    return rpc("PerformTransaction", {"id": tx_id})


def click_form(click_id, number, amount, action, prepare_id=None):
    data = {"click_trans_id": str(click_id), "service_id": "113234",
            "click_paydoc_id": str(click_id + 5000000), "merchant_trans_id": number,
            "amount": amount, "action": str(action), "error": "0", "error_note": "Success",
            "sign_time": "2026-10-07 12:00:00"}
    parts = [data["click_trans_id"], "113234", "Cl1ck-Secret-Key", number]
    if prepare_id is not None:
        data["merchant_prepare_id"] = str(prepare_id)
        parts.append(str(prepare_id))
    parts += [amount, str(action), data["sign_time"]]
    data["sign_string"] = hashlib.md5("".join(parts).encode()).hexdigest()
    return data


def row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


def addfull_for(number):
    return next((p for m, p in calls if m == "DocOrderDelivery/AddFull"
                 and p["document"]["external_code"] == number), None)


def fiscal_calls(doc_id):
    return [p for m, p in calls if m == "DocOrderDelivery/SetFiscalInfo" and p["id"] == doc_id]


def cfg(values):
    return client.put("/api/admin/payments/config", json={"values": values}, headers=ADMIN)


# ------------------------------------------------------------ форма оплаты
o1 = new_order("rf-key-001", "payme")
pay_payme(o1, "tx-1")
doc = addfull_for(o1["number"])
check(1, "Без настройки — «Пласт. карта» (2), как раньше",
      doc and doc["document"]["payment_type_id"] == 2, doc and doc["document"]["payment_type_id"])
check(1, "Кассиру видно, чем оплачено: «ОПЛАЧЕНО ОНЛАЙН (Payme)»",
      doc and doc["document"]["description"].startswith("ОПЛАЧЕНО ОНЛАЙН (Payme), ДЕНЬГИ НЕ БРАТЬ"))
check(1, "Ссылки на чек ещё нет — касса чек напечатает", doc and "qrcodeurl" not in doc["document"])

view = client.get("/api/admin/payments", headers=ADMIN).json()
types = view.get("regos_payment_types", [])
check(2, "Панели — формы оплаты REGOS, доступные на кассе",
      {t["id"] for t in types} == {2, 1, 12}, types)
check(2, "Форма оплаты — только номером", cfg({"REGOS_PAY_PAYME": "abc"}).status_code == 400)
check(2, "Выбрали «ROZMART Payme»", cfg({"REGOS_PAY_PAYME": "12"}).status_code == 200)
o2 = new_order("rf-key-002", "payme")
pay_payme(o2, "tx-2")
doc2 = addfull_for(o2["number"])
check(2, "Оплаченный через Payme приходит с формой «ROZMART Payme»",
      doc2 and doc2["document"]["payment_type_id"] == 12)
cash = client.post("/api/orders", json={
    "customer_name": "Javohir Rahmonov", "phone": "+998901234567", "address": "Chilonzor 9",
    "delivery_slot": "Как можно скорее", "payment_method": "cash", "consent": True,
    "client_key": "rf-key-003", "items": [{"variant_id": variant.id, "quantity": 1}]},
    headers=BUYER).json()
cash_doc = addfull_for(cash["number"])
check(2, "Наличный заказ — по-прежнему «Наличные» (1)",
      cash_doc and cash_doc["document"]["payment_type_id"] == 1)

# ------------------------------------------------------------ чек Payme -> REGOS
doc_id = row(o2["number"]).regos_document_id
QR = "https://ofd.soliq.uz/epi?t=EZ000000000030&r=1&c=20261007&s=1"
r = rpc("SetFiscalData", {"id": "tx-2", "type": "PERFORM", "fiscal_data": {
    "status_code": 0, "qr_code_url": QR}})
check(3, "SetFiscalData принят", (r.get("result") or {}).get("success") is True, r)
got = fiscal_calls(doc_id)
check(3, "Ссылка на чек ушла в заказ REGOS (SetFiscalInfo)",
      len(got) == 1 and got[0]["qrcodeurl"] == QR, got)
check(3, "Отметка «REGOS принял»", row(o2["number"]).fiscal_qr_regos_at is not None
      and row(o2["number"]).fiscal_qr_url == QR)
rpc("SetFiscalData", {"id": "tx-2", "type": "PERFORM", "fiscal_data": {"qr_code_url": QR}})
check(3, "Повтор SetFiscalData второй раз в REGOS не шлёт", len(fiscal_calls(doc_id)) == 1)
rpc("SetFiscalData", {"id": "tx-2", "type": "CANCEL", "fiscal_data": {"qr_code_url": QR + "c"}})
check(3, "Чек возврата ссылку продажи не подменяет", row(o2["number"]).fiscal_qr_url == QR)

# REGOS не принял (кассир держит документ) — повтор в фоне
o3 = new_order("rf-key-004", "payme")
pay_payme(o3, "tx-3")
doc3 = row(o3["number"]).regos_document_id
fail["DocOrderDelivery/SetFiscalInfo"] = RegosError(1050, "Документ заблокирован", "SetFiscalInfo")
rpc("SetFiscalData", {"id": "tx-3", "type": "PERFORM", "fiscal_data": {"qr_code_url": QR + "3"}})
r3 = row(o3["number"])
check(4, "REGOS отказал — ошибка записана, попытка одна", r3.fiscal_qr_regos_at is None
      and "заблокирован" in (r3.fiscal_qr_regos_error or "") and r3.fiscal_qr_regos_tries == 1)
fail.clear()
s = SessionLocal(); orders_push.fiscal_qr_pending(s); s.close()
check(4, "Повтор в фоне — принято", row(o3["number"]).fiscal_qr_regos_at is not None
      and fiscal_calls(doc3)[-1]["qrcodeurl"] == QR + "3")

# чек пришёл раньше выгрузки — ссылка уходит в самом документе
s = SessionLocal()
s.query(Setting).filter_by(name=orders_push.SETTING_AUTO).update({"value": "0"}); s.commit(); s.close()
o4 = new_order("rf-key-005", "payme")
pay_payme(o4, "tx-4")
rpc("SetFiscalData", {"id": "tx-4", "type": "PERFORM", "fiscal_data": {"qr_code_url": QR + "4"}})
check(5, "Заказ ещё не в REGOS — ссылка ждёт у заказа", row(o4["number"]).fiscal_qr_url == QR + "4"
      and row(o4["number"]).fiscal_qr_regos_at is None)
s = SessionLocal()
s.query(Setting).filter_by(name=orders_push.SETTING_AUTO).update({"value": "1"}); s.commit(); s.close()
orders_push.push_one(row(o4["number"]).id)
doc4 = addfull_for(o4["number"])
check(5, "Выгрузка — со ссылкой на чек (касса второй не печатает)",
      doc4 and doc4["document"].get("qrcodeurl") == QR + "4")
check(5, "…и SetFiscalInfo уже не нужен", row(o4["number"]).fiscal_qr_regos_at is not None
      and not fiscal_calls(row(o4["number"]).regos_document_id))

# ------------------------------------------------------------ Click: форма и чек
cfg({"REGOS_PAY_CLICK": "12"})
o5 = new_order("rf-key-006", "click")
amount = f"{o5['total']}.00"
r = client.post("/click/prepare", data=click_form(7001, o5["number"], amount, 0)).json()
click_answers["qr"] = {"error_code": -1}           # чек ещё не зарегистрирован
r = client.post("/click/complete", data=click_form(7001, o5["number"], amount, 1,
                                                    r["merchant_prepare_id"])).json()
doc5 = addfull_for(o5["number"])
check(6, "Click: оплачено, заказ на кассе с формой из панели и пометкой (Click)",
      r["error"] == 0 and doc5 and doc5["document"]["payment_type_id"] == 12
      and "ОПЛАЧЕНО ОНЛАЙН (Click)" in doc5["document"]["description"], r)
check(6, "Ссылки на чек у Click ещё нет — ждём", row(o5["number"]).fiscal_qr_url is None)
click_answers["qr"] = {"paymentId": 7001 + 5000000, "qrCodeURL": QR + "c5"}
s = SessionLocal(); click.retry_fiscal(s); s.close()
d5 = row(o5["number"]).regos_document_id
check(6, "Появилась — ушла в заказ REGOS", row(o5["number"]).fiscal_qr_url == QR + "c5"
      and fiscal_calls(d5) and fiscal_calls(d5)[-1]["qrcodeurl"] == QR + "c5")

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
