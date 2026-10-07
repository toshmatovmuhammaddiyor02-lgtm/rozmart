"""Оплата через Payme Merchant API.

Покупатель нажимает «Оплатить», приложение открывает страницу Payme
(`checkout_url`), там он платит картой или в приложении Payme. Всё остальное
Payme делает сам, обращаясь к нашему адресу /payme по протоколу JSON-RPC:

1. CheckPerformTransaction — можно ли оплатить этот заказ на эту сумму.
   В ответе — состав чека: без него Payme не передаст чек в налоговую;
2. CreateTransaction — покупатель начал платить. Заказ бронируем: пока идёт
   оплата, автоотмена его не трогает;
3. PerformTransaction — деньги списаны. Только здесь заказ становится
   оплаченным и уходит дальше тем же путём, что после оплаты в Telegram:
   сотрудникам и в REGOS;
4. CancelTransaction — оплата брошена или сделан возврат из кабинета Payme;
5. CheckTransaction, GetStatement — сверка с нашей стороны.

Payme повторяет запросы, если не дождался ответа, поэтому каждый метод при
повторе с тем же id возвращает тот же результат, а не делает дело второй раз.

Ответ всегда HTTP 200: ошибка — в теле, с кодом из протокола и текстом на
трёх языках. Текст ошибки по заказу Payme показывает покупателю.

Доступ — по ключу кассы в заголовке Authorization (Basic, логин «Paycom»).
Можно дополнительно ограничить адреса, с которых Payme ходит к нам
(PAYME_ALLOWED_IPS, адреса и подсети): по документации Payme это
185.234.113.1–185.234.113.15.

SetFiscalData — необязательный метод: Payme присылает в него фискальный чек
из налоговой после проведения. Сохраняем его у транзакции, а ссылку на чек
передаём в заказ REGOS: с ней касса не печатает второй чек на ту же продажу.
"""
import base64
import hmac
import ipaddress
import json
import logging
import os
import time

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import notify, order_status, payments
from app.db import get_db
from app.models import Order, OrderItem, PaymeTransaction, utcnow

log = logging.getLogger(__name__)

router = APIRouter(tags=["Payme"])

WEBHOOK_PATH = "/payme"

# Поле заказа в «account». Так же оно должно называться в настройках кассы Payme
ACCOUNT_FIELD = "order_id"

# Страница оплаты. У тестовой кассы своя — checkout.test.paycom.uz.
# Не путать с test.paycom.uz: там инструмент проверки кассы (песочница),
# а не страница оплаты, хотя документация Payme называет его «URL отправки
# чека в песочницу» — по ссылке оттуда покупатель увидит форму Merchant ID
CHECKOUT_OVERRIDE = os.getenv("PAYME_CHECKOUT_URL", "").strip().rstrip("/")


def checkout_base() -> str:
    """Страница оплаты — по текущему режиму: его переключают в панели."""
    if CHECKOUT_OVERRIDE:
        return CHECKOUT_OVERRIDE
    payments.refresh()
    return "https://checkout.test.paycom.uz" if payments.PAYME_TEST else "https://checkout.paycom.uz"

# Куда Payme вернёт покупателя после оплаты — обычно ссылка на бота магазина
RETURN_URL = os.getenv("PAYME_RETURN_URL", "").strip()

# адреса и подсети через запятую, например 185.234.113.0/28
ALLOWED_NETS = [ipaddress.ip_network(x.strip(), strict=False)
                for x in os.getenv("PAYME_ALLOWED_IPS", "").split(",") if x.strip()]


def allowed_ip(host: str | None) -> bool:
    if not ALLOWED_NETS:
        return True
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(address in net for net in ALLOWED_NETS)

# Созданная, но не проведённая транзакция живёт 12 часов — так по протоколу
TIMEOUT_MS = 12 * 60 * 60 * 1000

# Состояния транзакции по протоколу Payme
CREATED, PERFORMED, CANCELED, CANCELED_AFTER = 1, 2, -1, -2
# Причина отмены «по таймауту»
REASON_TIMEOUT = 4

# Код НДС по умолчанию, если в REGOS его нет. 12% — ставка в Узбекистане
DEFAULT_VAT = 12


def now_ms() -> int:
    return int(time.time() * 1000)


class PaymeError(Exception):
    def __init__(self, code: int, ru: str, uz: str | None = None, en: str | None = None,
                 data: str | None = None):
        super().__init__(ru)
        self.code, self.data = code, data
        self.message = {"ru": ru, "uz": uz or ru, "en": en or ru}


