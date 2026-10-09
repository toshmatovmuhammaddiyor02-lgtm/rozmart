"""Оплата картой: общее для всех способов и Telegram Payments.

Включённые способы — PAYMENT_PROVIDERS через запятую, например «payme,paynet»
(старое PAYMENT_PROVIDER — один способ). Покупатель выбирает способ сам при
оформлении, выбор запоминается у заказа (Order.pay_provider); оплатить можно
и другим включённым способом. Двойной оплаты не бывает: запись оплаты
условная (mark_paid), второй платёж по оплаченному заказу не проводится.

Способы:

* `telegram` — Telegram Payments: окно оплаты внутри Telegram, провайдер
  подключается в BotFather. Описан ниже;
* `payme` — Merchant API Payme: покупатель уходит на страницу Payme, а Payme
  сам спрашивает наш сервер по шагам. Только так Payme передаёт фискальный
  чек в налоговую. См. app/payme.py;
* `paynet` — универсальный WEB-сервис Paynet: покупатель платит в самом
  Paynet по номеру заказа. См. app/paynet.py;
* `click` — Click SHOP API: покупатель уходит на страницу my.click.uz, Click
  спрашивает наш сервер (Prepare, Complete), чек в налоговую отправляем мы.
  См. app/click.py.

Общее у них: заказ ждёт оплаты в NEW и в REGOS не уходит, неоплаченный
отменяется через UNPAID_MINUTES, оплата проводится одной условной записью
(`mark_paid`), тестовый режим открыт только тестировщикам.

Telegram Payments. Покупатель платит в окне Telegram, не выходя из приложения.
Путь заказа:

1. Оформление: заказ получает NEW «Ждёт оплаты» и в REGOS не уходит — NEW нет
   в PUSHABLE (app/regos/orders_push.py). Сотрудникам тоже ничего не приходит:
   неоплаченный заказ собирать не надо.
2. Приложение просит счёт (`create_invoice_link`) и открывает его
   `Telegram.WebApp.openInvoice`.
3. Перед списанием Telegram спрашивает сервер, можно ли (`pre_checkout_query`),
   и ждёт ответа не дольше 10 секунд. Соглашаемся, только если заказ есть,
   всё ещё ждёт оплаты и сумма та же до сума.
4. После списания приходит `successful_payment`. Только оно — правда о деньгах:
   статус «paid» в приложении говорит лишь, что окно закрылось. Заказ переходит
   в CONFIRMED и дальше идёт тем же путём, что наличный: в REGOS, сотрудникам.

Повторная доставка уведомления об оплате — обычное дело у Telegram. Второй раз
провести платёж не даёт уникальный `telegram_payment_charge_id` в базе.

Тестовый режим. Токен провайдера вида `…:TEST:…` денег не списывает. Витрина
при этом боевая, с настоящими покупателями, и если показать им оплату картой,
любой «оплатит» заказ тестовой картой, а кассир отдаст товар по пометке
«оплачено». Поэтому, пока токен тестовый, оплату картой видят и могут выбрать
только тестировщики из PAYMENT_TEST_USERS. Проверяет это сервер, а не витрина.
"""
import logging
import os
import threading
import time
from datetime import timedelta

from sqlalchemy import or_, select

from app import notify, order_status
from app.models import Order, utcnow
from app.telegram import BOT_TOKEN

log = logging.getLogger(__name__)

# Включённые способы оплаты в том порядке, в каком их видит покупатель.
KNOWN = ("telegram", "payme", "paynet", "click")
PROVIDERS = [p for p in (os.getenv("PAYMENT_PROVIDERS") or os.getenv("PAYMENT_PROVIDER")
                         or "telegram").replace(" ", "").lower().split(",") if p in KNOWN]
# основной — для заказов, где способ не выбран (оформленных до выбора)
PROVIDER = PROVIDERS[0] if PROVIDERS else "telegram"

# Токен провайдера из BotFather. В репозитории его нет — только на сервере.
TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()

