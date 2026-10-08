"""Выгрузка оформленных заказов в REGOS.

Заказ уезжает документом DocOrderDelivery — тем же, которым в REGOS попадают
заказы из Uzum Tezkor и подобных сервисов. Метод AddFull создаёт шапку и строки
одним вызовом в одной транзакции: частично созданного заказа не бывает.

Идемпотентность. REGOS не умеет «создать, если ещё нет», поэтому от повторной
отправки защищает поле external_code — туда кладётся номер заказа витрины
(RB-8001). Перед отправкой проверяем, нет ли уже документа с таким кодом:
повторный вызов при ретрае или перезапуске сервиса не заведёт второй заказ.

Отправляет оператор кнопкой в панели либо, если включена автоотправка, витрина
сразу после оформления. В обоих случаях интеграция доводит документ только до
статуса «Утверждён» — дальше заказ забирает кассир, см. confirm().

Что нужно завести в REGOS до включения выгрузки:
  * источник заказов в справочнике DeliveryFrom (DeliveryFrom/Add) — сейчас там
    только «Uzum tezkor», для ROZMART нужен свой. Его id -> REGOS_ORDER_FROM_ID;
  * при необходимости тип доставки в DeliveryType -> REGOS_ORDER_DELIVERY_TYPE_ID.
Пока ORDER_FROM_ID не задан, выгрузка выключена и заказы просто копятся в базе:
витрина продолжает работать, ничего не теряется.
"""
import logging
import os

from app.models import Order
from app.regos import config, translit
from app.regos.client import RegosClient, RegosError

log = logging.getLogger(__name__)


class OrderPushError(Exception):
    """Заказ не удалось выгрузить. Витрине это не мешает: заказ уже принят."""


class ConfirmError(Exception):
    """Заказ выгружен, но подтвердить его не удалось. Документ в REGOS есть,
    и повторять выгрузку нельзя — иначе появится дубль. В очередь на кассу
    такой заказ не встанет, пока оператор не подтвердит его руками."""

    def __init__(self, message: str, document_id: int | None = None):
        super().__init__(message)
        self.document_id = document_id


def already_pushed(client: RegosClient, number: str) -> int | None:
    """id документа в REGOS, если заказ с таким номером уже выгружен.

    Смотрим на сами строки, а не на поле total: REGOS возвращает в нём общее
    число документов без учёта фильтра. Проверено — по несуществующему коду
    приходит пустой список при total = 4.
    """
    result = client.call("DocOrderDelivery/Get", {
        "filters": [{"Field": "external_code", "Operator": "Equal", "Value": number}],
        "limit": 1,
        "offset": 0,
    })
    rows = result.get("result") if isinstance(result, dict) else result
    return rows[0]["id"] if rows else None


def build_payload(order: Order, item_ids: dict[int, str],
                  customer_id: int | None = None, stock_id: int | None = None) -> dict:
    """Тело запроса AddFull.

    item_ids — соответствие variant_id -> код REGOS (Variant.external_code).
    Берётся заранее, чтобы не дёргать базу внутри цикла.

    Суммы в REGOS передаются как есть, в сумах: витрина хранит их целыми (BR-10),
    а доставка отдельной строкой не идёт — это не номенклатура, её учитывают
    в самом документе доставки.
    """
    document = {
        "external_code": order.number,
        "from_id": config.ORDER_FROM_ID,
        "stock_id": stock_id or config.STOCK_ID,
        # вид цены, по которому считался заказ: администратор мог с тех пор
        # переключить витрину на другой, а документ должен совпасть с заказом
        "price_type_id": order.price_type_id or config.PRICE_TYPE_ID,
        "date": unix(order.created_at),
        "address": order.address,
        "phone": order.phone,
        "description": _description(order),
        "payment_type_id": payment_type_id(order),
        # кассир видит срок доставки отдельным полем, а не только в описании
        "delivery_date": delivery_timestamp(order),
    }
    if order.lat is not None and order.lon is not None:
        # Поле location принимает объект и отдаёт его обратно как есть —
        # проверено round-trip'ом на живом документе. Строкой «широта,долгота»
        # тот же метод не принимает: ошибка 1008 по полю location.
        document["location"] = {"latitude": order.lat, "longitude": order.lon}
    if customer_id:
        document["customer_id"] = customer_id
    if order.fiscal_qr_url:
        # чек уже выдала платёжная система: касса второй не печатает
        document["qrcodeurl"] = order.fiscal_qr_url
    if config.ORDER_DELIVERY_TYPE_ID:
        document["delivery_type_id"] = config.ORDER_DELIVERY_TYPE_ID

    operations = []
    for position, item in enumerate(order.items, start=1):
        code = item_ids.get(item.variant_id)
        if code is None:
            raise OrderPushError(
                f"Заказ {order.number}: у фасовки {item.variant_id} нет кода REGOS. "
                "Похоже, товар заведён вручную и не синхронизирован."
            )
        # AddFull принимает либо item_id, либо item_code. Витрина хранит код
        # номенклатуры («003098»), а не внутренний id, поэтому item_code —
        # ведущие нули при переводе в число отпадают сами.
        operations.append({
            "item_code": int(code),
            "quantity": float(item.quantity),
            "price": float(item.price),
            "order": position,
        })

    return {"document": document, "operations": operations}


