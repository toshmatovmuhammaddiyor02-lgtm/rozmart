"""Оплата через Paynet («универсальный WEB-сервис поставщика услуг»).

У Paynet нет страницы оплаты, куда мы отправляем покупателя. Покупатель
платит в самом Paynet (приложение, терминал, агент): выбирает наш сервис,
вводит номер заказа — Paynet спрашивает нас, что это за заказ, и проводит
платёж. Наш адрес /paynet отвечает на вызовы по JSON-RPC 2.0:

* GetInformation — что за заказ: сумма и кому (имя покупателя — не целиком);
* PerformTransaction — платёж. Одним вызовом: деньги списаны, заказ
  оплачен и идёт дальше тем же путём, что после любой оплаты — сотрудникам
  и в REGOS;
* CheckTransaction — состояние платежа: 1 проведён, 2 отменён, 3 не найден;
* CancelTransaction — отмена проведённого платежа (возврат): заказ
  отменяем и у нас, и в REGOS, сотрудникам — «возврат»;
* GetStatement — проведённые платежи за период (отменённые не входят);
* ChangePassword — необязательный, не поддерживаем: пароль меняем сами.

Заказ в полях запроса — по номеру: «RB-8047» или просто «8047» (в Paynet
номер удобнее вводить цифрами). Имя поля — PAYNET_FIELD, по умолчанию
order_id; если его нет, берём первое поле — тестер Paynet шлёт client_id.

Доступ — HTTP Basic с логином и паролем, которые задаём мы (PAYNET_LOGIN,
PAYNET_PASSWORD) и передаём Paynet. Без них или с неверными — HTTP 401,
так требует спецификация. Остальные ошибки — HTTP 200 и error в теле.

Время в ответах — «ГГГГ-ММ-ДД чч:мм:сс» по Ташкенту (GMT+5).

Тестер Paynet (страница из их архива) шлёт запросы из браузера, поэтому
адрес отвечает и на предварительный запрос CORS — иначе браузер не покажет
ему ответ. Серверам Paynet это не мешает.
"""
import base64
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notify, order_status, payments
from app.db import get_db
from app.models import Order, PaynetTransaction, utcnow

log = logging.getLogger(__name__)

router = APIRouter(tags=["Paynet"])

WEBHOOK_PATH = "/paynet"
FIELD = os.getenv("PAYNET_FIELD", "order_id").strip() or "order_id"
# Ссылка, по которой приложение откроет оплату в Paynet, если Paynet её даст, —
# payments.PAYNET_PAY_URL (задаётся в панели). Подстановки: {service}, {order},
# {amount} (в сумах). Пусто — покупателю показываем, что ввести в Paynet

# Тестовый счёт для проверок Paynet. Их тестер гоняет одни и те же сценарии
# много раз: оплата, проверка, отмена, выписка. На настоящем заказе каждый
# прогон стоил бы нового заказа, сообщений сотрудникам и документа на кассе.
# Поэтому есть счёт, который ведёт себя как заказ на TEST_AMOUNT сумов, но ни
# заказов, ни кассы, ни сотрудников не трогает. Работает, только пока Paynet
# в тестовом режиме (PAYNET_TEST): в боевом такого «заказа» нет.
TEST_ACCOUNT = os.getenv("PAYNET_TEST_ACCOUNT", "1000001").strip()
TEST_AMOUNT = 5000
TEST_NUMBER = "TEST-PAYNET"

TASHKENT = timezone(timedelta(hours=5))
SUCCESS, CANCELED, NOT_FOUND = 1, 2, 3
ORDER_PREFIX = "RB-"

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, Accept",
    "Access-Control-Max-Age": "600",
}


class PaynetError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def err(code: int) -> PaynetError:
    return PaynetError(code, MESSAGES.get(code, "Ошибка"))


