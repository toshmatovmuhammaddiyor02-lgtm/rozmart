"""Оплата через Click (SHOP API).

Покупатель нажимает «Оплатить», приложение открывает страницу Click
(`pay_url`, my.click.uz): там он платит в приложении Click или любой картой.
Остальное Click делает сам, обращаясь к нашему серверу в два шага:

1. Prepare (/click/prepare, action=0) — можно ли оплатить заказ на эту сумму.
   Заказ бронируем: пока идёт оплата, автоотмена его не трогает;
2. Complete (/click/complete, action=1) — чем кончилось списание. error=0 —
   деньги списаны: заказ оплачен и идёт дальше тем же путём, что после
   любой оплаты, — сотрудникам и в REGOS. error<0 — оплата не прошла:
   подготовленный платёж отменяем, заказ по-прежнему ждёт оплаты.

Запрос — form-urlencoded, подписан: sign_string = md5 от полей запроса
и секретного ключа (`sign`). Ответ — JSON, всегда HTTP 200, ошибка — в error.

Click повторяет запросы, если не дождался ответа. Prepare с тем же
click_trans_id отвечает тем же merchant_prepare_id; повторный Complete
проведённого платежа — «уже оплачен» (-4): так разрешает документация.

Если деньги списаны, а заказ их уже не ждёт (отменили, оплатили иначе),
Complete по документации Click обязан ответить «успешно», а мы — вернуть
платёж методом reversal Merchant API. Не вышло — говорим сотрудникам.

Чек в налоговую (фискализация) — тоже на нас: после оплаты отправляем
состав чека (ИКПУ, код упаковки, НДС) методом ofd_data/submit_items. Не
принят — пробуем ещё несколько раз в фоне (`retry_fiscal`). Принятый чек
налоговая регистрирует не сразу: ссылку на него (QR) забираем у Click, пока
не появится, и передаём в заказ REGOS — касса второй чек не печатает.
"""
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import notify, order_status, payments
from app.db import get_db
from app.models import ClickTransaction, Order, OrderItem, utcnow

log = logging.getLogger(__name__)

router = APIRouter(tags=["Click"])

PREPARE_PATH = "/click/prepare"
COMPLETE_PATH = "/click/complete"
PAY_PAGE = "https://my.click.uz/services/pay"
API = "https://api.click.uz/v2/merchant"
# куда Click вернёт покупателя после оплаты — обычно ссылка на бота магазина
RETURN_URL = (os.getenv("CLICK_RETURN_URL") or os.getenv("PAYME_RETURN_URL") or "").strip()
ORDER_PREFIX = "RB-"

PREPARED, COMPLETED, CANCELED, REVERSED, REVERSE_FAILED = 0, 1, -1, -2, -3

# НДС, если в REGOS его нет. 12% — ставка в Узбекистане
DEFAULT_VAT = 12
# Количество в чеке — в тысячных долях, как в протоколе ОФД: 1 шт = 1000
AMOUNT_SCALE = 1000
# Сколько раз пробуем отправить чек и с каким шагом
FISCAL_ATTEMPTS = 5
FISCAL_STEP = timedelta(minutes=10)
# Сколько ждём ссылку на чек после оплаты: кассир берёт заказ через минуты,
# и позже ссылка ему уже не поможет — чек напечатает касса
QR_WAIT = timedelta(minutes=30)

# коды ошибок — из документации Click (SHOP API — Ошибки)
NOTES = {
    0: "Success",
    -1: "SIGN CHECK FAILED!",
    -2: "Incorrect parameter amount",
    -3: "Action not found",
    -4: "Already paid",
    -5: "User does not exist",
    -6: "Transaction does not exist",
    -7: "Failed to update user",
    -8: "Error in request from click",
    -9: "Transaction cancelled",
}


class ClickError(Exception):
    def __init__(self, code: int):
        super().__init__(NOTES[code])
        self.code = code


# ------------------------------------------------------------ ссылка на оплату

def pay_url(order: Order) -> str:
    """Страница оплаты заказа в Click. Сумма — в сумах, «N.NN»."""
    params = {
        "service_id": payments.CLICK_SERVICE_ID,
        "merchant_id": payments.CLICK_MERCHANT_ID,
        "amount": f"{order.total:.2f}",
        "transaction_param": order.number,
    }
    if RETURN_URL:
        params["return_url"] = RETURN_URL
    return f"{PAY_PAGE}?{urlencode(params)}"