# у оплаты внутри Telegram платёжную систему выбирает BotFather — название не пишем
PROVIDER_TITLES = {"payme": "Payme", "click": "Click", "paynet": "Paynet"}


def payment_type_id(order: Order) -> int:
    """Форма оплаты в REGOS. Оплаченный онлайн — своя форма способа оплаты
    (задаётся в панели: «ROZMART Payme» и т. п.), иначе — «онлайн» по умолчанию."""
    if order.paid_at is not None and order.pay_provider:
        from app import payments
        own = payments.regos_payment_type(order.pay_provider)
        if own:
            return own
    return config.PAYMENT_TYPE_IDS.get(order.payment_method, 0)


def _description(order: Order) -> str:
    """Комментарий покупателя и срок доставки — оператору в REGOS они нужнее
    всего, а отдельных полей под них в документе нет.

    У оплаченного картой пометка стоит первой и заглавными: примечание — это
    всё, что кассир и курьер видят о деньгах. Способ оплаты «Пласт. карта»
    сам по себе не говорит, что деньги уже получены.
    """
    parts = []
    if order.paid_at is not None:
        from app import payments
        via = PROVIDER_TITLES.get(order.pay_provider or "", "")
        via = f" ({via})" if via else ""
        parts.append(f"ТЕСТ! ОПЛАЧЕНО ОНЛАЙН{via} (ТЕСТОВАЯ ОПЛАТА), ДЕНЬГИ НЕ БРАТЬ"
                     if payments.is_test(order.pay_provider)
                     else f"ОПЛАЧЕНО ОНЛАЙН{via}, ДЕНЬГИ НЕ БРАТЬ")
    parts += [f"ROZMART {order.number}", order.customer_name, order.delivery_slot]
    if order.comment:
        parts.append(order.comment.strip())
    return " | ".join(p for p in parts if p) + "\n" + _receipt_lines(order)


# Сколько позиций печатать: листок у кассы узкий, а примечание не резиновое
RECEIPT_ITEMS = 25


