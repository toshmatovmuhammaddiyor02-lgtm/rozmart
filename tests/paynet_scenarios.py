"""Оплата через Paynet — через HTTP к приложению.

    .venv\\Scripts\\python.exe tests/paynet_scenarios.py

Те же 12 сценариев, что прогоняет тестер Paynet («TEST CASE»), и то, что
он не проверяет: доступ, CORS для тестера в браузере, номер заказа цифрами,
оплата отменённого заказа, возврат. База — временный SQLite, REGOS не
вызывается. Выход с кодом 1, если хоть одна проверка не прошла.
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
from datetime import datetime, timedelta, timezone
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
    "PAYMENT_PROVIDER": "paynet",
    "PAYNET_LOGIN": "rozmart",
    "PAYNET_PASSWORD": "s3cret-Pass",
    "PAYNET_SERVICE_ID": "155",
    "PAYMENT_TEST_USERS": "900",
})

from fastapi.testclient import TestClient  # noqa: E402

import app.regos.orders_push as orders_push  # noqa: E402
from app import notify, payments  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (Category, Notification, Order, PaynetTransaction, Product,  # noqa: E402
                        StaffChat, Variant, utcnow)

pushed, cancel_pushed = [], []
orders_push.push_one = lambda order_id: pushed.append(order_id)
orders_push.cancel_one = lambda order_id: cancel_pushed.append(order_id)
notify.call = lambda method, payload, token=None: {"message_id": 1}

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True)
db.add(variant)
db.add(StaffChat(chat_id=-100, title="Магазин", kind="group", active=True))
db.commit()

client = TestClient(app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"rozmart:s3cret-Pass").decode()}
TASHKENT = timezone(timedelta(hours=5))


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


BUYER = init_data(900)
results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def rpc(method, params, headers=AUTH, request_id=1):
    r = client.post("/paynet", json={"jsonrpc": "2.0", "id": request_id, "method": method,
                                     "params": params}, headers=headers)
    return r


def code(r):
    return (r.json().get("error") or {}).get("code")


def new_order(key, payment="online", qty=1):
    body = {"customer_name": "Javohir Rahmonov", "phone": "+998901234567", "address": "Chilonzor 9",
            "delivery_slot": "Как можно скорее", "payment_method": payment, "consent": True,
            "client_key": key, "items": [{"variant_id": variant.id, "quantity": qty}]}
    r = client.post("/api/orders", json=body, headers=BUYER)
    assert r.status_code == 201, r.text
    return r.json()


def row(number):
    s = SessionLocal()
    try:
        return s.query(Order).filter_by(number=number).one()
    finally:
        s.close()


def now_local(delta_min=0):
    return (datetime.now(TASHKENT) + timedelta(minutes=delta_min)).strftime("%Y-%m-%d %H:%M:%S")


def perform_params(tid, amount, value, service=155):
    return {"amount": amount, "serviceId": service, "transactionId": tid,
            "fields": {"client_id": value}, "timestamp": now_local()}


# ------------------------------------------------------------ доступ
r = rpc("GetInformation", {"serviceId": 155, "fields": {"client_id": "1"}}, headers={})
check(0, "Без логина и пароля — HTTP 401", r.status_code == 401, r.status_code)
bad = {"Authorization": "Basic " + base64.b64encode(b"rozmart:wrong").decode()}
check(0, "Неверный пароль — HTTP 401", rpc("CheckTransaction", {}, headers=bad).status_code == 401)
r = client.options("/paynet", headers={"Origin": "null", "Access-Control-Request-Method": "POST",
                                       "Access-Control-Request-Headers": "authorization,content-type"})
check(0, "Тестеру в браузере — CORS разрешён", r.status_code in (200, 204)
      and r.headers.get("access-control-allow-origin") == "*"
      and "Authorization" in r.headers.get("access-control-allow-headers", ""))
check(0, "Ответ с ошибкой доступа тоже с CORS — браузер его покажет",
      rpc("GetInformation", {}, headers={}).headers.get("access-control-allow-origin") == "*")
check(0, "GET вместо POST — -32300", client.get("/paynet").json()["error"]["code"] == -32300)
check(0, "Кривой JSON — -32700",
      client.post("/paynet", content=b"{oops", headers=AUTH).json()["error"]["code"] == -32700)
check(0, "Неизвестный метод — -32601", code(rpc("Refund", {"serviceId": 155})) == -32601)
check(0, "Чужой сервис — 305", code(rpc("GetInformation", {"serviceId": 999,
                                                             "fields": {"client_id": "1"}})) == 305)

# ------------------------------------------------------------ сценарии Paynet
order = new_order("paynet-key-001", qty=13)            # 13 × 39 000 + 12 000 = 519 000
number, digits = order["number"], order["number"].split("-")[1]
amount = order["total"] * 100
TID = "1646338021315"

r = rpc("GetInformation", {"serviceId": 155, "fields": {"client_id": "998973610078"}})
check(1, "GetInformation: заказа нет — 302", code(r) == 302, r.json())
r = rpc("GetInformation", {"serviceId": 155, "fields": {"client_id": digits}}, request_id=12350)
res = r.json().get("result") or {}
check(2, "GetInformation: заказ есть — сумма и имя", r.json()["id"] == 12350 and res.get("status") == 0
      and res["fields"]["amount"] == amount and res["fields"]["name"] == "Javohir R.", res)
check(2, "Время в ответе — ГГГГ-ММ-ДД чч:мм:сс", len(res.get("timestamp", "")) == 19
      and res["timestamp"][4] == "-" and res["timestamp"][13] == ":")
check(2, "Номер можно и целиком «RB-…»", rpc("GetInformation", {
    "serviceId": 155, "fields": {"order_id": number}}).json().get("result") is not None)

r = rpc("PerformTransaction", perform_params(TID, amount, "998973610078"))
check(3, "PerformTransaction: заказа нет — 302", code(r) == 302, r.json())
r = rpc("PerformTransaction", perform_params(TID, amount - 100000, digits))
check(4, "PerformTransaction: не та сумма — 413", code(r) == 413, r.json())
check(4, "Неудачные попытки не записаны — номер транзакции свободен",
      SessionLocal().query(PaynetTransaction).count() == 0)

pushed.clear()   # выгрузка при оформлении — не наша: неоплаченный NEW она пропускает
r = rpc("PerformTransaction", perform_params(TID, amount, digits), request_id=12345)
res = r.json().get("result") or {}
got = row(number)
check(5, "PerformTransaction: оплачено", r.json()["id"] == 12345 and res.get("providerTrnId")
      and got.status == "CONFIRMED" and got.paid_amount == order["total"]
      and got.provider_charge_id == TID, r.json())
check(5, "Заказ ушёл в REGOS и сотрудникам «оплачено»", pushed == [got.id]
      and any("ОПЛАЧЕНО" in n.text for n in SessionLocal().query(Notification)))
provider_trn = res.get("providerTrnId")

r = rpc("PerformTransaction", perform_params(TID, amount, digits))
check(6, "Повтор номера транзакции — 201", code(r) == 201, r.json())
check(6, "Повтор не провёл второй раз", pushed == [got.id]
      and SessionLocal().query(PaynetTransaction).count() == 1)

r = rpc("CheckTransaction", {"serviceId": 155, "transactionId": TID, "timestamp": now_local()})
res = r.json().get("result") or {}
check(7, "CheckTransaction: проведена — 1", res.get("transactionState") == 1
      and res.get("providerTrnId") == provider_trn, res)

r = rpc("GetStatement", {"serviceId": 155, "dateFrom": now_local(-60), "dateTo": now_local(60)})
st = (r.json().get("result") or {}).get("statements")
check(8, "GetStatement: платёж в выписке", st and len(st) == 1 and st[0]["amount"] == amount
      and st[0]["transactionId"] == int(TID) and st[0]["providerTrnId"] == provider_trn, st)
r = rpc("GetStatement", {"serviceId": 155, "dateFrom": now_local(60), "dateTo": now_local(120)})
check(8, "Вне периода — пусто", r.json()["result"]["statements"] == [])
check(8, "Неверная дата — 414", code(rpc("GetStatement", {"serviceId": 155, "dateFrom": "вчера",
                                                          "dateTo": now_local()})) == 414)
r = rpc("GetStatement", {"serviceId": 155, "dateFrom": now_local(-60).replace(" ", "T")[:16],
                         "dateTo": now_local(60).replace(" ", "T")[:16]})
check(8, "Дата из тестера в браузере («ГГГГ-ММ-ДДTчч:мм») тоже понимается",
      len(r.json()["result"]["statements"]) == 1)

r = rpc("CancelTransaction", {"serviceId": 155, "transactionId": TID, "timestamp": now_local()})
res = r.json().get("result") or {}
check(9, "CancelTransaction: отменена — 2", res.get("transactionState") == 2
      and res.get("providerTrnId") == provider_trn, r.json())
check(9, "Заказ отменён — выдавать нельзя, и в REGOS тоже", row(number).status == "CANCELED"
      and cancel_pushed == [got.id] if got.regos_document_id else row(number).status == "CANCELED")
check(9, "Сотрудникам — Paynet отменил оплату",
      any("Paynet отменил оплату" in n.text for n in SessionLocal().query(Notification)))

r = rpc("CancelTransaction", {"serviceId": 155, "transactionId": TID, "timestamp": now_local()})
check(10, "Повторная отмена — 202", code(r) == 202, r.json())
r = rpc("CheckTransaction", {"serviceId": 155, "transactionId": TID, "timestamp": now_local()})
check(11, "CheckTransaction: отменённая — 2", (r.json().get("result") or {}).get("transactionState") == 2)
r = rpc("GetStatement", {"serviceId": 155, "dateFrom": now_local(-60), "dateTo": now_local(60)})
check(12, "GetStatement после отмены — пусто", r.json()["result"]["statements"] == [])

# ------------------------------------------------------------ сверх тестера
r = rpc("CheckTransaction", {"serviceId": 155, "transactionId": "99999999999"})
check(13, "Неизвестная транзакция — состояние 3", (r.json().get("result") or {}).get("transactionState") == 3)
check(13, "Отмена неизвестной — 203", code(rpc("CancelTransaction", {
    "serviceId": 155, "transactionId": "99999999999"})) == 203)
check(13, "Без номера транзакции — 411", code(rpc("PerformTransaction", {
    "serviceId": 155, "amount": 1, "fields": {"client_id": "1"}})) == 411)

cash = new_order("paynet-key-002", payment="cash")
check(14, "Наличный заказ через Paynet не оплатить", code(rpc("PerformTransaction", perform_params(
    "1646338021400", cash["total"] * 100, cash["number"]))) == 302)

late = new_order("paynet-key-003")
s = SessionLocal()
s.query(Order).filter_by(number=late["number"]).update(
    {"created_at": utcnow() - timedelta(hours=1)}); s.commit(); s.close()
rpc("GetInformation", {"serviceId": 155, "fields": {"client_id": late["number"]}})
payments.cancel_stale(SessionLocal())
check(15, "Покупатель смотрит заказ в Paynet — автоотмена ждёт", row(late["number"]).status == "NEW")
s = SessionLocal()
s.query(Order).filter_by(number=late["number"]).update(
    {"checkout_at": utcnow() - timedelta(hours=1)}); s.commit(); s.close()
payments.cancel_stale(SessionLocal())
r = rpc("PerformTransaction", perform_params("1646338021401", late["total"] * 100, late["number"]))
check(15, "Отменённый заказ оплатить нельзя — деньги не спишутся", code(r) == 302
      and row(late["number"]).paid_at is None and SessionLocal().query(PaynetTransaction).filter_by(
          paynet_id="1646338021401").count() == 0)

inv = client.post(f"/api/orders/{new_order('paynet-key-004')['number']}/invoice", headers=BUYER).json()
check(16, "Приложению — что ввести в Paynet", inv.get("kind") == "paynet" and inv.get("order").isdigit()
      and inv.get("amount") and inv.get("link") is None, inv)

# До номера сервиса: логин и пароль уже переданы Paynet, номера ещё нет.
# Адрес должен отвечать — иначе Paynet не прогонит тесты и номер не выдаст
early = new_order("paynet-key-005")["number"]
payments.PAYNET_SERVICE_ID = ""
r = rpc("GetInformation", {"serviceId": 777, "fields": {"client_id": early}})
check(17, "Номера сервиса ещё нет — Paynet получает ответ (тесты пройдут)",
      r.status_code == 200 and r.json().get("result") is not None, r.text[:200])
check(17, "…без пароля по-прежнему 401", rpc("GetInformation", {"serviceId": 777}, headers={}).status_code == 401)
check(17, "…а покупатели Paynet не видят, пока номера нет", "paynet" not in payments.active())
payments.PAYNET_SERVICE_ID = "155"

# Тестовый счёт: Paynet гоняет сценарии сколько угодно раз, заказы не трогаем
from app import paynet as paynet_module  # noqa: E402
from app.models import Notification  # noqa: E402
payments.PAYNET_TEST = True
s = SessionLocal()
orders_before = s.query(Order).count()
notes_before = s.query(Notification).count()
s.close()
r = rpc("GetInformation", {"serviceId": 155, "fields": {"order_id": "1000001"}}).json()
check(18, "Тестовый счёт: сумма 5 000 сум", (r.get("result") or {}).get("fields", {}).get("amount") == 500000, r)
check(18, "Неверная сумма — 413", code(rpc("PerformTransaction", perform_params("1700000000001", 100, "1000001"))) == 413)
r = rpc("PerformTransaction", perform_params("1700000000002", 500000, "1000001")).json()
check(18, "Оплата проведена", (r.get("result") or {}).get("providerTrnId"), r)
check(18, "Повтор той же транзакции — 201",
      code(rpc("PerformTransaction", perform_params("1700000000002", 500000, "1000001"))) == 201)
r = rpc("CheckTransaction", {"serviceId": 155, "transactionId": "1700000000002"}).json()
check(18, "Проверка — проведена (1)", r["result"]["transactionState"] == 1)
r = rpc("GetStatement", {"serviceId": 155, "dateFrom": now_local(-60), "dateTo": now_local(60)}).json()
check(18, "В выписке есть", any(x["transactionId"] == 1700000000002 for x in r["result"]["statements"]))
r = rpc("CancelTransaction", {"serviceId": 155, "transactionId": "1700000000002"}).json()
check(18, "Отмена — 2, повторная отмена — 202", r["result"]["transactionState"] == 2 and code(
    rpc("CancelTransaction", {"serviceId": 155, "transactionId": "1700000000002"})) == 202)
s = SessionLocal()
check(18, "Ни заказов, ни сообщений сотрудникам", s.query(Order).count() == orders_before
      and s.query(Notification).count() == notes_before)
s.close()
from app import payments_report  # noqa: E402
payments.PAYNET_TEST = False
check(18, "В боевом режиме тестового счёта нет — 302",
      code(rpc("GetInformation", {"serviceId": 155, "fields": {"order_id": "1000001"}})) == 302)
payments.PAYNET_TEST = True

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