# тексты — из спецификации Paynet
MESSAGES = {
    -32300: "Метод запроса не POST",
    -32700: "Ошибка парсинга JSON",
    -32600: "Неверный запрос",
    -32601: "Запрашиваемый метод не найден",
    -32602: "Отсутствуют обязательные поля параметров",
    -32603: "Системная ошибка",
    201: "Транзакция уже существует",
    202: "Транзакция уже отменена",
    203: "Транзакция не найдена",
    302: "Клиент не найден",
    305: "Услуга не найдена",
    411: "Не заданы один или несколько обязательных параметров",
    412: "Неверный логин или пароль",
    413: "Неверная сумма",
    414: "Неверный формат даты и времени",
    501: "Транзакции запрещены для данного плательщика",
}


# ------------------------------------------------------------ время

def stamp(moment: datetime | None = None) -> str:
    """Время для Paynet: по Ташкенту. В базе — UTC без пояса."""
    moment = moment or utcnow()
    return moment.replace(tzinfo=timezone.utc).astimezone(TASHKENT).strftime("%Y-%m-%d %H:%M:%S")


def parse_stamp(value) -> datetime:
    """«2026-10-06 10:49:00» по Ташкенту -> UTC без пояса. Терпим и «T»
    вместо пробела, и время без секунд."""
    text = str(value or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(text, fmt).replace(tzinfo=TASHKENT)
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    raise err(414)


# ------------------------------------------------------------ заказ

def order_number(params: dict) -> str:
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise err(411)
    # ключи бывают с пробелами — в примерах Paynet " fields ", " client_id "
    clean = {str(k).strip(): v for k, v in fields.items()}
    value = clean.get(FIELD, next(iter(clean.values())))
    number = str(value or "").strip().upper()
    if number.isdigit():
        number = ORDER_PREFIX + number
    return number


def is_test_account(number: str) -> bool:
    return bool(payments.PAYNET_TEST and TEST_ACCOUNT
                and number in (ORDER_PREFIX + TEST_ACCOUNT, TEST_ACCOUNT.upper()))


def find_order(db: Session, number: str) -> Order:
    order = db.scalar(select(Order).where(Order.number == number))
    if order is None:
        raise err(302)
    return order


def payable(order: Order) -> None:
    """Заказ ждёт оплаты в Paynet. Иначе — «клиент не найден»: для Paynet
    заказ, который нельзя оплатить, всё равно что отсутствующий."""
    if order.payment_method != "online" or order.status != "NEW" or order.paid_at is not None:
        raise err(302)


def masked_name(name: str) -> str:
    """Имя покупателя — Paynet показывает его плательщику. Номер заказа могут
    ввести наугад, поэтому целиком не отдаём: «Javohir R.»"""
    parts = (name or "").split()
    if not parts:
        return ""
    return parts[0] + (f" {parts[1][0]}." if len(parts) > 1 and parts[1] else "")


def check_service(params: dict) -> None:
    try:
        service = int(params.get("serviceId"))
    except (TypeError, ValueError):
        raise err(411)
    # пока номера нет, принимаем тестовый номер Paynet — см. paynet_endpoint_ready
    if payments.PAYNET_SERVICE_ID and str(service) != payments.PAYNET_SERVICE_ID:
        raise err(305)


def tx_id(params: dict) -> str:
    value = params.get("transactionId")
    if value in (None, "") or not str(value).strip().isdigit():
        raise err(411)
    return str(value).strip()


# ------------------------------------------------------------ методы

def get_information(db: Session, params: dict) -> dict:
    check_service(params)
    number = order_number(params)
    if is_test_account(number):
        return {"status": 0, "timestamp": stamp(), "fields": {
            FIELD: TEST_ACCOUNT, "name": "Test P.", "amount": TEST_AMOUNT * payments.MINOR,
            "description": "ROZMART, тестовый счёт Paynet"}}
    order = find_order(db, number)
    payable(order)
    # покупатель в Paynet смотрит на этот заказ: пока он платит, автоотмена
    # его не трогает
    if order.checkout_at is None or order.checkout_at < utcnow():
        order.checkout_at = utcnow()
        db.commit()
    return {
        "status": 0,
        "timestamp": stamp(),
        "fields": {
            FIELD: order.number,
            "name": masked_name(order.customer_name),
            "amount": order.total * payments.MINOR,
            "description": f"ROZMART, заказ {order.number}",
        },
    }


def perform(db: Session, params: dict, background: BackgroundTasks) -> dict:
    check_service(params)
    paynet_id = tx_id(params)
    amount = params.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        raise err(411)

    if db.scalar(select(PaynetTransaction).where(PaynetTransaction.paynet_id == paynet_id)):
        raise err(201)

    number = order_number(params)
    if is_test_account(number):
        if int(amount) != amount or int(amount) != TEST_AMOUNT * payments.MINOR:
            raise err(413)
        tx = PaynetTransaction(paynet_id=paynet_id, order_id=None, order_number=TEST_NUMBER,
                               amount=int(amount), state=SUCCESS, created_at=utcnow())
        db.add(tx)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise err(201)
        log.info("Paynet: тестовый платёж %s", paynet_id)
        return {"providerTrnId": tx.id, "timestamp": stamp(tx.created_at),
                "fields": {FIELD: TEST_ACCOUNT}}

    order = find_order(db, number)
    payable(order)
    if int(amount) != amount or int(amount) != order.total * payments.MINOR:
        log.warning("Paynet: заказ %s, сумма %s, а в заказе %s",
                    order.number, amount, order.total * payments.MINOR)
        raise err(413)

    tx = PaynetTransaction(paynet_id=paynet_id, order_id=order.id, order_number=order.number,
                           amount=int(amount), state=SUCCESS, created_at=utcnow())
    db.add(tx)
    try:
        db.flush()
    except IntegrityError:
        # тот же номер пришёл дважды одновременно — провёл первый
        db.rollback()
        raise err(201)
    # условная запись: заказ всё ещё ждёт оплаты. Не дождался (отменили,
    # оплатили иначе) — откатываем всё, Paynet деньги не спишет
    if not payments.mark_paid(db, order, f"paynet:{paynet_id}", paynet_id, int(amount) // payments.MINOR):
        db.rollback()
        log.warning("Paynet: платёж %s не проведён — заказ %s уже не ждёт оплаты",
                    paynet_id, order.number)
        raise err(302)
    db.commit()
    log.info("Заказ %s оплачен через Paynet, транзакция %s", order.number, paynet_id)

    from app.shop_bot import send_onward
    send_onward(db, background, order)
    return {"providerTrnId": tx.id, "timestamp": stamp(tx.created_at),
            "fields": {FIELD: order.number}}


def check(db: Session, params: dict) -> dict:
    check_service(params)
    paynet_id = tx_id(params)
    tx = db.scalar(select(PaynetTransaction).where(PaynetTransaction.paynet_id == paynet_id))
    if tx is None:
        return {"transactionState": NOT_FOUND, "timestamp": stamp(), "providerTrnId": 0}
    return {"transactionState": tx.state, "timestamp": stamp(tx.canceled_at or tx.created_at),
            "providerTrnId": tx.id}


def cancel(db: Session, params: dict, background: BackgroundTasks) -> dict:
    """Отмена проведённого платежа — деньги вернутся покупателю. Заказ
    выдавать нельзя: отменяем его у нас и в REGOS, сотрудникам — «возврат»."""
    check_service(params)
    paynet_id = tx_id(params)
    tx = db.scalar(select(PaynetTransaction).where(PaynetTransaction.paynet_id == paynet_id)
                   .with_for_update())
    if tx is None:
        raise err(203)
    if tx.state == CANCELED:
        raise err(202)
    tx.state, tx.canceled_at = CANCELED, utcnow()
    order = db.get(Order, tx.order_id) if tx.order_id else None
    canceled = False
    if order is not None and order.status not in ("CANCELED", "DONE"):
        canceled = order_status.move(db, order, "CANCELED")
    db.commit()
    log.warning("Paynet: отмена платежа %s по заказу %s", paynet_id, tx.order_number)

    if order is not None:
        def text(lang):
            return (notify.st(lang, "refund_paynet", number=notify.esc(order.number),
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
        if canceled:
            from app import courier_bot
            background.add_task(courier_bot.order_canceled, order)
    return {"providerTrnId": tx.id, "timestamp": stamp(tx.canceled_at), "transactionState": CANCELED}


def statement(db: Session, params: dict) -> dict:
    check_service(params)
    start, end = parse_stamp(params.get("dateFrom")), parse_stamp(params.get("dateTo"))
    rows = db.scalars(select(PaynetTransaction).where(
        PaynetTransaction.state == SUCCESS,
        PaynetTransaction.created_at >= start,
        PaynetTransaction.created_at <= end,
    ).order_by(PaynetTransaction.created_at)).all()
    return {"statements": [{
        "amount": tx.amount, "providerTrnId": tx.id,
        "transactionId": int(tx.paynet_id), "timestamp": stamp(tx.created_at),
    } for tx in rows]}


METHODS = {
    "GetInformation": lambda db, p, bg: get_information(db, p),
    "PerformTransaction": perform,
    "CheckTransaction": lambda db, p, bg: check(db, p),
    "CancelTransaction": cancel,
    "GetStatement": lambda db, p, bg: statement(db, p),
}


# ------------------------------------------------------------ вход

def authorized(header: str | None) -> bool:
    payments.refresh()
    if not header or not header.startswith("Basic ") or not payments.PAYNET_PASSWORD:
        return False
    try:
        login, _, password = base64.b64decode(header[6:].strip()).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return (hmac.compare_digest(login.encode(), payments.PAYNET_LOGIN.encode())
            and hmac.compare_digest(password.encode(), payments.PAYNET_PASSWORD.encode()))


def reply(request_id, result=None, error: PaynetError | None = None, status: int = 200):
    body = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        body["error"] = {"code": error.code, "message": error.message}
    else:
        body["result"] = result
    return JSONResponse(body, status_code=status, headers=CORS)


def process(db: Session, background: BackgroundTasks, method: str, params: dict):
    try:
        return METHODS[method](db, params, background), None
    except PaynetError as exc:
        db.rollback()
        return None, exc


@router.options(WEBHOOK_PATH, include_in_schema=False)
async def paynet_preflight():
    return Response(status_code=204, headers=CORS)


@router.get(WEBHOOK_PATH, include_in_schema=False)
async def paynet_not_post():
    return reply(None, error=err(-32300))


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def paynet_endpoint(request: Request, background: BackgroundTasks,
                          db: Session = Depends(get_db)):
    if not payments.paynet_endpoint_ready() \
            or not authorized(request.headers.get("Authorization")):
        # так требует Paynet: без верного логина и пароля — HTTP 401
        return reply(None, error=err(412), status=401)
    try:
        data = await request.json()
    except ValueError:
        return reply(None, error=err(-32700))
    request_id = data.get("id") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("method"), str):
        return reply(request_id, error=err(-32600))
    method = data["method"].strip()     # в примерах Paynet бывает « CancelTransaction»
    if method not in METHODS:
        return reply(request_id, error=err(-32601))
    params = data.get("params")
    if not isinstance(params, dict):
        return reply(request_id, error=err(-32602))
    try:
        result, error = await run_in_threadpool(process, db, background, method, params)
    except Exception:                       # noqa: BLE001
        db.rollback()
        log.exception("Paynet: сбой в %s", method)
        return reply(request_id, error=err(-32603))
    return reply(request_id, result=result, error=error)