def _sum(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _receipt_lines(order: Order) -> str:
    """Состав заказа и суммы — в конец примечания.

    Листок, который печатает касса, собирает REGOS по своему шаблону, и
    позиций документа в нём нет: только покупатель, адрес и примечание.
    Сборщику и курьеру нужен состав, поэтому кладём его сюда, по строке на
    позицию: «название / количество × цена = сумма». Ниже — доставка и итог.
    """
    lines = ["---- Состав заказа ----"]
    for item in order.items[:RECEIPT_ITEMS]:
        lines.append(f"{item.product_name} {item.weight}".strip())
        lines.append(f"  {item.quantity} x {_sum(item.price)} = {_sum(item.price * item.quantity)}")
    if len(order.items) > RECEIPT_ITEMS:
        rest = order.items[RECEIPT_ITEMS:]
        lines.append(f"…и ещё {len(rest)} поз. на {_sum(sum(i.price * i.quantity for i in rest))}")
    lines += [
        "-----------------------",
        f"Товары: {_sum(order.goods_total)} сум",
        f"Доставка: {_sum(order.delivery_price)} сум" if order.delivery_price else "Доставка: бесплатно",
        f"ИТОГО: {_sum(order.total)} сум",
    ]
    return "\n".join(lines)


def push_order(client: RegosClient, order: Order, item_ids: dict[int, str],
               cash_id: int = 0, stock_id: int | None = None) -> int:
    """Отправляет заказ в REGOS. Возвращает id созданного документа.

    Повторный вызов для уже выгруженного заказа ничего не создаёт и возвращает
    id существующего документа. cash_id — касса из панели (operating_cash),
    0 — общая очередь склада; stock_id — склад витрины (config.stock_id).
    """
    if not config.orders_enabled():
        raise OrderPushError(
            "Выгрузка заказов не настроена: нет REGOS_ORDER_FROM_ID. "
            "Заведите источник заказов в справочнике DeliveryFrom."
        )

    if existing := already_pushed(client, order.number):
        log.info("Заказ %s уже в REGOS (документ %s)", order.number, existing)
        return existing

    customer_id = ensure_customer(client, order)
    try:
        result = client.call(
            "DocOrderDelivery/AddFull", build_payload(order, item_ids, customer_id, stock_id)
        )
    except RegosError as exc:
        # 1029 — не хватает остатка: витрина продала то, чего нет на складе.
        # Это не сбой обмена, а расхождение данных, и оператору о нём надо знать.
        raise OrderPushError(f"Заказ {order.number}: {exc.description}") from exc

    new_id = result["new_id"]
    log.info("Заказ %s выгружен в REGOS, документ %s", order.number, new_id)
    if order.fiscal_qr_url:
        # ссылка на чек ушла в самом документе — SetFiscalInfo уже не нужен
        from app.models import utcnow
        order.fiscal_qr_regos_at = utcnow()

    try:
        confirm(client, new_id)
    except (RegosError, RuntimeError, OSError) as exc:
        # документ уже создан — сообщаем, но выгрузку неудачной не считаем
        log.warning("Заказ %s: не подтверждён: %s", order.number, exc)
        raise ConfirmError(
            f"Заказ выгружен (документ {new_id}), но не подтверждён: {exc}",
            document_id=new_id,
        ) from exc

    if cash_id:
        try:
            assign_cash(client, new_id, cash_id)
        except (RegosError, RuntimeError, OSError) as exc:
            # заказ всё равно у кассиров — в общей очереди склада
            log.warning("Заказ %s: касса %s не назначена: %s", order.number, cash_id, exc)
            raise ConfirmError(
                f"Заказ выгружен (документ {new_id}) и стоит в общей очереди: "
                f"на кассу {cash_id} поставить не удалось: {exc}",
                document_id=new_id,
            ) from exc
    return new_id


# ------------------------------------------------------------- пакетная выгрузка

# Заказ уезжает в REGOS только после подтверждения. NEW — это «ждёт оплаты»
# (BR-13): передавать такой в магазин рано, его ещё могут не оплатить.
# Отменённые не отправляем вовсе.
PUSHABLE = {"CONFIRMED", "ASSEMBLING", "DELIVERING", "DONE"}

# После стольких неудач перестаём долбить REGOS и оставляем заказ оператору:
# если не вышло десять раз подряд, дело не во временном сбое.
MAX_ATTEMPTS = 10


def pending(db) -> list:
    """Подтверждённые заказы, которые ещё не уехали в REGOS."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from app.models import Order

    return list(db.scalars(
        select(Order)
        .where(
            Order.regos_document_id.is_(None),
            Order.status.in_(PUSHABLE),
            Order.regos_attempts < MAX_ATTEMPTS,
        )
        .order_by(Order.id)
        .options(selectinload(Order.items))
    ))


def item_codes(db, orders: list) -> dict[int, str]:
    """Коды REGOS по id фасовок всех переданных заказов — одним запросом,
    а не по запросу на строку."""
    from sqlalchemy import select

    from app.models import Variant

    ids = {item.variant_id for order in orders for item in order.items}
    if not ids:
        return {}
    rows = db.execute(
        select(Variant.id, Variant.external_code).where(Variant.id.in_(ids))
    ).all()
    return dict(rows)


def push_pending(db, client: RegosClient | None = None) -> dict:
    """Отправляет все ожидающие заказы. Возвращает сводку.

    Ошибка по одному заказу не мешает остальным: каждый фиксируется отдельно.
    Текст ошибки сохраняется в заказе — оператор видит его в панели и понимает,
    что случилось, не читая журналы сервера.
    """
    if not config.orders_enabled():
        return {"enabled": False, "sent": 0, "failed": 0,
                "reason": "не задан REGOS_ORDER_FROM_ID"}

    orders = pending(db)
    if not orders:
        return {"enabled": True, "sent": 0, "failed": 0, "pending": 0}

    client = client or RegosClient()
    codes = item_codes(db, orders)
    sent = failed = 0

    for order in orders:
        order.regos_attempts += 1
        try:
            order.regos_document_id = push_order(client, order, codes, operating_cash(db),
                                                 config.stock_id(db))
            order.regos_error = None
            sent += 1
        except ConfirmError as exc:
            # документ создан — записываем его id, иначе следующая попытка
            # заведёт дубль. Ошибку показываем оператору
            order.regos_document_id = exc.document_id
            order.regos_error = str(exc)[:500]
            sent += 1
        except (OrderPushError, RegosError) as exc:
            order.regos_error = str(exc)[:500]
            failed += 1
            log.warning("Заказ %s не выгружен: %s", order.number, exc)
        except (RuntimeError, OSError) as exc:
            # сеть или сам REGOS недоступен: это не про конкретный заказ,
            # дальше идти смысла нет — попробуем в следующий раз
            order.regos_error = str(exc)[:500]
            failed += 1
            log.warning("Обмен с REGOS прерван: %s", exc)
            db.commit()
            break
        db.commit()

    return {"enabled": True, "sent": sent, "failed": failed,
            "pending": len(orders) - sent}


def confirm(client: RegosClient, document_id: int) -> None:
    """Подтверждает заказ, чтобы он встал в очередь на кассе.

    Жизненный цикл заказа в REGOS:
        Новый -> Утверждён -> В обработке -> Оплата (продажа) -> Завершён

    Без выбранной в панели кассы интеграция доводит документ ровно до
    «Утверждён» и останавливается: заказ в общей очереди склада, кассир
    нажимает «Принять», и REGOS сам привязывает его кассу и переводит
    в «В обработке». С выбранной кассой — см. assign_cash().

    Чего делать НЕЛЬЗЯ, проверено на живых заказах: переводить в «Оплата
    (продажа)». Это состояние означает «заказ уже взят в оплату на конкретной
    кассе» — в очереди его нет, и заказы переставали доходить до кассира,
    хотя в REGOS выглядели проведёнными.

    Неудача здесь не отменяет саму выгрузку: заказ уже в REGOS, и потерять его
    было бы хуже. Оператор увидит ошибку в панели и подтвердит документ руками.
    """
    client.call("DocOrderDelivery/SetStatus", {"id": document_id, "status": "Approved"})
    log.info("Документ %s подтверждён и ждёт приёма на кассе", document_id)


# --------------------------------------------------------------------- касса

# На какую кассу склада ставить заказ. Выбирается в панели. 0 — общая очередь:
# заказ ждёт «Принять» у любого кассира склада, как заказы Uzum.
SETTING_CASH = "regos_operating_cash_id"


def operating_cash(db) -> int:
    from app.models import Setting

    row = db.get(Setting, SETTING_CASH)
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


def save_operating_cash(db, cash_id: int) -> None:
    from app.models import Setting

    row = db.get(Setting, SETTING_CASH)
    if row is None:
        db.add(Setting(name=SETTING_CASH, value=str(cash_id)))
    else:
        row.value = str(cash_id)
    db.commit()


def assign_cash(client: RegosClient, document_id: int, cash_id: int) -> None:
    """Ставит подтверждённый заказ на кассу; REGOS сам переводит его
    в «В обработке». Только после «Утверждён»: к неподтверждённому документу
    кассу REGOS не привязывает (ошибка 1064).

    В первой версии привязка была, и заказы до кассира не доходили. Но тогда
    сошлись две ошибки: выбрана была касса 7, за которой ни разу не открывали
    смену, а документ сразу уводился в «Оплата (продажа)». Поэтому в панели
    рядом с кассой видна её последняя смена, а «Оплату» здесь не ставим.
    """
    client.call("DocOrderDelivery/SetOperatingCash",
                {"id": document_id, "operating_cash_id": cash_id})
    log.info("Документ %s поставлен на кассу %s", document_id, cash_id)


def cashes(client: RegosClient, stock_id: int | None = None) -> list[dict]:
    """Кассы склада витрины и последняя смена на каждой.

    Названий у касс в REGOS нет, только id. Различить их можно по сменам:
    код смены — номер, который кассир видит на экране («11-0000532»),
    и имя кассира, открывшего смену.
    """
    def rows(result):
        return (result.get("result") if isinstance(result, dict) else result) or []

    found = []
    for cash in rows(client.call("OperatingCash/Get", {})):
        if (cash.get("stock") or {}).get("id") != (stock_id or config.STOCK_ID):
            continue
        sessions = rows(client.call("DocCashSession/Get", {"limit": 1, "offset": 0, "filters": [
            {"Field": "operating_cash_id", "Operator": "Equal", "Value": str(cash["id"])}]}))
        last = None
        if sessions:
            s = sessions[0]
            last = {
                "code": s.get("code"),
                "start": s.get("start_date") or None,
                "closed": bool(s.get("closed")),
                "close": s.get("close_date") or None,
                "cashier": (s.get("start_user") or {}).get("full_name"),
            }
        found.append({"id": cash["id"], "description": cash.get("description") or "",
                      "last_session": last})
    # рабочие — первыми: в первой версии кассу без смен выбрали только потому,
    # что она стояла в списке первой
    found.sort(key=lambda c: -((c["last_session"] or {}).get("start") or 0))
    return found


# ------------------------------------------------- покупатель и срок доставки

# Группа, в которую попадают покупатели из витрины. Заводится один раз
# (RetailCustomerGroup/Add), в REGOS их до этого не было вовсе.
CUSTOMER_GROUP_ID = int(os.getenv("REGOS_CUSTOMER_GROUP_ID", "1"))

# Часовой пояс магазина. Сервер живёт в UTC, а сроки доставки покупатель
# понимает по-ташкентски; в Узбекистане перевода часов нет, поэтому просто +5.
SHOP_UTC_OFFSET = 5

# Во сколько по местному времени приходится каждый срок доставки.
SLOT_HOURS = {
    "Сегодня вечером": (0, 19),
    "Завтра утром": (1, 10),
}


def unix(moment) -> int:
    """Наивное время из базы (оно всегда UTC) -> Unix-время.

    Через .timestamp() напрямую нельзя: у времени без пояса этот метод считает
    его по часовому поясу машины. На сервере в UTC совпало бы случайно, а на
    любой другой машине дало бы сдвиг — так и вышло при проверке: срок доставки
    уехал на пять часов.
    """
    from datetime import timezone

    return int(moment.replace(tzinfo=timezone.utc).timestamp())


def delivery_timestamp(order) -> int:
    """Срок доставки как Unix-время.

    Витрина хранит срок словами («Завтра утром»), а REGOS ждёт отметку времени
    и показывает её кассиру в поле «Дата и время доставки». «Как можно скорее»
    и всё нераспознанное считаем ближайшими двумя часами.
    """
    from datetime import timedelta

    shift, hour = SLOT_HOURS.get(order.delivery_slot, (None, None))
    if shift is None:
        return unix(order.created_at + timedelta(hours=2))

    local = order.created_at + timedelta(hours=SHOP_UTC_OFFSET, days=shift)
    local = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return unix(local - timedelta(hours=SHOP_UTC_OFFSET))


def ensure_customer(client: RegosClient, order) -> int | None:
    """Находит покупателя по телефону или заводит карточку.

    Без неё касса показывает пустые «Покупатель» и «Телефон»: это поля
    из карточки, а не из самого документа — телефон в документе есть,
    но на экран кассира он не попадает.

    Имя переводится в латиницу: REGOS не принимает кириллицу в именах,
    см. app/regos/translit.py. Исходное имя остаётся в описании документа.

    Ошибка здесь не срывает выгрузку: заказ важнее карточки.
    """
    phone = (order.phone or "").strip()
    if not phone:
        return None

    # REGOS хранит телефон без «плюса» — ищем в обоих видах
    for value in (phone, phone.lstrip("+")):
        try:
            result = client.call("RetailCustomer/Get", {
                "filters": [{"Field": "main_phone", "Operator": "Equal", "Value": value}],
                "limit": 1, "offset": 0,
            })
        except (RegosError, RuntimeError, OSError):
            return None
        rows = result.get("result") if isinstance(result, dict) else result
        if rows:
            return rows[0]["id"]

    first, last = translit.customer_name(order.customer_name)
    payload = {"first_name": first, "group_id": CUSTOMER_GROUP_ID,
               "main_phone": phone.lstrip("+")}
    if last:
        payload["last_name"] = last
    try:
        return client.call("RetailCustomer/Add", payload)["new_id"]
    except (RegosError, RuntimeError, OSError) as exc:
        log.warning("Карточка покупателя не создана для %s: %s", order.number, exc)
        return None


# --------------------------------------------------------------- автоотправка

# Отправлять ли заказ в REGOS сразу после оформления. По умолчанию выключено:
# оператор сперва смотрит заказ и отправляет кнопкой. Включается в панели,
# когда поток заказов вырос и разбирать каждый руками некогда.
SETTING_AUTO = "regos_auto_push"


def auto_push_enabled(db) -> bool:
    from app.models import Setting

    row = db.get(Setting, SETTING_AUTO)
    return bool(row and row.value == "1")


def save_auto_push(db, enabled: bool) -> None:
    from app.models import Setting

    value = "1" if enabled else "0"
    row = db.get(Setting, SETTING_AUTO)
    if row is None:
        db.add(Setting(name=SETTING_AUTO, value=value))
    else:
        row.value = value
    db.commit()


def auto_push_ready(db) -> tuple[bool, str]:
    """Готова ли автоотправка. Вторым значением — причина, если нет.

    Причину показываем в панели рядом с переключателем: включённая автоотправка,
    которая молча ничего не делает, — это ровно та ошибка, из-за которой заказы
    уже один раз не дошли до кассы.
    """
    if not config.orders_enabled():
        return False, "не задан REGOS_ORDER_FROM_ID"
    return True, ""


def push_one(order_id: int) -> None:
    """Отправляет один заказ. Вызывается фоном после оформления.

    Своя сессия базы: та, в которой создавался заказ, к этому моменту уже
    закрыта вместе с запросом.
    """
    from app.db import SessionLocal
    from app.models import Order

    db = SessionLocal()
    try:
        ready, reason = auto_push_ready(db)
        if not ready or not auto_push_enabled(db):
            if not ready:
                log.warning("Автоотправка заказа %s пропущена: %s", order_id, reason)
            return

        # Блокировка строки на время выгрузки. Выгрузку одного заказа могут
        # запустить дважды почти одновременно — автоотправка и повтор
        # уведомления об оплате, автоотправка и кнопка оператора. Обе увидели бы
        # «документа ещё нет» и обе создали бы документ: у кассира два заказа.
        # Второй ждёт первого и видит уже выгруженный заказ
        order = db.get(Order, order_id, with_for_update=True)
        if order is None or order.regos_document_id or order.status not in PUSHABLE:
            return

        order.regos_attempts += 1
        try:
            order.regos_document_id = push_order(
                RegosClient(), order, item_codes(db, [order]), operating_cash(db),
                config.stock_id(db),
            )
            order.regos_error = None
        except ConfirmError as exc:
            order.regos_document_id = exc.document_id
            order.regos_error = str(exc)[:500]
        except (OrderPushError, RegosError, RuntimeError, OSError) as exc:
            order.regos_error = str(exc)[:500]
            log.warning("Автоотправка заказа %s не удалась: %s", order.number, exc)
        db.commit()
    except Exception:                       # noqa: BLE001
        # фоновая задача не должна ронять сервер: заказ уже принят,
        # оператор увидит его в панели и отправит кнопкой
        log.exception("Сбой автоотправки заказа %s", order_id)
    finally:
        db.close()


# ------------------------------------------------- фискальный чек онлайн-оплаты

# Сколько раз пробуем записать ссылку на чек в заказ REGOS. Не выходит, если
# кассир уже провёл заказ (документ закрыт) — тогда чек напечатала касса,
# и дальше стучаться незачем
FISCAL_QR_TRIES = 10


def send_fiscal_qr(order_id: int) -> None:
    """Записывает в заказ REGOS ссылку на фискальный чек платёжной системы
    (DocOrderDelivery/SetFiscalInfo): с ней касса заказ повторно не
    фискализирует. Заказ ещё не выгружен — ссылка уйдёт при выгрузке."""
    from app.db import SessionLocal
    from app.models import Order, utcnow

    db = SessionLocal()
    try:
        order = db.get(Order, order_id, with_for_update=True)
        if (order is None or not order.fiscal_qr_url or not order.regos_document_id
                or order.fiscal_qr_regos_at is not None
                or order.fiscal_qr_regos_tries >= FISCAL_QR_TRIES
                or not config.is_configured()):
            return
        order.fiscal_qr_regos_tries += 1
        try:
            RegosClient().call("DocOrderDelivery/SetFiscalInfo", {
                "id": order.regos_document_id, "qrcodeurl": order.fiscal_qr_url})
            order.fiscal_qr_regos_at = utcnow()
            order.fiscal_qr_regos_error = None
            log.info("Заказ %s: ссылка на чек передана в REGOS", order.number)
        except (RegosError, RuntimeError, OSError) as exc:
            order.fiscal_qr_regos_error = str(exc)[:500]
            log.warning("Заказ %s: ссылка на чек не передана в REGOS: %s", order.number, exc)
        db.commit()
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Сбой передачи чека в REGOS по заказу %s", order_id)
    finally:
        db.close()


def fiscal_qr_pending(db) -> None:
    """Повторяет передачу ссылок на чек, которые не ушли сразу."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.models import Order, utcnow

    ids = db.scalars(select(Order.id).where(
        Order.fiscal_qr_url.is_not(None),
        Order.regos_document_id.is_not(None),
        Order.fiscal_qr_regos_at.is_(None),
        Order.fiscal_qr_regos_tries < FISCAL_QR_TRIES,
        Order.paid_at > utcnow() - timedelta(days=2),
    )).all()
    for order_id in ids:
        send_fiscal_qr(order_id)


def set_fiscal_qr(db, order, url: str, background=None) -> None:
    """Платёжная система выдала чек: запоминаем ссылку у заказа и, если
    заказ уже в REGOS, передаём её туда. Коммит — здесь."""
    if not url or order is None or order.fiscal_qr_url == url:
        return
    order.fiscal_qr_url = url
    order.fiscal_qr_regos_at = None
    order.fiscal_qr_regos_tries = 0
    db.commit()
    if order.regos_document_id:
        if background is not None:
            background.add_task(send_fiscal_qr, order.id)
        else:
            send_fiscal_qr(order.id)


# --------------------------------------------------------------- отмена заказа

# Статус «Отменен» в справочнике REGOS. Нужен, чтобы отличить документ, который
# уже отменён, и не звать SetStatus впустую.
CANCELED_STATUS_ID = 27


def document_status(client: RegosClient, document_id: int) -> dict | None:
    """Статус документа заказа. None — документа в REGOS больше нет."""
    result = client.call("DocOrderDelivery/Get", {
        "filters": [{"Field": "id", "Operator": "Equal", "Value": document_id}],
        "limit": 1,
        "offset": 0,
    })
    rows = result.get("result") if isinstance(result, dict) else result
    if not rows:
        return None
    status = rows[0].get("status")
    return status if isinstance(status, dict) else None


def cancel_document(client: RegosClient, document_id: int) -> str:
    """Отменяет документ в REGOS. Возвращает, что получилось.

    Удалить документ через API нельзя: метод DocOrderDelivery/Delete есть,
    но на любом документе — «Новый», «Утвержден», «Отменен» — отвечает ошибкой
    1009 «records to update or delete were not found». Проверено на трёх
    документах и на всех вариантах параметров. Поэтому и при удалении заказа
    на витрине документ в REGOS именно отменяется: кассир видит, что заказ
    больше не нужен, и по нему ничего не отгружает.
    """
    status = document_status(client, document_id)
    if status is None:
        return "missing"                     # документа нет — отменять нечего
    if status.get("id") == CANCELED_STATUS_ID:
        return "already"
    client.call("DocOrderDelivery/SetStatus", {"id": document_id, "status": "Canceled"})
    return "canceled"


def cancel_one(order_id: int) -> None:
    """Отменяет документ заказа в REGOS. Вызывается фоном после отмены в панели.

    Своя сессия базы: та, в которой оператор менял состояние, уже закрыта.
    Ошибку не выбрасываем — заказ на витрине отменён в любом случае, а причина
    остаётся в карточке, чтобы оператор увидел: на кассе заказ ещё живой.
    """
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        order = db.get(Order, order_id)
        if order is None or not order.regos_document_id:
            return
        try:
            done = cancel_document(RegosClient(), order.regos_document_id)
            if done == "canceled":
                order.regos_error = None
                log.info("Заказ %s отменён и в REGOS", order.number)
            elif done == "missing":
                log.info("Заказ %s: документ %s в REGOS не найден",
                         order.number, order.regos_document_id)
        except (RegosError, RuntimeError, OSError) as exc:
            order.regos_error = f"не отменён в REGOS: {exc}"[:500]
            log.warning("Заказ %s не отменён в REGOS: %s", order.number, exc)
        db.commit()
    except Exception:                       # noqa: BLE001
        log.exception("Сбой отмены заказа %s в REGOS", order_id)
    finally:
        db.close()
