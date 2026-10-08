"""Отмена заказа кассиром: REGOS возвращает документ в «Новый».

    .venv\\Scripts\\python.exe tests/kassa_cancel_scenarios.py

Так это выглядело 8 октября 2026 на кассе SAMPI: кассир отменил три заказа,
а документы в REGOS стали «Новый» без кассы — не «Отменен». Синхронизация
статусов должна понять это как отмену: заказ отменён на витрине, сотрудникам
сообщение, документ в REGOS доведён до «Отменен».
База — временный SQLite. Выход с кодом 1, если хоть одна проверка не прошла.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "NOTIFY_BOT_TOKEN": "222:notify-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "test-key",
    "REGOS_ORDER_FROM_ID": "2",
})

from sqlalchemy import select  # noqa: E402

from app import courier_bot  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Notification, Order, StaffChat  # noqa: E402
from app.regos import orders_sync  # noqa: E402
from app.regos.client import RegosError  # noqa: E402

courier_bot.order_canceled = lambda order: None

# id документа -> статус в REGOS
DOCS: dict[int, int] = {}
NAMES = {22: "Новый", 23: "Утвержден", 24: "В обработке", 27: "Отменен"}
calls, fail = [], {}


class FakeRegos:
    def call(self, method, params=None, **kw):
        calls.append((method, params))
        if method in fail:
            raise fail[method]
        if method == "DocOrderDelivery/Get":
            f = params["filters"][0]
            ids = [int(x) for x in str(f["Value"]).split(",")]
            return {"result": [{"id": i, "status": {"id": DOCS[i], "name": NAMES[DOCS[i]]}}
                               for i in ids if i in DOCS]}
        if method == "DocOrderDelivery/SetStatus":
            DOCS[params["id"]] = 27 if params["status"] == "Canceled" else DOCS[params["id"]]
            return {"row_affected": 1}
        return {}


init_db()
db = SessionLocal()
db.add(StaffChat(chat_id=-100, title="Магазин", kind="group", active=True))


def order(number, status, doc, regos_status, error=None, paid=False):
    o = Order(number=number, status=status, telegram_id=5, customer_name="Азиз",
              phone="+998901234567", address="Ташкент", delivery_slot="Как можно скорее",
              payment_method="cash", goods_total=30000, delivery_price=0, total=30000,
              regos_document_id=doc, regos_error=error)
    db.add(o)
    DOCS[doc] = regos_status
    return o


order("RB-8067", "CONFIRMED", 46, 22)                 # в очереди, кассир отменил
order("RB-8068", "ASSEMBLING", 47, 22)                # уже принят, кассир отменил
order("RB-8070", "CONFIRMED", 49, 22, error="не подтверждён: таймаут")  # выгрузка не удалась
order("RB-8071", "CONFIRMED", 50, 23)                 # просто ждёт кассира
order("RB-8072", "ASSEMBLING", 51, 27)                # отменён в REGOS как положено
db.commit()

results = []


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def status(number):
    db.expire_all()
    return db.scalar(select(Order).where(Order.number == number))


r = orders_sync.sync(db, FakeRegos())
moved = {c["number"]: c["to"] for c in r["changed"]}
check(1, "Отменён кассиром в очереди («Новый») — отменён и у нас",
      status("RB-8067").status == "CANCELED", moved)
check(1, "Отменён кассиром после «Принять» — тоже", status("RB-8068").status == "CANCELED")
check(2, "Документы в REGOS доведены до «Отменен»", DOCS[46] == 27 and DOCS[47] == 27, DOCS)
texts = [n.text for n in db.scalars(select(Notification))]
check(3, "Сотрудникам — сообщение об отмене на кассе по каждому",
      sum("RB-8067" in t for t in texts) == 1 and sum("RB-8068" in t for t in texts) == 1, texts)
check(4, "Неудачная выгрузка («Новый» с ошибкой) — не отмена",
      status("RB-8070").status == "CONFIRMED" and DOCS[49] == 22)
check(5, "«Утвержден» — без изменений", status("RB-8071").status == "CONFIRMED")
check(6, "Обычная отмена в REGOS по-прежнему работает, лишний SetStatus не шлём",
      status("RB-8072").status == "CANCELED"
      and not any(p.get("id") == 51 for m, p in calls if m == "DocOrderDelivery/SetStatus"))

# второй проход: отменённые больше не трогаем и не шлём повторно
calls.clear()
before = db.query(Notification).count()
r = orders_sync.sync(db, FakeRegos())
check(7, "Повторный проход ничего не меняет и не дублирует сообщения",
      r["changed"] == [] and db.query(Notification).count() == before)

# REGOS не дал закрыть документ — заказ всё равно отменён, причина видна
order("RB-8073", "CONFIRMED", 52, 22)
db.commit()
fail["DocOrderDelivery/SetStatus"] = RegosError(1, "нет связи", "DocOrderDelivery/SetStatus")
orders_sync.sync(db, FakeRegos())
o = status("RB-8073")
check(8, "Не закрыли документ в REGOS — заказ отменён, а в карточке видно почему",
      o.status == "CANCELED" and "не закрыт" in (o.regos_error or ""), o.regos_error)

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