# Payme Merchant API: ID кассы и ключ (тестовый или боевой) из кабинета Payme.
PAYME_MERCHANT_ID = os.getenv("PAYME_MERCHANT_ID", "").strip()
PAYME_KEY = os.getenv("PAYME_KEY", "").strip()
# Тестовый режим по умолчанию: боевой включается только явным PAYME_TEST=0.
# Перепутать в эту сторону безопасно, в обратную — нет
PAYME_TEST = os.getenv("PAYME_TEST", "1").strip() != "0"

# Paynet: логин и пароль, с которыми Paynet ходит к нам (задаём их мы и
# передаём Paynet), и номер нашего сервиса у Paynet. См. app/paynet.py
PAYNET_LOGIN = os.getenv("PAYNET_LOGIN", "").strip()
PAYNET_PASSWORD = os.getenv("PAYNET_PASSWORD", "").strip()
PAYNET_SERVICE_ID = os.getenv("PAYNET_SERVICE_ID", "").strip()
PAYNET_TEST = os.getenv("PAYNET_TEST", "1").strip() != "0"
# Ссылка на оплату сервиса в Paynet, если Paynet её даст: {service}, {order}, {amount}
PAYNET_PAY_URL = os.getenv("PAYNET_PAY_URL", "").strip()

# Click: сервис, поставщик, пользователь для Merchant API и секретный ключ —
# из кабинета merchant.click.uz. Тестовой среды у Click нет, платежи всегда
# настоящие, поэтому вместо тестового режима — пилот: CLICK_PILOT=1 (так по
# умолчанию) — способ видят только тестировщики. См. app/click.py
CLICK_SERVICE_ID = os.getenv("CLICK_SERVICE_ID", "").strip()
CLICK_MERCHANT_ID = os.getenv("CLICK_MERCHANT_ID", "").strip()
CLICK_MERCHANT_USER_ID = os.getenv("CLICK_MERCHANT_USER_ID", "").strip()
CLICK_SECRET_KEY = os.getenv("CLICK_SECRET_KEY", "").strip()
CLICK_PILOT = os.getenv("CLICK_PILOT", "1").strip() != "0"
# Для чека: ИНН продавца (CommissionInfo у Click обязателен) и ИКПУ с кодом
# упаковки доставки — у товаров они свои, из REGOS, а у доставки их нет
CLICK_TIN = os.getenv("CLICK_TIN", "").strip()
CLICK_DELIVERY_SPIC = os.getenv("CLICK_DELIVERY_SPIC", "").strip()
CLICK_DELIVERY_PACKAGE = os.getenv("CLICK_DELIVERY_PACKAGE", "").strip()

# Форма оплаты в REGOS для заказа, оплаченного этим способом: id из
# справочника PaymentType. Пусто — «онлайн» по умолчанию (REGOS_PAYMENT_ONLINE_ID,
# «Пласт. карта»). Своя форма со своим счётом не смешивает онлайн-деньги
# с терминалом магазина и даёт кассиру закрыть чек одной кнопкой
REGOS_PAY_PAYME = os.getenv("REGOS_PAY_PAYME", "").strip()
REGOS_PAY_CLICK = os.getenv("REGOS_PAY_CLICK", "").strip()
REGOS_PAY_PAYNET = os.getenv("REGOS_PAY_PAYNET", "").strip()
REGOS_PAY_TELEGRAM = os.getenv("REGOS_PAY_TELEGRAM", "").strip()

# Комиссия платёжных систем, % — по договорам. Деньги приходят на основной
# счёт уже без неё; отчёт «Онлайн-платежи» считает по ней, сколько должно прийти
FEE_PAYME = os.getenv("FEE_PAYME", "1").strip()
FEE_CLICK = os.getenv("FEE_CLICK", "2").strip()
FEE_PAYNET = os.getenv("FEE_PAYNET", "1").strip()

# Секрет вебхука магазинного бота. Без него вебхук отвергает всё, и оплата
# гарантированно не пройдёт: запрос перед списанием уйдёт в тайм-аут.
# Поэтому без секрета оплата картой считается выключенной.
SHOP_SECRET = os.getenv("SHOP_WEBHOOK_SECRET", "").strip()

# Кто может платить картой, пока токен тестовый. Telegram-id через запятую.
TEST_USERS = {
    int(x) for x in os.getenv("PAYMENT_TEST_USERS", "").replace(" ", "").split(",") if x
}