# ------------------------------------------------------------ подпись

def sign(form: dict, with_prepare_id: bool) -> str:
    """md5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id
    [+ merchant_prepare_id] + amount + action + sign_time) — строки как
    пришли: сумму «1000.00» нельзя превращать в «1000», подпись не сойдётся."""
    parts = [form.get("click_trans_id", ""), form.get("service_id", ""),
             payments.CLICK_SECRET_KEY, form.get("merchant_trans_id", "")]
    if with_prepare_id:
        parts.append(form.get("merchant_prepare_id", ""))
    parts += [form.get("amount", ""), form.get("action", ""), form.get("sign_time", "")]
    return hashlib.md5("".join(parts).encode()).hexdigest()


def signed(form: dict, with_prepare_id: bool) -> bool:
    payments.refresh()
    if not payments.CLICK_SECRET_KEY:
        return False
    got = str(form.get("sign_string", "")).strip().lower()
    return hmac.compare_digest(got.encode(), sign(form, with_prepare_id).encode())


# ------------------------------------------------------------ разбор запроса

def number_of(value) -> int:
    text = str(value or "").strip()
    if not text.lstrip("-").isdigit():
        raise ClickError(-8)
    return int(text)


def amount_of(value) -> Decimal:
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ClickError(-2)
    if not amount.is_finite() or amount <= 0:
        raise ClickError(-2)
    return amount


def order_number(form: dict) -> str:
    number = str(form.get("merchant_trans_id") or "").strip().upper()
    if number.isdigit():
        number = ORDER_PREFIX + number
    return number


def check_request(form: dict, action: int, with_prepare_id: bool) -> None:
    required = ["click_trans_id", "service_id", "merchant_trans_id", "amount", "action",
                "sign_time", "sign_string"] + (["merchant_prepare_id"] if with_prepare_id else [])
    if any(str(form.get(name, "")).strip() == "" for name in required):
        raise ClickError(-8)
    if not signed(form, with_prepare_id):
        log.warning("Click: неверная подпись, транзакция %s", form.get("click_trans_id"))
        raise ClickError(-1)
    if str(form.get("service_id")).strip() != payments.CLICK_SERVICE_ID:
        raise ClickError(-8)
    if str(form.get("action")).strip() != str(action):
        raise ClickError(-3)


def find_order(db: Session, number: str) -> Order:
    order = db.scalar(select(Order).where(Order.number == number).options(
        selectinload(Order.items).selectinload(OrderItem.variant)))
    if order is None:
        raise ClickError(-5)
    return order


def check_payable(order: Order, amount: Decimal) -> None:
    if order.paid_at is not None:
        raise ClickError(-4)
    if order.status == "CANCELED":
        raise ClickError(-9)
    if order.payment_method != "online" or order.status != "NEW":
        raise ClickError(-5)
    if amount != order.total:
        log.warning("Click: заказ %s, сумма %s, а в заказе %s", order.number, amount, order.total)
        raise ClickError(-2)


# ------------------------------------------------------------ шаги

def prepare(db: Session, form: dict) -> dict:
    check_request(form, 0, with_prepare_id=False)
    click_id = number_of(form["click_trans_id"])
    amount = amount_of(form["amount"])

    tx = db.scalar(select(ClickTransaction).where(ClickTransaction.click_trans_id == click_id))
    if tx is not None:
        # повтор того же Prepare
        if tx.state == COMPLETED:
            raise ClickError(-4)
        if tx.state != PREPARED:
            raise ClickError(-9)
        return answer_prepare(form, tx)

    order = find_order(db, order_number(form))
    check_payable(order, amount)
    if number_of(form.get("error") or 0) < 0:
        raise ClickError(-9)

    tx = ClickTransaction(
        click_trans_id=click_id, click_paydoc_id=number_of(form.get("click_paydoc_id") or 0) or None,
        order_id=order.id, order_number=order.number, amount=order.total, state=PREPARED,
        created_at=utcnow(),
    )
    db.add(tx)
    # покупатель платит: пока идёт оплата, автоотмена заказ не трогает
    if order.checkout_at is None or order.checkout_at < utcnow():
        order.checkout_at = utcnow()
    try:
        db.commit()
    except IntegrityError:
        # тот же Prepare пришёл дважды одновременно — записал первый
        db.rollback()
        tx = db.scalar(select(ClickTransaction).where(ClickTransaction.click_trans_id == click_id))
    log.info("Click: заказ %s, платёж %s подготовлен", order.number, click_id)
    return answer_prepare(form, tx)


