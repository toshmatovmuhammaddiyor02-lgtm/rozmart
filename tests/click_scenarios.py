"""Оплата через Click (SHOP API) — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/click_scenarios.py

Prepare и Complete по документации Click: подпись, коды ошибок, повторы,
неуспешная оплата, деньги за отменённый заказ (возврат), чек в налоговую,
ссылка на оплату, пилот только для тестировщиков, настройки в панели.
База — временный SQLite, REGOS и Merchant API Click не вызываются. Выход
с кодом 1, если хоть одна проверка не прошла.
"""
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

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
    "PAYME_RETURN_URL": "https://t.me/rozmartuz_bot",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app import click, notify, payments  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (Category, ClickTransaction, Notification, Order, Product,  # noqa: E402
                        StaffChat, Variant, utcnow)

pushed = []
orders_push.push_one = lambda order_id: pushed.append(order_id)
orders_push.cancel_one = lambda order_id: None
notify.call = lambda method, payload, token=None: {"message_id": 1}

# Merchant API Click — подменяем: запоминаем запросы, отвечаем как скажем
api_calls, api_answers = [], {}


def fake_api(method, path, body=None):
    api_calls.append((method, path, body))
    answer = api_answers.get(path.split("/")[2] if path.startswith("/payment/") else path,
                             {"error_code": 0, "error_note": "Success"})
    if isinstance(answer, Exception):
        raise answer
    return answer


click.api = fake_api

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True, mxik="01601002002011001",
                  package_code="1403931", vat_percent=12)
db.add(variant)
db.add(StaffChat(chat_id=-100, title="Магазин", kind="group", active=True))
db.commit()

client = TestClient(app)


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


BUYER, STRANGER, ADMIN = init_data(900), init_data(901), init_data(1)
SECRET = "Cl1ck-Secret-Key"
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def form(click_id, number, amount, action, prepare_id=None, error="0", service="113234",
         secret=SECRET, paydoc=None):
    data = {"click_trans_id": str(click_id), "service_id": service,
            "click_paydoc_id": str(paydoc or click_id + 5000000), "merchant_trans_id": number,
            "amount": amount, "action": str(action), "error": error,
            "error_note": "Success" if error == "0" else "Ошибка",
            "sign_time": "2026-10-07 12:00:00"}
    if prepare_id is not None:
        data["merchant_prepare_id"] = str(prepare_id)
    parts = [data["click_trans_id"], service, secret, number]
    if prepare_id is not None:
        parts.append(str(prepare_id))
    parts += [amount, str(action), data["sign_time"]]
    data["sign_string"] = hashlib.md5("".join(parts).encode()).hexdigest()
    return data


def prepare(click_id, number, amount, **kw):
    return client.post("/click/prepare", data=form(click_id, number, amount, 0, **kw)).json()


def complete(click_id, number, amount, prepare_id, **kw):
    return client.post("/click/complete", data=form(click_id, number, amount, 1, prepare_id,
                                                     **kw)).json()