# Пилот на боевых деньгах: способ уже настоящий (боевой ключ), но видят
# его пока только PAYMENT_TEST_USERS — проверить живую оплату до того, как
# её увидят все покупатели. PAYMENT_TESTERS_ONLY=1
TESTERS_ONLY = os.getenv("PAYMENT_TESTERS_ONLY", "0").strip() == "1"

# Через сколько минут неоплаченный заказ отменяется.
UNPAID_MINUTES = int(os.getenv("UNPAID_ORDER_MINUTES", "30"))

# Пока окно оплаты открыто, заказ по таймауту не отменяем. Пять минут — с запасом
# на ввод карты и подтверждение по СМС. У Payme покупатель уходит на другую
# страницу, иногда в приложение Payme, — там дольше, поэтому запас больше.
CHECKOUT_GRACE = timedelta(minutes=15 if {"payme", "paynet", "click"} & set(PROVIDERS) else 5)

CURRENCY = "UZS"
# У сума в Telegram два знака после запятой (exp=2): суммы передаются в тийинах.
# 59 990 сумов — это 5 999 000.
MINOR = 100

# Как часто ищем просроченные неоплаченные заказы.
SWEEP_SECONDS = 60

# Что увидит покупатель в окне оплаты, если платить нельзя. Текст — ему, а не нам.
REFUSE = {
    "missing": "Заказ не найден. Оформите его заново.",
    "canceled": "Заказ отменён — оплатить его уже нельзя.",
    "busy": "Заказ уже в работе у магазина — оплатить его здесь нельзя. Свяжитесь с магазином.",
    "paid": "Этот заказ уже оплачен.",
    "not_card": "Этот заказ оформлен с оплатой наличными.",
    "amount": "Сумма заказа изменилась. Откройте заказ в приложении и оплатите заново.",
}


class PaymentError(Exception):
    """Счёт выставить не удалось. Текст — для покупателя."""


def configured(provider: str) -> bool:
    """Для способа заданы ключи."""
    refresh()
    if provider == "payme":
        return bool(PAYME_MERCHANT_ID and PAYME_KEY)
    if provider == "paynet":
        return bool(PAYNET_LOGIN and PAYNET_PASSWORD and PAYNET_SERVICE_ID)
    if provider == "click":
        return bool(CLICK_SERVICE_ID and CLICK_MERCHANT_ID and CLICK_SECRET_KEY)
    if provider == "telegram":
        return bool(TOKEN and SHOP_SECRET)
    return False


def provider_enabled(provider: str) -> bool:
    return provider in PROVIDERS and configured(provider)


def paynet_endpoint_ready() -> bool:
    """Отвечать ли Paynet. Хватает логина и пароля: номер сервиса Paynet
    выдаёт после того, как прогонит свои тесты на нашем адресе, — без ответа
    до номера дело бы не дошло. Покупателям Paynet появится только с номером
    (configured), так что до него платить через Paynet некому, кроме тестов."""
    refresh()
    return "paynet" in PROVIDERS and bool(PAYNET_LOGIN and PAYNET_PASSWORD)


def active() -> list[str]:
    """Включённые и настроенные способы — в порядке PAYMENT_PROVIDERS."""
    return [p for p in PROVIDERS if configured(p)]


def enabled() -> bool:
    return bool(active())


def is_test(provider: str | None = None) -> bool:
    provider = provider or PROVIDER
    if provider == "payme":
        return PAYME_TEST
    if provider == "paynet":
        return PAYNET_TEST
    if provider == "click":
        return False                        # тестовой среды у Click нет
    return ":TEST:" in TOKEN


def pilot(provider: str) -> bool:
    """Способ настоящий, но видят его пока только тестировщики."""
    return TESTERS_ONLY or (provider == "click" and CLICK_PILOT)


# ------------------------------------------------------------ настройки из панели