def answer_prepare(form: dict, tx: ClickTransaction) -> dict:
    return {"click_trans_id": tx.click_trans_id, "merchant_trans_id": form.get("merchant_trans_id"),
            "merchant_prepare_id": tx.id, "error": 0, "error_note": NOTES[0]}


def complete(db: Session, form: dict, background: BackgroundTasks) -> dict:
    check_request(form, 1, with_prepare_id=True)
    click_id = number_of(form["click_trans_id"])
    prepare_id = number_of(form["merchant_prepare_id"])
    amount = amount_of(form["amount"])

    tx = db.scalar(select(ClickTransaction).where(ClickTransaction.id == prepare_id)
                   .with_for_update())
    if tx is None or tx.click_trans_id != click_id or tx.order_number != order_number(form):
        raise ClickError(-6)
    if tx.state in (COMPLETED, REVERSED, REVERSE_FAILED):
        raise ClickError(-4)
    if tx.state == CANCELED:
        raise ClickError(-9)
    if amount != tx.amount:
        raise ClickError(-2)
    paydoc = number_of(form.get("click_paydoc_id") or 0)
    if paydoc:
        tx.click_paydoc_id = paydoc

    if number_of(form.get("error") or 0) < 0:
        # оплата у Click не прошла: платёж отменяем, заказ ждёт оплаты дальше —
        # покупатель может заплатить заново
        tx.state, tx.canceled_at = CANCELED, utcnow()
        db.commit()
        log.info("Click: платёж %s по заказу %s не прошёл: %s %s", click_id, tx.order_number,
                 form.get("error"), form.get("error_note"))
        raise ClickError(-9)

    order = db.get(Order, tx.order_id) if tx.order_id else None
    if order is not None and payments.mark_paid(db, order, f"click:{click_id}",
                                                str(tx.click_paydoc_id or click_id), tx.amount):
        tx.state, tx.completed_at = COMPLETED, utcnow()
        db.commit()
        log.info("Заказ %s оплачен через Click, платёж %s", order.number, click_id)
        from app.shop_bot import send_onward
        send_onward(db, background, order)
        background.add_task(fiscalize, tx.id)
        return answer_complete(form, tx)

    # Деньги списаны, а заказ их уже не ждал. По документации Click отвечаем
    # «успешно» и сами возвращаем платёж
    tx.state, tx.completed_at = REVERSED, utcnow()
    db.commit()
    log.error("Click: платёж %s пришёл, а заказ %s уже %s — возвращаю", click_id,
              tx.order_number, order.status if order else "удалён")
    background.add_task(reverse, tx.id)
    return answer_complete(form, tx)


def answer_complete(form: dict, tx: ClickTransaction) -> dict:
    return {"click_trans_id": tx.click_trans_id, "merchant_trans_id": form.get("merchant_trans_id"),
            "merchant_confirm_id": tx.id, "error": 0, "error_note": NOTES[0]}


# ------------------------------------------------------------ Merchant API

def auth_header() -> str:
    """Auth: merchant_user_id:sha1(timestamp + secret_key):timestamp"""
    stamp = str(int(time.time()))
    digest = hashlib.sha1((stamp + payments.CLICK_SECRET_KEY).encode()).hexdigest()
    return f"{payments.CLICK_MERCHANT_USER_ID}:{digest}:{stamp}"


def api(method: str, path: str, body: dict | None = None) -> dict:
    """Запрос к Merchant API Click. Ответ — JSON; сеть и HTTP-ошибки — исключение."""
    request = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Accept": "application/json", "Content-Type": "application/json",
                 "Auth": auth_header()})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")[:500]
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"HTTP {exc.code}: {text}") from exc