# ------------------------------------------------------------ ошибки протокола

def err_auth():
    return PaymeError(-32504, "Недостаточно привилегий", "Ruxsat yo'q", "Insufficient privileges")


def err_method():
    return PaymeError(-32601, "Метод не найден", "Metod topilmadi", "Method not found")


def err_request():
    return PaymeError(-32600, "Неверный запрос", "Noto'g'ri so'rov", "Invalid request")


def err_amount():
    return PaymeError(-31001, "Неверная сумма", "Noto'g'ri summa", "Invalid amount")


def err_not_found_tx():
    return PaymeError(-31003, "Транзакция не найдена", "Tranzaksiya topilmadi",
                      "Transaction not found")


def err_state():
    return PaymeError(-31008, "Операцию выполнить нельзя", "Amalni bajarib bo'lmaydi",
                      "Unable to perform operation")


def err_order(code: int, ru: str, uz: str, en: str):
    # ошибки по заказу: -31050…-31099, с именем поля — Payme его подсвечивает
    return PaymeError(code, ru, uz, en, data=ACCOUNT_FIELD)


ORDER_MISSING = (-31050, "Заказ не найден", "Buyurtma topilmadi", "Order not found")
ORDER_CASH = (-31051, "Заказ оформлен с оплатой наличными",
              "Buyurtma naqd pulga rasmiylashtirilgan", "Order is paid in cash")
ORDER_PAID = (-31052, "Заказ уже оплачен", "Buyurtma allaqachon to'langan", "Order already paid")
ORDER_CANCELED = (-31053, "Заказ отменён", "Buyurtma bekor qilingan", "Order canceled")
ORDER_BUSY = (-31054, "Заказ уже в работе у магазина", "Buyurtma allaqachon ishlanmoqda",
              "Order is already being processed")
ORDER_PAYING = (-31055, "Заказ уже оплачивается", "Buyurtma to'lanmoqda",
                "Order is being paid")


# ------------------------------------------------------------ ссылка на оплату

def checkout_url(order: Order, lang: str = "ru") -> str:
    """Страница оплаты заказа в Payme. Параметры — base64 от «k=v;k=v».
    lang — язык страницы Payme: тот же, что у покупателя в приложении."""
    params = [f"m={payments.PAYME_MERCHANT_ID}", f"ac.{ACCOUNT_FIELD}={order.number}",
              f"a={order.total * payments.MINOR}", f"l={'uz' if lang == 'uz' else 'ru'}"]
    if RETURN_URL:
        params.append(f"c={RETURN_URL}")
    encoded = base64.b64encode(";".join(params).encode()).decode()
    return f"{checkout_base()}/{encoded}"


# ------------------------------------------------------------ чек

def receipt(order: Order) -> dict:
    """Состав фискального чека — для ответа на CheckPerformTransaction.

    Сумма строк обязана сойтись с заказом до тийина: Payme её сверяет.
    МХИК, код упаковки и НДС — из фасовки (приходят из REGOS). Если кода
    упаковки нет, строку всё равно отдаём, но пишем в журнал: такой чек
    налоговая может не принять, и заполнить код надо в REGOS.
    """
    items = []
    for item in order.items:
        variant = item.variant
        row = {
            "title": f"{item.product_name} {item.weight}".strip()[:128],
            "price": item.price * payments.MINOR,
            "count": item.quantity,
            "code": (variant.mxik if variant else None) or "",
            "vat_percent": (variant.vat_percent if variant and variant.vat_percent is not None
                            else DEFAULT_VAT),
        }
        if variant and variant.package_code:
            row["package_code"] = variant.package_code
        else:
            log.warning("Чек %s: у «%s» нет кода упаковки", order.number, item.product_name)
        if not row["code"]:
            log.warning("Чек %s: у «%s» нет кода МХИК", order.number, item.product_name)
        items.append(row)
    detail = {"receipt_type": 0, "items": items}
    if order.delivery_price:
        detail["shipping"] = {"title": "Доставка", "price": order.delivery_price * payments.MINOR}
    return detail


# ------------------------------------------------------------ заказ и транзакция

def _order_by_account(db: Session, params: dict) -> Order:
    account = params.get("account")
    if not isinstance(account, dict):
        raise err_request()
    number = str(account.get(ACCOUNT_FIELD) or "").strip()
    order = db.scalar(select(Order).where(Order.number == number).options(
        selectinload(Order.items).selectinload(OrderItem.variant)))
    if order is None:
        raise err_order(*ORDER_MISSING)
    return order


