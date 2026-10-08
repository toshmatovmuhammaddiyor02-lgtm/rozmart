"""Фискальный чек Payme/Click: покупателю в Telegram и в карточку заказа.

    .venv\\Scripts\\python.exe tests/receipt_scenarios.py

Telegram подменён. База — временный SQLite.
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
from datetime import datetime
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "NOTIFY_BOT_TOKEN": "222:notify-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import notify  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Notification, Order  # noqa: E402
from app.regos.orders_push import set_fiscal_qr  # noqa: E402

sent, fail = [], {}


def fake_call(method, payload, token=None):
    if "error" in fail:
        raise fail["error"]
    sent.append((method, payload, token))
    return {"message_id": 1}


notify.call = fake_call

URL = "https://ofd.soliq.uz/epi?t=EP000000000678&r=197860327&c=20261007100522&s=155974930937"

init_db()
db = SessionLocal()


def order(number, telegram_id=555):
    o = Order(number=number, status="CONFIRMED", telegram_id=telegram_id, customer_name="Азиз <b>",
              phone="+998901234567", address="Ташкент", delivery_slot="Как можно скорее",
              payment_method="online", pay_provider="payme", paid_at=datetime(2026, 10, 8, 6, 0),
              goods_total=59000, delivery_price=0, total=59000)
    db.add(o)
    db.commit()
    return o


results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def receipts(order_id):
    return list(db.scalars(select(Notification).where(
        Notification.order_id == order_id, Notification.kind == notify.RECEIPT)))


o = order("RB-9201")
set_fiscal_qr(db, o, URL)
rows = receipts(o.id)
check(1, "Чек пришёл — покупателю сообщение, одно", len(rows) == 1, len(rows))
check(1, "…от магазинного бота, в личный чат покупателя",
      sent and sent[-1][2] == "111:shop-token" and sent[-1][1]["chat_id"] == 555, sent[-1:])
text = sent[-1][1]["text"] if sent else ""
check(2, "В тексте номер заказа, ссылка на чек, на двух языках",
      "RB-9201" in text and "ofd.soliq.uz" in text and "Открыть чек" in text and "Chekni ochish" in text)
check(2, "Ссылка экранирована для HTML (&amp; в адресе)", "&amp;r=197860327" in text, text[:200])
check(2, "Отмечено отправленным", rows[0].sent_at is not None)

set_fiscal_qr(db, o, URL + "&again=1")
check(3, "Ссылка обновилась — второго сообщения нет", len(receipts(o.id)) == 1 and len(sent) == 1)

# покупатель не открывал чат с ботом — повторять бессмысленно
o2 = order("RB-9202", telegram_id=777)
fail["error"] = notify.NotifyError("sendMessage: 403 Forbidden: bot can't initiate conversation",
                                   permanent=True)
set_fiscal_qr(db, o2, URL)
db.expire_all()
r2 = receipts(o2.id)[0]
check(4, "Бот не может написать первым — не ушло, повторов не будет",
      r2.sent_at is None and r2.attempts >= notify.MAX_ATTEMPTS and "403" in (r2.error or ""))
check(4, "…и очередь его больше не берёт", r2.id not in [x.id for x in notify.pending(db)])

fail["error"] = notify.NotifyError("sendMessage: 502 Bad Gateway")
o3 = order("RB-9203", telegram_id=888)
set_fiscal_qr(db, o3, URL)
db.expire_all()
r3 = receipts(o3.id)[0]
check(5, "Временный сбой — останется в очереди на повтор",
      r3.sent_at is None and r3.attempts == 1 and r3.id in [x.id for x in notify.pending(db)])
fail.clear()
notify.sweep()
db.expire_all()
check(5, "Поток очереди дослал", receipts(o3.id)[0].sent_at is not None)

# карточка заказа в панели
user = json.dumps({"id": 1, "first_name": "Админ"})
fields = {"auth_date": str(int(time.time())), "user": user}
c = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
fields["hash"] = hmac.new(secret, c.encode(), hashlib.sha256).hexdigest()
card = TestClient(app).get(f"/api/admin/orders/{o.id}",
                           headers={"X-Telegram-Init-Data": urlencode(fields)}).json()
check(6, "В карточке заказа — ссылка на чек и платёжная система",
      card.get("fiscal_qr_url", "").startswith("https://ofd.soliq.uz") and card.get("pay_provider") == "payme",
      {k: card.get(k) for k in ("fiscal_qr_url", "pay_provider")})

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