def receipt_items(order: Order) -> list[dict]:
    """Строки чека для ofd_data/submit_items. Суммы — в тийинах.

    ИКПУ, код упаковки и НДС — из фасовки (приходят из REGOS). Без кода
    упаковки налоговая чек может не принять — пишем в журнал."""
    tin = {"TIN": payments.CLICK_TIN}
    rows = []
    for item in order.items:
        variant = item.variant
        vat = (variant.vat_percent if variant and variant.vat_percent is not None
               else DEFAULT_VAT)
        total = item.price * item.quantity * payments.MINOR
        row = {
            "Name": f"{item.product_name} {item.weight}".strip()[:63],
            "SPIC": (variant.mxik if variant else None) or "",
            "PackageCode": (variant.package_code if variant else None) or "",
            "GoodPrice": item.price * payments.MINOR,
            "Price": total,
            "Amount": item.quantity * AMOUNT_SCALE,
            "VAT": round(total * vat / (100 + vat)),
            "VATPercent": vat,
            "CommissionInfo": tin,
        }
        if not row["SPIC"] or not row["PackageCode"]:
            log.warning("Чек Click %s: у «%s» нет ИКПУ или кода упаковки",
                        order.number, item.product_name)
        rows.append(row)
    if order.delivery_price:
        total = order.delivery_price * payments.MINOR
        rows.append({
            "Name": "Доставка", "SPIC": payments.CLICK_DELIVERY_SPIC,
            "PackageCode": payments.CLICK_DELIVERY_PACKAGE,
            "GoodPrice": total, "Price": total, "Amount": AMOUNT_SCALE,
            "VAT": round(total * DEFAULT_VAT / (100 + DEFAULT_VAT)), "VATPercent": DEFAULT_VAT,
            "CommissionInfo": tin,
        })
    return rows


def fiscalize(tx_id: int) -> None:
    """Отправляет чек оплаченного заказа в налоговую через Click."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        tx = db.get(ClickTransaction, tx_id)
        if tx is None or tx.state != COMPLETED or tx.fiscal_status == "ok":
            return
        payments.refresh()
        tx.fiscal_attempts = (tx.fiscal_attempts or 0) + 1
        tx.fiscal_at = utcnow()
        if not (payments.CLICK_MERCHANT_USER_ID and payments.CLICK_TIN and tx.click_paydoc_id):
            tx.fiscal_status, tx.fiscal_note = "error", "нет merchant_user_id, ИНН или номера платежа"
            db.commit()
            log.error("Чек Click по заказу %s не отправлен: %s", tx.order_number, tx.fiscal_note)
            return
        order = db.scalar(select(Order).where(Order.id == tx.order_id).options(
            selectinload(Order.items).selectinload(OrderItem.variant)))
        if order is None:
            tx.fiscal_status, tx.fiscal_note = "error", "заказ удалён"
            db.commit()
            return
        try:
            result = api("POST", "/payment/ofd_data/submit_items", {
                "service_id": int(payments.CLICK_SERVICE_ID),
                "payment_id": tx.click_paydoc_id,
                "items": receipt_items(order),
                "received_ecash": 0,
                "received_cash": 0,
                "received_card": tx.amount * payments.MINOR,
            })
        except Exception as exc:                # noqa: BLE001
            result = {"error_code": None, "error_note": str(exc)[:300]}
        ok = result.get("error_code") == 0
        tx.fiscal_status = "ok" if ok else "error"
        tx.fiscal_note = json.dumps(result, ensure_ascii=False)[:1000]
        db.commit()
        (log.info if ok else log.error)("Чек Click по заказу %s: %s", tx.order_number,
                                        tx.fiscal_note)
        if ok:
            fetch_qr(db, tx, order)
    except Exception:                           # noqa: BLE001
        db.rollback()
        log.exception("Чек Click: сбой")
    finally:
        db.close()


def fetch_qr(db: Session, tx: ClickTransaction, order: Order) -> None:
    """Забирает у Click ссылку на зарегистрированный чек и передаёт её
    в заказ REGOS. Нет ещё — не беда, `retry_fiscal` спросит снова."""
    try:
        result = api("GET", f"/payment/ofd_data/{payments.CLICK_SERVICE_ID}/{tx.click_paydoc_id}")
    except Exception as exc:                    # noqa: BLE001
        log.info("Чек Click по заказу %s: ссылки пока нет (%s)", tx.order_number, exc)
        return
    url = str(result.get("qrCodeURL") or "").strip()
    if not url:
        return
    log.info("Чек Click по заказу %s зарегистрирован: %s", tx.order_number, url)
    from app.regos.orders_push import set_fiscal_qr
    set_fiscal_qr(db, order, url)


def retry_fiscal(db: Session) -> None:
    """Повторяет неотправленные чеки — не чаще FISCAL_STEP, не больше FISCAL_ATTEMPTS."""
    if "click" not in payments.PROVIDERS:
        return
    now = utcnow()
    ids = db.scalars(select(ClickTransaction.id).where(
        ClickTransaction.state == COMPLETED,
        ClickTransaction.fiscal_status == "error",
        ClickTransaction.fiscal_attempts < FISCAL_ATTEMPTS,
        ClickTransaction.completed_at > now - timedelta(days=3),
    )).all()
    for tx_id in ids:
        tx = db.get(ClickTransaction, tx_id)
        if tx.fiscal_at is None or tx.fiscal_at < now - FISCAL_STEP * tx.fiscal_attempts:
            fiscalize(tx_id)

    # чек принят, а ссылки на него ещё нет — спрашиваем, пока ждать есть смысл
    waiting = db.execute(select(ClickTransaction, Order).join(
        Order, Order.id == ClickTransaction.order_id).where(
        ClickTransaction.state == COMPLETED,
        ClickTransaction.fiscal_status == "ok",
        ClickTransaction.completed_at > now - QR_WAIT,
        Order.fiscal_qr_url.is_(None),
    )).all()
    for tx, order in waiting:
        fetch_qr(db, tx, order)


def reverse(tx_id: int) -> None:
    """Возвращает платёж, пришедший за заказ, который его уже не ждал."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        tx = db.get(ClickTransaction, tx_id)
        if tx is None or tx.state != REVERSED:
            return
        payments.refresh()
        try:
            result = api("DELETE", f"/payment/reversal/{payments.CLICK_SERVICE_ID}/"
                                   f"{tx.click_paydoc_id}")
        except Exception as exc:                # noqa: BLE001
            result = {"error_code": None, "error_note": str(exc)[:300]}
        ok = result.get("error_code") == 0
        if not ok:
            tx.state = REVERSE_FAILED
        tx.canceled_at = utcnow()
        db.commit()
        (log.warning if ok else log.error)("Click: возврат платежа %s по заказу %s: %s",
                                           tx.click_trans_id, tx.order_number, result)
        order = db.get(Order, tx.order_id) if tx.order_id else None

        def text(lang):
            key = "click_late_back" if ok else "click_late_manual"
            return notify.st(lang, key, number=notify.esc(tx.order_number),
                             total=notify.money(tx.amount, lang),
                             state=notify.esc(order_status.text(order.status, lang)
                                              if order else "—"),
                             payment=tx.click_paydoc_id or tx.click_trans_id)
        rows = notify.queue_staff(db, order, text)
        if rows:
            db.commit()
            notify.send_many([row.id for row in rows])
    except Exception:                           # noqa: BLE001
        db.rollback()
        log.exception("Click: сбой возврата")
    finally:
        db.close()