def _check_payable(order: Order, amount) -> None:
    """Можно ли оплатить заказ этой суммой. Иначе — ошибка для Payme."""
    if order.payment_method != "online":
        raise err_order(*ORDER_CASH)
    if order.paid_at is not None:
        raise err_order(*ORDER_PAID)
    if order.status == "CANCELED":
        raise err_order(*ORDER_CANCELED)
    if order.status != "NEW":
        raise err_order(*ORDER_BUSY)
    if not isinstance(amount, int) or amount != order.total * payments.MINOR:
        log.warning("Payme: заказ %s, сумма %s, а в заказе %s",
                    order.number, amount, order.total * payments.MINOR)
        raise err_amount()


def _tx(db: Session, params: dict, lock: bool = False) -> PaymeTransaction:
    payme_id = params.get("id")
    if not isinstance(payme_id, str) or not payme_id:
        raise err_request()
    stmt = select(PaymeTransaction).where(PaymeTransaction.payme_id == payme_id)
    if lock:
        stmt = stmt.with_for_update()
    tx = db.scalar(stmt)
    if tx is None:
        raise err_not_found_tx()
    return tx


def _expired(tx: PaymeTransaction) -> bool:
    return now_ms() - tx.create_time > TIMEOUT_MS


def _mark_checkout(order: Order) -> None:
    """Покупатель на странице оплаты: пока платит, автоотмена заказ не трогает.
    Только продлеваем — отметку, поставленную на будущее (тестовый заказ
    для песочницы), очередной запрос Payme не сокращает."""
    now = utcnow()
    if order.checkout_at is None or order.checkout_at < now:
        order.checkout_at = now


def _cancel(tx: PaymeTransaction, reason: int) -> None:
    tx.state = CANCELED_AFTER if tx.state == PERFORMED else CANCELED
    tx.reason = reason
    tx.cancel_time = now_ms()


# ------------------------------------------------------------ методы

def check_perform(db: Session, params: dict) -> dict:
    order = _order_by_account(db, params)
    _check_payable(order, params.get("amount"))
    _mark_checkout(order)
    db.commit()
    return {"allow": True, "detail": receipt(order)}


def create(db: Session, params: dict) -> dict:
    payme_id = params.get("id")
    if not isinstance(payme_id, str) or not payme_id or not isinstance(params.get("time"), int):
        raise err_request()

    tx = db.scalar(select(PaymeTransaction).where(PaymeTransaction.payme_id == payme_id)
                   .with_for_update())
    if tx is not None:
        # повтор того же запроса
        if tx.state != CREATED:
            raise err_state()
        if _expired(tx):
            _cancel(tx, REASON_TIMEOUT)
            db.commit()
            raise err_state()
        return {"create_time": tx.create_time, "transaction": str(tx.id), "state": tx.state}

    order = _order_by_account(db, params)
    _check_payable(order, params.get("amount"))

    # По одному заказу — одна живая оплата. Брошенную (дольше запаса на оплату)
    # отменяем сами: иначе покупатель, закрывший страницу Payme, не смог бы
    # заплатить заново ещё двенадцать часов. Если Payme потом попробует
    # провести отменённую — получит отказ, и деньги вернутся покупателю
    grace_ms = int(payments.CHECKOUT_GRACE.total_seconds() * 1000)
    for other in db.scalars(select(PaymeTransaction).where(
            PaymeTransaction.order_id == order.id,
            PaymeTransaction.state == CREATED).with_for_update()):
        if now_ms() - other.create_time > grace_ms:
            _cancel(other, REASON_TIMEOUT)
        else:
            raise err_order(*ORDER_PAYING)

    tx = PaymeTransaction(
        payme_id=payme_id, order_id=order.id, order_number=order.number,
        amount=params["amount"], payme_time=params["time"], create_time=now_ms(),
        perform_time=0, cancel_time=0, state=CREATED, reason=None,
    )
    db.add(tx)
    _mark_checkout(order)
    try:
        db.commit()
    except IntegrityError:
        # тот же запрос пришёл дважды одновременно — транзакцию создал первый
        db.rollback()
        tx = _tx(db, params)
    log.info("Payme: заказ %s, транзакция %s создана", order.number, payme_id)
    return {"create_time": tx.create_time, "transaction": str(tx.id), "state": tx.state}