# Ключи и режимы оплаты администратор задаёт в панели («Онлайн оплата»).
# Хранятся в Setting: «pay:PAYME_KEY» и т. п. Заданное в панели перекрывает
# переменную окружения, пустое — возвращает её: так ключи, лежащие на сервере,
# продолжают работать, пока их не поменяли в панели.
#
# Переключатели способов — Setting «payment_on:payme» = "0" — выключен, нет
# записи — включён. Выключенный способ покупатель не видит (плитка «Скоро»),
# но уже начатые оплаты и возвраты сервер принимает как прежде — деньги не
# должны застрять.
CONFIG_PREFIX = "pay:"
SWITCH_PREFIX = "payment_on:"
# сколько секунд помним настройки: читать базу на каждый запрос незачем
SWITCH_TTL = 5

# имя -> разбор строки из базы; секреты панель целиком не показывает
CONFIG_FIELDS = {
    "PAYME_MERCHANT_ID": str, "PAYME_KEY": str, "PAYME_TEST": lambda v: v != "0",
    "PAYNET_SERVICE_ID": str, "PAYNET_LOGIN": str, "PAYNET_PASSWORD": str,
    "PAYNET_TEST": lambda v: v != "0", "PAYNET_PAY_URL": str,
    "CLICK_SERVICE_ID": str, "CLICK_MERCHANT_ID": str, "CLICK_MERCHANT_USER_ID": str,
    "CLICK_SECRET_KEY": str, "CLICK_PILOT": lambda v: v != "0", "CLICK_TIN": str,
    "CLICK_DELIVERY_SPIC": str, "CLICK_DELIVERY_PACKAGE": str,
    "REGOS_PAY_PAYME": str, "REGOS_PAY_CLICK": str, "REGOS_PAY_PAYNET": str,
    "REGOS_PAY_TELEGRAM": str,
    "FEE_PAYME": str, "FEE_CLICK": str, "FEE_PAYNET": str,
    "TEST_USERS": lambda v: {int(x) for x in v.replace(" ", "").split(",") if x.isdigit()},
    "TESTERS_ONLY": lambda v: v == "1",
}
SECRETS = {"PAYME_KEY", "PAYNET_PASSWORD", "CLICK_SECRET_KEY"}
# что панель показывает только хвостом «••••abcd»: секреты и ID кассы
MASKED = SECRETS | {"PAYME_MERCHANT_ID"}
# значения из окружения — к ним возвращаемся, если в панели поле очистили
_BASE = {name: globals()[name] for name in CONFIG_FIELDS}
_state = {"at": 0.0, "off": set(), "applied": set(), "panel": {}}


def refresh(force: bool = False) -> None:
    """Перечитывает настройки из базы, если прошло больше SWITCH_TTL."""
    now = time.time()
    if not force and now - _state["at"] < SWITCH_TTL:
        return
    from app.db import SessionLocal
    from app.models import Setting

    db = SessionLocal()
    try:
        rows = db.scalars(select(Setting).where(
            Setting.name.like(SWITCH_PREFIX + "%") | Setting.name.like(CONFIG_PREFIX + "%"))).all()
    except Exception:                       # noqa: BLE001
        # база недоступна — оставляем, что знали: молча включить выключенное нельзя
        log.exception("Настройки оплаты не прочитаны")
        _state["at"] = now
        return
    finally:
        db.close()

    off = {r.name[len(SWITCH_PREFIX):] for r in rows
           if r.name.startswith(SWITCH_PREFIX) and r.value == "0"}
    panel = {r.name[len(CONFIG_PREFIX):]: r.value for r in rows
             if r.name.startswith(CONFIG_PREFIX) and r.name[len(CONFIG_PREFIX):] in CONFIG_FIELDS
             and r.value.strip() != ""}
    # Меняем только то, что задано в панели, и возвращаем окружение тому,
    # что в панели очистили. Остального не касаемся — в том числе того, что
    # проверки подменяют прямо в модуле
    for name in set(_state["applied"]) - set(panel):
        globals()[name] = _BASE[name]
    for name, value in panel.items():
        globals()[name] = CONFIG_FIELDS[name](value.strip())
    _state.update(at=now, off=off, applied=set(panel), panel=panel)


def switched_off() -> set[str]:
    """Способы, выключенные администратором."""
    refresh()
    return _state["off"]