def new_order(key, payment="online", qty=1, headers=BUYER, provider="click"):
    body = {"customer_name": "Javohir Rahmonov", "phone": "+998901234567", "address": "Chilonzor 9",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": qty}]}
    if payment == "online":
        body["pay_provider"] = provider
    r = client.post("/api/orders", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


def tx(click_id):
    s = SessionLocal()
    try:
        return s.query(ClickTransaction).filter_by(click_trans_id=click_id).one_or_none()
    finally:
        s.close()


def staff_texts():
    s = SessionLocal()
    try:
        return [n.text for n in s.query(Notification).order_by(Notification.id)]
    finally:
        s.close()


def money(n):
    return f"{n}.00"


# ------------------------------------------------------------ подпись и запрос
o = new_order("click-key-001", qty=2)
number, total = o["number"], o["total"]
pushed.clear()

r = client.post("/click/prepare", data={**form(1001, number, money(total), 0), "sign_string": "0" * 32})
check(1, "Неверная подпись — -1", r.json()["error"] == -1, r.json())
r = prepare(1001, number, money(total), secret="другой-ключ")
check(1, "Подпись чужим ключом — -1", r["error"] == -1, r)
bad = form(1001, number, money(total), 0); bad.pop("sign_time")
check(1, "Нет обязательного поля — -8", client.post("/click/prepare", data=bad).json()["error"] == -8)
check(1, "Prepare с action=1 — -3", client.post("/click/prepare", data=form(
    1001, number, money(total), 1)).json()["error"] == -3)
check(1, "Чужой service_id (подпись верна) — -8", prepare(1001, number, money(total),
                                                          service="999")["error"] == -8)
check(1, "Ответ всегда JSON с HTTP 200", client.post("/click/prepare", data={}).status_code == 200)

# ------------------------------------------------------------ Prepare
check(2, "Заказа нет — -5", prepare(1001, "RB-99999", money(total))["error"] == -5)
check(2, "Не та сумма — -2", prepare(1001, number, money(total - 1))["error"] == -2)
check(2, "Неудачные попытки не записаны", tx(1001) is None)

r = prepare(1001, number, money(total))
check(3, "Prepare: можно — error 0 и merchant_prepare_id", r["error"] == 0
      and r["merchant_prepare_id"] and r["click_trans_id"] == 1001
      and r["merchant_trans_id"] == number, r)
first_prepare = r["merchant_prepare_id"]
check(3, "Заказ забронирован — автоотмена ждёт", row(number).checkout_at is not None)
check(3, "Сумма «N» без копеек тоже понимается", prepare(1002, number, str(total))["error"] == 0)
check(3, "Номер заказа цифрами тоже понимается",
      prepare(1003, number.removeprefix("RB-"), money(total))["error"] == 0)
check(4, "Повтор Prepare — тот же merchant_prepare_id",
      prepare(1001, number, money(total))["merchant_prepare_id"] == first_prepare)

# ------------------------------------------------------------ Complete: оплата не прошла
r = complete(1001, number, money(total), first_prepare, error="-5017")
check(5, "Click сообщил об ошибке оплаты — -9", r["error"] == -9, r)
check(5, "Платёж отменён, заказ ждёт оплаты дальше", tx(1001).state == click.CANCELED
      and row(number).status == "NEW" and row(number).paid_at is None)
check(5, "Повторный Complete отменённого — -9",
      complete(1001, number, money(total), first_prepare)["error"] == -9)
check(5, "Prepare отменённого платежа — -9", prepare(1001, number, money(total))["error"] == -9)

# ------------------------------------------------------------ Complete: проверки
second = tx(1002)
check(6, "Чужой merchant_prepare_id — -6",
      complete(1002, number, money(total), 999999)["error"] == -6)
check(6, "merchant_prepare_id от другого платежа — -6",
      complete(1002, number, money(total), tx(1003).id)["error"] == -6)
check(6, "Complete с action=0 — -3", client.post("/click/complete", data=form(
    1002, number, money(total), 0, second.id)).json()["error"] == -3)
check(6, "Complete на другую сумму — -2",
      complete(1002, number, money(total + 100), second.id)["error"] == -2)
check(6, "Неверная подпись Complete — -1", client.post("/click/complete", data={
    **form(1002, number, money(total), 1, second.id), "sign_string": "f" * 32}).json()["error"] == -1)

# ------------------------------------------------------------ Complete: оплачено
api_calls.clear()
r = complete(1002, number, str(total), second.id)
check(7, "Complete: оплачено — error 0 и merchant_confirm_id", r["error"] == 0
      and r["merchant_confirm_id"] == second.id, r)
got = row(number)
check(7, "Заказ оплачен и принят", got.status == "CONFIRMED" and got.paid_at is not None
      and got.paid_amount == total and got.payment_charge_id == "click:1002"
      and got.provider_charge_id == str(1002 + 5000000), (got.status, got.payment_charge_id))
check(7, "Заказ ушёл в REGOS и сотрудникам «оплачено»", pushed == [got.id]
      and any("ОПЛАЧЕНО" in t and number in t for t in staff_texts()))
check(7, "Не тестовая оплата: пометки «тестовая» нет",
      not any("ТЕСТОВАЯ" in t for t in staff_texts()))

# чек в налоговую
sent = [c for c in api_calls if c[1] == "/payment/ofd_data/submit_items"]
body = sent[0][2] if sent else {}
items = body.get("items", [])
check(8, "Чек отправлен в Click", len(sent) == 1 and body.get("service_id") == 113234
      and body.get("payment_id") == 1002 + 5000000, body)
check(8, "Сумма чека = сумма заказа в тийинах", body.get("received_card") == total * 100
      and sum(i["Price"] for i in items) == total * 100 and body.get("received_cash") == 0)
it = items[0] if items else {}
check(8, "Строка: ИКПУ, код упаковки, количество, НДС, ИНН",
      it.get("SPIC") == "01601002002011001" and it.get("PackageCode") == "1403931"
      and it.get("Amount") == 2000 and it.get("GoodPrice") == 3900000
      and it.get("VATPercent") == 12 and it.get("VAT") == round(7800000 * 12 / 112)
      and it.get("CommissionInfo") == {"TIN": "312345678"} and len(it.get("Name", "")) <= 63, it)
check(8, "Отметка «чек принят»", tx(1002).fiscal_status == "ok")

# повторы
check(9, "Повторный Complete проведённого — -4 (уже оплачен)",
      complete(1002, number, money(total), second.id)["error"] == -4)
check(9, "Повтор не провёл второй раз", pushed == [got.id])
check(9, "Prepare по оплаченному заказу — -4", prepare(1004, number, money(total))["error"] == -4)

# ------------------------------------------------------------ деньги за заказ, который не ждал
# третий подготовленный платёж (1003) по уже оплаченному заказу: Click списал деньги
api_calls.clear()
r = complete(1003, number, money(total), tx(1003).id)
check(10, "Деньги за оплаченный заказ: Click — «успешно»", r["error"] == 0, r)
check(10, "…и платёж возвращён через reversal", tx(1003).state == click.REVERSED
      and any(c[0] == "DELETE" and c[1] == f"/payment/reversal/113234/{1003 + 5000000}"
              for c in api_calls), api_calls)
check(10, "Сотрудникам — «возвращены покупателю»",
      any("Click" in t and "возвращены" in t and number in t for t in staff_texts()))
check(10, "Заказ не тронут: оплачен один раз", row(number).payment_charge_id == "click:1002")

late = new_order("click-key-002")
r = prepare(2001, late["number"], money(late["total"]))
s = SessionLocal()
s.query(Order).filter_by(number=late["number"]).update(
    {"status": "CANCELED"}); s.commit(); s.close()
api_answers["reversal"] = {"error_code": -5014, "error_note": "Payment not found"}
r = complete(2001, late["number"], money(late["total"]), r["merchant_prepare_id"])
check(11, "Деньги за отменённый заказ: «успешно», возврат не удался", r["error"] == 0
      and tx(2001).state == click.REVERSE_FAILED)
check(11, "Сотрудникам — вернуть вручную в кабинете Click",
      any("merchant.click.uz" in t and late["number"] in t for t in staff_texts()))
check(11, "Отменённый заказ не стал оплаченным", row(late["number"]).paid_at is None)
api_answers.pop("reversal")
check(11, "Prepare отменённого заказа — -9",
      prepare(2002, late["number"], money(late["total"]))["error"] == -9)

cash = new_order("click-key-003", payment="cash")
check(12, "Наличный заказ через Click не оплатить — -5",
      prepare(3001, cash["number"], money(cash["total"]))["error"] == -5)

# ------------------------------------------------------------ чек: ошибки и повтор
fis = new_order("click-key-004")
r = prepare(4001, fis["number"], money(fis["total"]))
api_answers["ofd_data"] = {"error_code": -500, "error_note": "Invalid package code"}
complete(4001, fis["number"], money(fis["total"]), r["merchant_prepare_id"])
check(13, "Чек не принят — оплата всё равно проведена", row(fis["number"]).status == "CONFIRMED"
      and tx(4001).fiscal_status == "error" and "package" in (tx(4001).fiscal_note or ""))
api_answers.pop("ofd_data")
s = SessionLocal()
s.query(ClickTransaction).filter_by(click_trans_id=4001).update(
    {"fiscal_at": utcnow() - timedelta(hours=1)}); s.commit()
click.retry_fiscal(s); s.close()
check(13, "Повтор в фоне — чек принят", tx(4001).fiscal_status == "ok"
      and tx(4001).fiscal_attempts == 2)

# ------------------------------------------------------------ приложение: ссылка и пилот
inv_order = new_order("click-key-005")
inv = client.post(f"/api/orders/{inv_order['number']}/invoice", headers=BUYER).json()
q = parse_qs(urlparse(inv.get("link", "")).query)
check(14, "Приложению — страница Click", inv.get("kind") == "click"
      and inv["link"].startswith("https://my.click.uz/services/pay?"), inv)
check(14, "В ссылке сервис, поставщик, сумма N.NN, номер заказа, возврат в бота",
      q.get("service_id") == ["113234"] and q.get("merchant_id") == ["50001"]
      and q.get("amount") == [f"{inv_order['total']}.00"]
      and q.get("transaction_param") == [inv_order["number"]]
      and q.get("return_url") == ["https://t.me/rozmartuz_bot"], q)

opts = client.get("/api/payment-options", headers=STRANGER).json()
check(15, "Пилот: покупатель Click не видит, Payme видит", opts["providers"] == ["payme"], opts)
opts = client.get("/api/payment-options", headers=BUYER).json()
check(15, "Пилот: тестировщик видит Click", "click" in opts["providers"]
      and opts["test"] is False, opts)
r = client.post("/api/orders", json={
    "customer_name": "A", "phone": "+998901234567", "address": "Chilonzor 9",
    "delivery_slot": "Как можно скорее", "payment_method": "online", "consent": True,
    "client_key": "click-key-006", "pay_provider": "click",
    "items": [{"variant_id": variant.id, "quantity": 1}]}, headers=STRANGER)
check(15, "Покупатель в пилоте не оформит заказ на Click",
      r.status_code != 201 or r.json().get("pay_provider") != "click", r.text[:120])


def cfg(values):
    return client.put("/api/admin/payments/config", json={"values": values}, headers=ADMIN)


check(16, "Панель: ИНН не из 9 цифр не принимается", cfg({"CLICK_TIN": "12345"}).status_code == 400)
check(16, "Панель: ID Click только цифрами", cfg({"CLICK_MERCHANT_ID": "abc"}).status_code == 400)
check(16, "Панель: ИКПУ доставки — 17 цифр", cfg({"CLICK_DELIVERY_SPIC": "123"}).status_code == 400)
check(16, "Панель: короткий ключ не принимается", cfg({"CLICK_SECRET_KEY": "123"}).status_code == 400)
view = client.get("/api/admin/payments", headers=ADMIN).json()
ck = view["config"]["CLICK_SECRET_KEY"]
check(16, "Панель: ключ Click — только хвостом", ck.get("set") and ck.get("masked") == "••••-Key"
      and "value" not in ck, ck)
check(16, "Панель: адреса Prepare и Complete", view["endpoints"]["click"][0].endswith("/click/prepare")
      and view["endpoints"]["click"][1].endswith("/click/complete"))
card = next(p for p in view["providers"] if p["provider"] == "click")
check(16, "Панель: Click — «видят только тестировщики»", card["testers_only"] and not card["test"])
check(17, "Пилот выключен в панели", cfg({"CLICK_PILOT": "0"}).status_code == 200)
opts = client.get("/api/payment-options", headers=STRANGER).json()
check(17, "…и Click видят все покупатели", opts["providers"] == ["payme", "click"], opts)
check(17, "Payme пилот Click не задел", "payme" in opts["providers"])

# ------------------------------------------------------------ выключен или без ключей
client.put("/api/admin/payments/click", json={"on": False}, headers=ADMIN)
opts = client.get("/api/payment-options", headers=BUYER).json()
check(18, "Выключен в панели — покупатели не видят", "click" not in opts["providers"])
on_order = new_order("click-key-007", provider="payme")
r = prepare(5001, on_order["number"], money(on_order["total"]))
check(18, "…но начатые оплаты Click принимает", r["error"] == 0, r)
client.put("/api/admin/payments/click", json={"on": True}, headers=ADMIN)
saved = payments.CLICK_SECRET_KEY
payments.CLICK_SECRET_KEY = ""
payments._BASE["CLICK_SECRET_KEY"] = ""
payments.refresh(force=True)
check(18, "Без ключа — Click не работает (-8)", prepare(5002, on_order["number"],
                                                        money(on_order["total"]))["error"] == -8)
payments.CLICK_SECRET_KEY = saved
payments._BASE["CLICK_SECRET_KEY"] = saved

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