def perform(db: Session, params: dict, background: BackgroundTasks) -> dict:
    tx = _tx(db, params, lock=True)
    if tx.state == PERFORMED:
        return {"transaction": str(tx.id), "perform_time": tx.perform_time, "state": tx.state}
    if tx.state != CREATED:
        raise err_state()
    if _expired(tx):
        _cancel(tx, REASON_TIMEOUT)
        db.commit()
        raise err_state()

    order = db.get(Order, tx.order_id) if tx.order_id else None
    if order is None or not payments.mark_paid(
            db, order, f"payme:{tx.payme_id}", tx.payme_id, tx.amount // payments.MINOR):
        # Заказ отменили (автоотмена, оператор) или удалили, пока шла оплата.
        # Провести нельзя: отказываем, и Payme вернёт деньги покупателю сам
        log.warning("Payme: транзакция %s не проведена — заказ %s уже %s",
                    tx.payme_id, tx.order_number, order.status if order else "удалён")
        _cancel(tx, REASON_TIMEOUT)
        db.commit()
        raise err_state()

    tx.state = PERFORMED
    tx.perform_time = now_ms()
    db.commit()
    log.info("Заказ %s оплачен через Payme, транзакция %s", order.number, tx.payme_id)

    # дальше — как после оплаты в Telegram: сотрудникам и в REGOS
    from app.shop_bot import send_onward
    send_onward(db, background, order)
    return {"transaction": str(tx.id), "perform_time": tx.perform_time, "state": tx.state}


def cancel(db: Session, params: dict, background: BackgroundTasks) -> dict:
    tx = _tx(db, params, lock=True)
    reason = params.get("reason")
    if tx.state in (CANCELED, CANCELED_AFTER):
        return {"transaction": str(tx.id), "cancel_time": tx.cancel_time, "state": tx.state}

    was_paid = tx.state == PERFORMED
    _cancel(tx, reason if isinstance(reason, int) else None)
    order = db.get(Order, tx.order_id) if tx.order_id else None
    if not was_paid:
        # оплату бросили до списания: заказ по-прежнему ждёт оплаты,
        # покупатель может заплатить заново, а не успеет — отменит автоотмена
        db.commit()
        return {"transaction": str(tx.id), "cancel_time": tx.cancel_time, "state": tx.state}

    # Возврат уже проведённой оплаты — его делают из кабинета Payme. Деньги
    # покупателю вернутся, значит заказ выдавать нельзя: отменяем его и у нас,
    # и в REGOS, и говорим сотрудникам
    canceled = False
    if order is not None and order.status not in ("CANCELED", "DONE"):
        canceled = order_status.move(db, order, "CANCELED")
    db.commit()
    log.warning("Payme: возврат по заказу %s, транзакция %s", tx.order_number, tx.payme_id)
    if order is not None:
        def text(lang):
            return (notify.st(lang, "refund", number=notify.esc(order.number),
                              total=notify.money(tx.amount // payments.MINOR, lang)) + "\n"
                    + (notify.st(lang, "refund_canceled") if canceled else notify.st(
                        lang, "refund_status",
                        status=notify.esc(order_status.text(order.status, lang)))))
        rows = notify.queue_staff(db, order, text)
        if rows:
            db.commit()
            background.add_task(notify.send_many, [row.id for row in rows])
        if canceled and order.regos_document_id:
            from app.regos.orders_push import cancel_one
            background.add_task(cancel_one, order.id)
    return {"transaction": str(tx.id), "cancel_time": tx.cancel_time, "state": tx.state}


def check(db: Session, params: dict) -> dict:
    tx = _tx(db, params)
    return {"create_time": tx.create_time, "perform_time": tx.perform_time,
            "cancel_time": tx.cancel_time, "transaction": str(tx.id),
            "state": tx.state, "reason": tx.reason}


def statement(db: Session, params: dict) -> dict:
    start, end = params.get("from"), params.get("to")
    if not isinstance(start, int) or not isinstance(end, int):
        raise err_request()
    rows = db.scalars(select(PaymeTransaction).where(
        PaymeTransaction.payme_time >= start, PaymeTransaction.payme_time <= end,
    ).order_by(PaymeTransaction.payme_time)).all()
    return {"transactions": [{
        "id": tx.payme_id, "time": tx.payme_time, "amount": tx.amount,
        "account": {ACCOUNT_FIELD: tx.order_number},
        "create_time": tx.create_time, "perform_time": tx.perform_time,
        "cancel_time": tx.cancel_time, "transaction": str(tx.id),
        "state": tx.state, "reason": tx.reason, "receivers": None,
    } for tx in rows]}


def set_fiscal(db: Session, params: dict, background: BackgroundTasks | None = None) -> dict:
    """Фискальный чек от Payme: сохраняем как есть и QR-ссылку отдельно."""
    fiscal = params.get("fiscal_data")
    payme_id = params.get("id")
    if not isinstance(payme_id, str) or not isinstance(fiscal, dict):
        raise PaymeError(-32602, "Неверные параметры", "Noto'g'ri parametrlar", "Invalid params")
    tx = db.scalar(select(PaymeTransaction).where(PaymeTransaction.payme_id == payme_id))
    if tx is None:
        raise PaymeError(-32001, "Чек не найден", "Chek topilmadi", "Receipt not found")
    kind = str(params.get("type") or "")
    stored = json.loads(tx.fiscal_data) if tx.fiscal_data else {}
    stored[kind or "PERFORM"] = fiscal
    tx.fiscal_data = json.dumps(stored, ensure_ascii=False)
    if kind != "CANCEL" and fiscal.get("qr_code_url"):
        tx.fiscal_qr_url = str(fiscal["qr_code_url"])
    db.commit()
    log.info("Payme: фискальный чек %s по заказу %s, статус %s",
             kind, tx.order_number, fiscal.get("status_code"))
    if kind != "CANCEL" and tx.fiscal_qr_url and tx.state == PERFORMED and tx.order_id:
        from app.regos.orders_push import set_fiscal_qr
        set_fiscal_qr(db, db.get(Order, tx.order_id), tx.fiscal_qr_url, background)
    return {"success": True}


METHODS = {
    "CheckPerformTransaction": lambda db, p, bg: check_perform(db, p),
    "CreateTransaction": lambda db, p, bg: create(db, p),
    "PerformTransaction": perform,
    "CancelTransaction": cancel,
    "CheckTransaction": lambda db, p, bg: check(db, p),
    "GetStatement": lambda db, p, bg: statement(db, p),
    "SetFiscalData": lambda db, p, bg: set_fiscal(db, p, bg),
}


# ------------------------------------------------------------ вход

def authorized(header: str | None) -> bool:
    """Basic с логином «Paycom» и ключом кассы. Сравнение постоянного времени."""
    payments.refresh()
    if not header or not header.startswith("Basic ") or not payments.PAYME_KEY:
        return False
    try:
        login, _, key = base64.b64decode(header[6:].strip()).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return login == "Paycom" and hmac.compare_digest(key.encode(), payments.PAYME_KEY.encode())


def answer(request_id, result=None, error: PaymeError | None = None) -> JSONResponse:
    body = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = {"code": error.code, "message": error.message, "data": error.data}
    else:
        body["result"] = result
    return JSONResponse(body)


def process(db: Session, background: BackgroundTasks, method: str, params: dict):
    """Синхронный разбор: внутри запросы к базе — не держим ими цикл событий."""
    try:
        return METHODS[method](db, params, background), None
    except PaymeError as exc:
        db.rollback()
        return None, exc


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def payme_endpoint(request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_db)):
    request_id = None
    try:
        data = await request.json()
    except ValueError:
        return answer(None, error=PaymeError(-32700, "Ошибка разбора JSON", "JSON xatosi",
                                             "Parse error"))
    if isinstance(data, dict):
        request_id = data.get("id")

    if not payments.provider_enabled("payme"):
        return answer(request_id, error=err_auth())
    if not allowed_ip(request.client.host if request.client else None):
        log.warning("Payme: запрос с чужого адреса %s", request.client and request.client.host)
        return answer(request_id, error=err_auth())
    if not authorized(request.headers.get("Authorization")):
        return answer(request_id, error=err_auth())
    if not isinstance(data, dict) or not isinstance(data.get("params"), dict):
        return answer(request_id, error=err_request())
    method = data.get("method")
    if method not in METHODS:
        return answer(request_id, error=err_method())

    try:
        result, error = await run_in_threadpool(process, db, background, method, data["params"])
    except Exception:                       # noqa: BLE001
        # Системная ошибка: Payme повторит запрос. Провести платёж дважды
        # повтор не может — транзакция ищется по id Payme
        db.rollback()
        log.exception("Payme: сбой в %s", method)
        return answer(request_id, error=PaymeError(-32400, "Системная ошибка",
                                                   "Tizim xatosi", "System error"))
    return answer(request_id, result=result, error=error)