def set_switch(db, provider: str, on: bool) -> None:
    from app.models import Setting

    name = SWITCH_PREFIX + provider
    row = db.get(Setting, name)
    if row is None:
        db.add(Setting(name=name, value="1" if on else "0"))
    else:
        row.value = "1" if on else "0"
    db.commit()
    refresh(force=True)                     # сразу, а не через TTL


def set_config(db, values: dict[str, str]) -> None:
    """Записывает настройки из панели. Пустая строка — вернуть окружение."""
    from app.models import Setting

    for name, value in values.items():
        if name not in CONFIG_FIELDS:
            continue
        key = CONFIG_PREFIX + name
        row = db.get(Setting, key)
        if row is None:
            db.add(Setting(name=key, value=value))
        else:
            row.value = value
    db.commit()
    refresh(force=True)


def config_view() -> dict:
    """Настройки для панели: откуда значение и само значение — секрет
    только хвостом, «••••B?5»."""
    refresh()
    shown = {}
    for name in CONFIG_FIELDS:
        value = globals()[name]
        if name == "TEST_USERS":
            text = ",".join(str(x) for x in sorted(value))
        elif isinstance(value, bool):
            text = "1" if value else "0"
        else:
            text = str(value or "")
        source = "panel" if name in _state["applied"] else ("server" if text else "none")
        if name in MASKED:
            shown[name] = {"set": bool(text), "masked": ("••••" + text[-4:]) if text else "",
                           "source": source}
        else:
            shown[name] = {"value": text, "source": source}
    return shown


def regos_payment_type(provider: str | None) -> int | None:
    """Форма оплаты REGOS для способа, если задана в панели или окружении."""
    refresh()
    value = str(globals().get(f"REGOS_PAY_{(provider or '').upper()}", "") or "")
    return int(value) if value.isdigit() else None


def providers_for(telegram_id: int | None) -> list[str]:
    """Какими способами этому человеку можно платить. Тестовый способ —
    только тестировщикам: иначе любой «оплатит» тестовой картой. В пилоте
    (PAYMENT_TESTERS_ONLY) — и боевой тоже только им."""
    tester = telegram_id is not None and telegram_id in TEST_USERS
    off = switched_off()
    return [p for p in active()
            if p not in off and (tester or (not is_test(p) and not pilot(p)))]


def available_for(telegram_id: int | None, provider: str | None = None) -> bool:
    """Можно ли этому человеку платить картой — вообще или этим способом."""
    allowed = providers_for(telegram_id)
    return provider in allowed if provider else bool(allowed)


def awaiting_payment(order: Order) -> bool:
    """Заказ ждёт оплаты картой: оформлен онлайн, денег ещё нет."""
    return order.payment_method == "online" and order.paid_at is None \
        and order.status == "NEW"


def checkout_open(order: Order) -> bool:
    """Покупатель сейчас в окне оплаты: Telegram недавно спрашивал «можно ли»."""
    return (awaiting_payment(order) and order.checkout_at is not None
            and order.checkout_at > utcnow() - CHECKOUT_GRACE)


def pay_until(order: Order):
    """До какого времени неоплаченный заказ живёт."""
    if not awaiting_payment(order):
        return None
    return order.created_at + timedelta(minutes=UNPAID_MINUTES)


def prices(order: Order) -> list[dict]:
    """Строки счёта: товары и отдельно доставка.

    Покупатель должен видеть, за что платит. Сумма строк обязана сойтись
    с заказом до тийина — её же Telegram пришлёт в pre_checkout_query,
    и там мы её сверяем.
    """
    rows = []
    for item in order.items:
        count = f" × {item.quantity}" if item.quantity > 1 else ""
        rows.append({
            "label": f"{item.product_name} {item.weight}{count}"[:60],
            "amount": item.price * item.quantity * MINOR,
        })
    # бесплатную доставку строкой с нулём не показываем
    if order.delivery_price:
        rows.append({"label": "Доставка", "amount": order.delivery_price * MINOR})
    return rows