# ------------------------------------------------------------ вход

def reply(form: dict, error: ClickError) -> dict:
    return {"click_trans_id": form.get("click_trans_id"),
            "merchant_trans_id": form.get("merchant_trans_id"),
            "error": error.code, "error_note": NOTES[error.code]}


def process(db: Session, background: BackgroundTasks, form: dict, action: int) -> dict:
    try:
        return prepare(db, form) if action == 0 else complete(db, form, background)
    except ClickError as exc:
        db.rollback()
        return reply(form, exc)


async def read_form(request: Request) -> dict:
    try:
        if "json" in request.headers.get("content-type", ""):
            data = await request.json()
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        return {k: str(v) for k, v in (await request.form()).items()}
    except Exception:                           # noqa: BLE001
        return {}


async def endpoint(request: Request, background: BackgroundTasks, db: Session, action: int):
    form = await read_form(request)
    if not payments.provider_enabled("click"):
        return JSONResponse(reply(form, ClickError(-8)))
    try:
        result = await run_in_threadpool(process, db, background, form, action)
    except Exception:                           # noqa: BLE001
        # Системная ошибка: Click повторит запрос. Провести платёж дважды
        # повтор не может — платёж ищется по click_trans_id
        db.rollback()
        log.exception("Click: сбой в %s", "Prepare" if action == 0 else "Complete")
        result = reply(form, ClickError(-7))
    return JSONResponse(result)


@router.post(PREPARE_PATH, include_in_schema=False)
async def click_prepare(request: Request, background: BackgroundTasks,
                        db: Session = Depends(get_db)):
    return await endpoint(request, background, db, 0)


@router.post(COMPLETE_PATH, include_in_schema=False)
async def click_complete(request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_db)):
    return await endpoint(request, background, db, 1)