# Выставленные счета: номер заказа -> (сумма, ссылка, когда). Ссылка на счёт
# живёт, пока по ней не заплатили, и выставлять новую на каждое нажатие
# «Оплатить» незачем. А главное — нельзя: каждый createInvoiceLink идёт в лимит
# запросов магазинного бота, и упёршийся в него бот перестанет отвечать
# на запросы перед списанием у всех покупателей сразу.
_invoices: dict[str, tuple[int, str, float]] = {}
INVOICE_TTL = 30 * 60


def create_invoice_link(order: Order) -> str:
    """Ссылка на счёт для Telegram.WebApp.openInvoice.

    Выставляет магазинный бот — тот, из которого открыто приложение:
    платёж Telegram привязывает к боту, чей счёт.
    """
    cached = _invoices.get(order.number)
    if cached and cached[0] == order.total and time.time() - cached[2] < INVOICE_TTL:
        return cached[1]
    link = _create_invoice_link(order)
    _invoices[order.number] = (order.total, link, time.time())
    return link


def _create_invoice_link(order: Order) -> str:
    names = ", ".join(i.product_name for i in order.items[:3])
    if len(order.items) > 3:
        names += f" и ещё {len(order.items) - 3}"
    try:
        return notify.call("createInvoiceLink", {
            "title": f"Заказ {order.number}",
            "description": f"{names}. Доставка: {order.delivery_slot}"[:255],
            # по нему находим заказ, когда Telegram спрашивает и когда сообщает
            "payload": order.number,
            "provider_token": TOKEN,
            "currency": CURRENCY,
            "prices": prices(order),
        }, token=BOT_TOKEN)
    except notify.NotifyError as exc:
        log.warning("Счёт по заказу %s не выставлен: %s", order.number, exc)
        if "TOTAL_AMOUNT" in str(exc).upper():
            raise PaymentError(
                "Сумма меньше минимальной для оплаты картой. Выберите оплату наличными."
            ) from exc
        raise PaymentError("Не удалось выставить счёт. Попробуйте ещё раз.") from exc


def find(db, number: str) -> Order | None:
    return db.scalar(select(Order).where(Order.number == number))


def check_pre_checkout(db, query: dict) -> tuple[bool, str | None]:
    """Можно ли списывать деньги. Возвращает (да/нет, текст отказа)."""
    order = find(db, query.get("invoice_payload") or "")
    if order is None:
        return False, REFUSE["missing"]
    if order.payment_method != "online":
        return False, REFUSE["not_card"]
    if order.paid_at is not None:
        return False, REFUSE["paid"]
    if order.status == "CANCELED":
        return False, REFUSE["canceled"]
    if order.status != "NEW":
        return False, REFUSE["busy"]
    if query.get("currency") != CURRENCY or query.get("total_amount") != order.total * MINOR:
        log.warning("Заказ %s: сумма в окне оплаты %s %s, в заказе %s",
                    order.number, query.get("total_amount"), query.get("currency"),
                    order.total * MINOR)
        return False, REFUSE["amount"]
    # окно оплаты открыто: пока человек вводит карту, заказ не отменяем
    order.checkout_at = utcnow()
    db.commit()
    return True, None


def record_payment(db, payment: dict) -> tuple[str, Order | None]:
    """Проводит платёж. Возвращает, что вышло, и заказ.

    'confirmed' — заказ оплачен и принят, дальше в REGOS и сотрудникам;
    'duplicate' — это уведомление уже проводили, ничего не делаем;
    'late'      — деньги пришли за заказ, который уже не ждал оплаты
                  (отменён или оплачен другим платежом) — нужен человек;
    'unknown'   — заказа с таким номером нет, а деньги списаны.
    """
    charge = payment.get("telegram_payment_charge_id")
    if charge:
        already = db.scalar(select(Order).where(Order.payment_charge_id == charge))
        if already is not None:
            return "duplicate", already

    order = find(db, payment.get("invoice_payload") or "")
    if order is None:
        log.error("Оплата %s по неизвестному заказу %s", charge, payment.get("invoice_payload"))
        return "unknown", None

    if (payment.get("currency") != CURRENCY
            or payment.get("total_amount") != order.total * MINOR):
        # Сумму сверяли перед списанием, и счёт выставляем сами — расхождения
        # быть не должно. Но если оно есть, заказ по такому платежу не принимаем:
        # пусть разберётся человек
        log.error("Заказ %s: оплачено %s %s, а заказ на %s", order.number,
                  payment.get("total_amount"), payment.get("currency"), order.total * MINOR)
        return "late", order

    if order.paid_at is not None or order.status != "NEW":
        # Второй платёж по оплаченному или оплата отменённого: деньги списаны,
        # а заказ их уже не ждал. Молча проводить нельзя ни то, ни другое.
        log.error("Заказ %s: оплата %s пришла в состоянии %s, оплачен ранее: %s",
                  order.number, charge, order.status, order.paid_at)
        return "late", order

    moved = mark_paid(db, order, charge, payment.get("provider_payment_charge_id"),
                      payment["total_amount"] // MINOR)
    db.commit()
    db.refresh(order)
    if not moved:
        log.error("Заказ %s изменился в момент оплаты %s: сейчас %s",
                  order.number, charge, order.status)
        return "late", order
    _invoices.pop(order.number, None)
    log.info("Заказ %s оплачен картой, платёж %s", order.number, charge)
    return "confirmed", order


def mark_paid(db, order: Order, charge: str, provider_charge: str | None,
              amount: int) -> bool:
    """Отмечает заказ оплаченным и принятым. Коммит — на вызывающем коде.

    Условная запись: заказ всё ещё NEW и не оплачен. Если в эту же долю
    секунды его отменила автоотмена или провёл второй платёж, запись
    не пройдёт — и вызывающий код обязан не потерять деньги, а отказать
    или позвать человека.
    """
    # «NEW» — в самой записи, а не только в проверке до неё: move сверяет
    # статус с тем, что прочитан, и отменённый заказ иначе стал бы оплаченным
    return order_status.move(db, order, "CONFIRMED", unpaid_only=True,
                             where=[Order.status == "NEW"], values={
        "paid_at": utcnow(),
        "payment_charge_id": charge,
        "provider_charge_id": provider_charge,
        "paid_amount": amount,
    })


def cancel_stale(db) -> list[str]:
    """Отменяет неоплаченные заказы старше UNPAID_MINUTES.

    В REGOS такие не уходили (NEW не выгружается), поэтому отмена только у нас.
    Заказ с открытым окном оплаты не трогаем: деньги могут вот-вот списаться.
    """
    now = utcnow()
    stale = db.scalars(select(Order).where(
        Order.payment_method == "online",
        Order.status == "NEW",
        Order.paid_at.is_(None),
        Order.created_at < now - timedelta(minutes=UNPAID_MINUTES),
    )).all()
    canceled = []
    for order in stale:
        if order.checkout_at and order.checkout_at > now - CHECKOUT_GRACE:
            continue
        # Условно: между чтением и записью могла прийти оплата или открыться
        # окно оплаты — и то и другое проверяем в самой записи, а не по чтению
        if order_status.move(db, order, "CANCELED", unpaid_only=True, where=[
                or_(Order.checkout_at.is_(None),
                    Order.checkout_at <= now - CHECKOUT_GRACE)]):
            canceled.append(order.number)
            _invoices.pop(order.number, None)
    db.commit()
    if canceled:
        log.info("Отменены неоплаченные заказы: %s", ", ".join(canceled))
    return canceled


def worker() -> None:
    from app.db import SessionLocal

    while True:
        db = SessionLocal()
        try:
            cancel_stale(db)
            from app import click
            from app.regos import orders_push
            click.retry_fiscal(db)
            orders_push.fiscal_qr_pending(db)
        except Exception:                       # noqa: BLE001
            # поток обязан пережить что угодно: иначе неоплаченные заказы
            # начнут копиться, а служба останется живой, и никто не заметит
            db.rollback()
            log.exception("Сбой отмены неоплаченных заказов")
        finally:
            db.close()
        time.sleep(SWEEP_SECONDS)


_started = False


def start_worker() -> None:
    global _started
    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=worker, name="payments", daemon=True).start()
    log.info("Оплата картой включена: %s", ", ".join(
        p + (" (тестовый режим)" if is_test(p) else "") for p in active()))
