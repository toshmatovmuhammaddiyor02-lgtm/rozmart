"""Служебный бот магазина: заказы в Telegram сотрудникам.

Бот не покупательский. Он рассказывает магазину о заказах: пришёл новый —
карточка с составом, контактами и адресом; отменили — сообщение об этом.
Покупатель следит за своим заказом в самом приложении, в «Моих заказах».

Отсюда и устройство доступа: в рассылку попадает только чат, которому его
выдали в админ-панели (см. app/bot.py). В карточке телефон и адрес человека,
и подписаться на такое по собственному желанию нельзя.

Сообщения кладутся в очередь и уходят отдельным потоком. Отправлять прямо
из обработчика запроса нельзя: недоступный Telegram задерживал бы ответ
покупателю, а перезапуск службы посреди отправки терял бы сообщение молча.
"""
import html
import http.client
import json
import logging
import os
import socket
import ssl
import threading
import time
from datetime import timedelta

from sqlalchemy import select

from app.models import Notification, Order, StaffChat, utcnow

log = logging.getLogger(__name__)

TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "").strip()

# Имя бота нужно приложению для ссылки t.me/<имя>?start=…. Берём его у самого
# Telegram, чтобы не заводить ещё одну переменную окружения, которую однажды
# забудут поправить после смены бота. Переменная всё же есть — на случай,
# когда при старте нет связи.
USERNAME = os.getenv("NOTIFY_BOT_USERNAME", "").strip().lstrip("@")

API_HOST = "api.telegram.org"
# Подключение — коротко: с сервера до Telegram оно иногда зависает
# (пойманы зависания на 30 секунд), и ждать его дольше бессмысленно —
# быстрее подключиться заново. Ответа ждём дольше: его Telegram отдаёт сам
CONNECT_TIMEOUT = 5
TIMEOUT = 20

# Сколько раз пробуем отправить, прежде чем сдаться. Перегрузка Telegram
# и обрыв связи лечатся повтором, всё остальное повтором не лечится.
MAX_ATTEMPTS = 5

# Сообщение о заказе имеет смысл, пока заказ свеж. Чат, которому выдали доступ
# через неделю, не должен получить ворох старых заказов.
MAX_AGE = timedelta(hours=24)

# Как часто поток разбирает очередь. Свежие сообщения уходят сразу — их
# отправляет фоновая задача запроса; этот проход нужен для повторов и для чатов,
# которым доступ выдали уже после оформления заказа.
SWEEP_SECONDS = 30


class NotifyError(Exception):
    """Отправить не удалось. permanent=True — повторять бессмысленно."""

    def __init__(self, message: str, permanent: bool = False, retry_after: int = 0):
        super().__init__(message)
        self.permanent = permanent
        self.retry_after = retry_after


def enabled() -> bool:
    return bool(TOKEN)


def call(method: str, payload: dict, token: str | None = None) -> dict:
    """Запрос к Bot API. Возвращает result, на отказ поднимает NotifyError.

    token — чужой токен, если спрашиваем не от имени служебного бота. Им
    пользуется оплата: счёт выставляет магазинный бот, а разбирать его
    ответы надо ровно так же.

    Telegram отвечает содержательной ошибкой в теле и ставит код состояния
    4xx, из-за которого urllib поднимает HTTPError раньше, чем мы прочитаем
    описание. Поэтому тело читается и в этом случае: без него в журнале
    остаётся голое «HTTP Error 403: Forbidden», по которому непонятно,
    заблокировал ли покупатель бота или неверен токен.
    """
    raw = _post(f"/bot{token or TOKEN}/{method}", json.dumps(payload).encode(), method)
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise NotifyError(f"{method}: Telegram ответил не JSON") from exc

    if body.get("ok"):
        return body.get("result", {})

    code = body.get("error_code")
    description = body.get("description", "")
    retry_after = int((body.get("parameters") or {}).get("retry_after", 0))
    # 403 — покупатель не нажимал «Старт» или заблокировал бота; 400 «chat not
    # found» — чата нет вовсе. Ни то, ни другое не исправится повтором
    permanent = code == 403 or (code == 400 and "chat not found" in description.lower())
    raise NotifyError(f"{method}: {code} {description}", permanent, retry_after)


# Соединение с Telegram держим открытым — своё у каждого потока: запросы
# идут и из обработчиков, и из фоновых потоков, а одно соединение на всех
# пришлось бы запирать. Новое соединение — это ещё и TLS-рукопожатие, около
# 0,1 с; у бота доставщиков список из 15 заказов — 16 запросов подряд.
_local = threading.local()
_tls = ssl.create_default_context()


def _connection() -> http.client.HTTPSConnection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = http.client.HTTPSConnection(API_HOST, timeout=CONNECT_TIMEOUT, context=_tls)
        _local.conn = conn
    if conn.sock is None:
        conn.connect()                   # с коротким тайм-аутом подключения
        conn.sock.settimeout(TIMEOUT)    # а ответа ждём дольше
    return conn


def _drop_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
    _local.conn = None


def _post(path: str, data: bytes, method: str) -> bytes:
    """POST к Bot API по открытому соединению. Тело ответа — и при 4xx:
    в нём Telegram объясняет, что не так.

    Повторяем один раз — и только когда запрос до Telegram точно не дошёл:
    не удалось подключиться, или Telegram успел закрыть простаивавшее
    соединение. Повтор после отправленного запроса мог бы прислать
    сообщение дважды.
    """
    for attempt in (1, 2):
        sent = False
        try:
            conn = _connection()
            conn.request("POST", path, body=data,
                         headers={"Content-Type": "application/json"})
            sent = True
            response = conn.getresponse()
            body = response.read()
            if response.status >= 500 and not body:
                raise NotifyError(f"{method}: HTTP {response.status}")
            return body
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError) as exc:
            # простаивавшее соединение закрыли на той стороне — откроем новое
            _drop_connection()
            if attempt == 2:
                raise NotifyError(f"{method}: нет связи с Telegram ({exc})") from exc
        except (socket.timeout, TimeoutError, OSError, http.client.HTTPException) as exc:
            _drop_connection()
            if sent or attempt == 2:
                # сеть или недоступный Telegram — тот случай, когда повтор
                # помогает, но уже позже и с очереди
                raise NotifyError(f"{method}: нет связи с Telegram ({exc})") from exc
    raise NotifyError(f"{method}: нет связи с Telegram")


def bot_username() -> str:
    """Имя бота для ссылки. Спрашивается один раз и запоминается."""
    global USERNAME
    if USERNAME or not TOKEN:
        return USERNAME
    try:
        USERNAME = call("getMe", {}).get("username", "")
    except NotifyError as exc:
        log.warning("Не удалось узнать имя информационного бота: %s", exc)
    return USERNAME


def send_message(chat_id: int, text: str) -> None:
    call("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


# ------------------------------------------------------------------ тексты


def money(value: int, lang: str = "ru") -> str:
    """125000 -> «125 000 сум». Пробелы неразрывные, чтобы сумма не ломалась."""
    return f"{value:,}".replace(",", " ") + (" so'm" if lang == "uz" else " сум")


# Сообщения сотрудникам на двух языках. Язык — у каждого чата свой
# (StaffChat.language): в личном — по Telegram сотрудника, в группе — того,
# кто добавил бота; администратор может переключить в панели. Узбекский —
# латиницей. Имена, адреса и товары приходят из заказа и не переводятся.
LANGS = ("ru", "uz")
STAFF_TEXTS = {
    "ru": {
        "new_order": "🆕 Новый заказ <b>{number}</b> — {total}",
        "more": "…и ещё {n}",
        "map_point": "точка на карте",
        "pay_cash": "наличными курьеру",
        "pay_online": "онлайн картой",
        "paid": "💳 <b>ОПЛАЧЕНО КАРТОЙ — деньги с покупателя не брать</b>",
        "paid_test": " (ТЕСТОВАЯ ОПЛАТА — денег не поступало)",
        "canceled": "❌ Заказ <b>{number}</b> отменён ({source})",
        "canceled_paid": "⚠️ <b>Заказ был оплачен картой — нужен возврат покупателю</b>",
        "src_panel": "в панели",
        "src_regos": "на кассе",
        "taken": "🚚 Заказ <b>{number}</b> взял доставщик {courier}",
        "delivered": "✅ Заказ <b>{number}</b> доставлен — {courier}.",
        "cash_got": " Получено наличными: {total}.",
        "dropped": "↩️ Доставщик {courier} отказался от заказа <b>{number}</b> — заказ снова свободен",
        "drop_reason": "💬 Причина: {reason}",
        "courier_off": "↩️ Доставщик {courier} отключён — заказ <b>{number}</b> снова свободен",
        "refund": "↩️ Возврат через Payme по заказу <b>{number}</b> — {total} вернутся покупателю.",
        "refund_paynet": "↩️ Paynet отменил оплату заказа <b>{number}</b> — {total} вернутся покупателю.",
        "click_late_back": "↩️ Click: оплата заказа <b>{number}</b> пришла, когда заказ уже был "
                           "«{state}». {total} возвращены покупателю (платёж {payment}).",
        "click_late_manual": "⚠️ Click: оплата заказа <b>{number}</b> пришла, когда заказ уже был "
                             "«{state}». Списано {total}, платёж {payment}.\n"
                             "Вернуть автоматически не удалось — верните в кабинете "
                             "merchant.click.uz или восстановите заказ.",
        "refund_canceled": "Заказ отменён — не собирать и не выдавать.",
        "refund_status": "Статус заказа: {status}.",
        "late": "⚠️ Оплата картой по заказу <b>{number}</b>, а заказ {state}.\n"
                "Списано {total}, платёж {charge}.\n"
                "Нужен возврат покупателю или восстановление заказа — решите вручную.",
        "late_paid": "уже оплачен",
        "late_canceled": "отменён",
        "unknown": "⚠️ Оплата картой по неизвестному заказу «{payload}».\n"
                   "Списано {total}, платёж {charge}. Нужен возврат.",
        "welcome_staff": "Этот чат подключён к заказам ROZMART.\n\n"
                         "Сюда приходит каждый новый заказ и сообщения об отменах.",
        "welcome_pending": "Это служебный бот магазина ROZMART — сюда приходят заказы "
                           "сотрудникам.\n\nЧат я запомнил. Чтобы заказы приходили и сюда, "
                           "попросите администратора включить его в панели: «Состав каталога» → "
                           "«Кому приходят заказы».\n\nЕсли вы покупатель — заказ и его состояние "
                           "видно в самом приложении магазина, в разделе «Мои заказы».",
    },
    "uz": {
        "new_order": "🆕 Yangi buyurtma <b>{number}</b> — {total}",
        "more": "…yana {n} ta",
        "map_point": "xaritadagi nuqta",
        "pay_cash": "kuryerga naqd pul",
        "pay_online": "karta orqali onlayn",
        "paid": "💳 <b>KARTA ORQALI TO'LANGAN — xaridordan pul olinmasin</b>",
        "paid_test": " (TEST TO'LOVI — pul tushmagan)",
        "canceled": "❌ <b>{number}</b> buyurtma bekor qilindi ({source})",
        "canceled_paid": "⚠️ <b>Buyurtma karta orqali to'langan edi — xaridorga pulni qaytarish kerak</b>",
        "src_panel": "panelda",
        "src_regos": "kassada",
        "taken": "🚚 <b>{number}</b> buyurtmani yetkazib beruvchi {courier} oldi",
        "delivered": "✅ <b>{number}</b> buyurtma yetkazildi — {courier}.",
        "cash_got": " Naqd olindi: {total}.",
        "dropped": "↩️ Yetkazib beruvchi {courier} <b>{number}</b> buyurtmadan voz kechdi — "
                   "buyurtma yana bo'sh",
        "drop_reason": "💬 Sabab: {reason}",
        "courier_off": "↩️ Yetkazib beruvchi {courier} o'chirildi — <b>{number}</b> buyurtma yana bo'sh",
        "refund": "↩️ <b>{number}</b> buyurtma bo'yicha Payme orqali qaytarish — {total} "
                  "xaridorga qaytariladi.",
        "refund_paynet": "↩️ Paynet <b>{number}</b> buyurtma to'lovini bekor qildi — {total} "
                         "xaridorga qaytariladi.",
        "click_late_back": "↩️ Click: <b>{number}</b> buyurtma to'lovi buyurtma «{state}» "
                           "bo'lgandan keyin keldi. {total} xaridorga qaytarildi (to'lov {payment}).",
        "click_late_manual": "⚠️ Click: <b>{number}</b> buyurtma to'lovi buyurtma «{state}» "
                             "bo'lgandan keyin keldi. Yechildi {total}, to'lov {payment}.\n"
                             "Avtomatik qaytarib bo'lmadi — merchant.click.uz kabinetida qaytaring "
                             "yoki buyurtmani tiklang.",
        "refund_canceled": "Buyurtma bekor qilindi — yig'ilmasin va berilmasin.",
        "refund_status": "Buyurtma holati: {status}.",
        "late": "⚠️ <b>{number}</b> buyurtma uchun karta orqali to'lov keldi, lekin buyurtma {state}.\n"
                "Yechildi {total}, to'lov {charge}.\n"
                "Xaridorga pulni qaytarish yoki buyurtmani tiklash kerak — qo'lda hal qiling.",
        "late_paid": "allaqachon to'langan",
        "late_canceled": "bekor qilingan",
        "unknown": "⚠️ Noma'lum buyurtma «{payload}» uchun karta orqali to'lov.\n"
                   "Yechildi {total}, to'lov {charge}. Pulni qaytarish kerak.",
        "welcome_staff": "Bu chat ROZMART buyurtmalariga ulangan.\n\n"
                         "Bu yerga har bir yangi buyurtma va bekor qilinganlar haqida xabar keladi.",
        "welcome_pending": "Bu ROZMART do'konining xizmat boti — xodimlarga buyurtmalar shu yerga "
                           "keladi.\n\nChatni eslab qoldim. Buyurtmalar bu yerga ham kelishi uchun "
                           "administratordan uni panelda yoqishni so'rang: «Состав каталога» → "
                           "«Кому приходят заказы».\n\nAgar siz xaridor bo'lsangiz — buyurtmangiz "
                           "va uning holati do'kon ilovasida, «Mening buyurtmalarim» bo'limida.",
    },
}

# Срок доставки хранится в заказе по-русски — это значение из формы
SLOTS_UZ = {
    "Как можно скорее": "Imkon qadar tezroq",
    "Сегодня вечером": "Bugun kechqurun",
    "Завтра утром": "Ertaga ertalab",
}


def st(lang: str, key: str, **values) -> str:
    return (STAFF_TEXTS.get(lang) or STAFF_TEXTS["ru"])[key].format(**values)


def lang_of(sender: dict | None) -> str:
    """Язык по настройке Telegram: узбекский — у кого Telegram на узбекском."""
    code = ((sender or {}).get("language_code") or "").lower()
    return "uz" if code.startswith("uz") else "ru"


def slot(order: Order, lang: str) -> str:
    return SLOTS_UZ.get(order.delivery_slot, order.delivery_slot) if lang == "uz" \
        else order.delivery_slot


def esc(text: str) -> str:
    """Экранирование: в имени товара и в адресе встречается «&», а разметка
    сообщения — HTML, и такой текст Telegram отвергает целиком."""
    return html.escape(text or "")


# Длинный заказ не режем на части, но и список из полусотни строк в сообщении
# нечитаем: показываем начало и говорим, сколько осталось.
ITEMS_SHOWN = 15


# --------------------------------------------------------- сообщения магазину

# Карточка заказа для сотрудников. Здесь, в отличие от сообщения покупателю,
# нужны контакты и адрес: по ним заказ собирают и везут. Поэтому подписка
# на эти сообщения закрыта кодом — см. app/bot.py.


def map_link(order: Order) -> str | None:
    """Ссылка на точку, которую покупатель поставил на карте.

    Адрес строкой курьеру всё равно нужен — по нему подъезд и квартира,
    — а вот дом он находит по этой ссылке. Открывается тем приложением,
    которое у него стоит: Google и Яндекс.Карты понимают такую ссылку оба.
    """
    if order.lat is None or order.lon is None:
        return None
    return f"https://maps.google.com/?q={order.lat},{order.lon}"


def payment_line(order: Order, lang: str = "ru") -> str:
    """Строка об оплате. У оплаченного картой — крупно и без двусмысленности:
    курьер, который возьмёт деньги второй раз, — худшее, что тут может быть."""
    if order.paid_at is not None:
        from app import payments
        return st(lang, "paid") + (st(lang, "paid_test") if payments.is_test(order.pay_provider) else "")
    key = {"cash": "pay_cash", "online": "pay_online"}.get(order.payment_method)
    return f"💵 {st(lang, key) if key else esc(order.payment_method)}"


def staff_new_order(order: Order, lang: str = "ru") -> str:
    lines = [st(lang, "new_order", number=esc(order.number), total=money(order.total, lang)), ""]
    for item in order.items[:ITEMS_SHOWN]:
        count = f" × {item.quantity}" if item.quantity > 1 else ""
        lines.append(f"• {esc(item.product_name)} {esc(item.weight)}{count} — "
                     f"{money(item.price * item.quantity, lang)}")
    if len(order.items) > ITEMS_SHOWN:
        lines.append(st(lang, "more", n=len(order.items) - ITEMS_SHOWN))

    contact = [esc(order.customer_name), esc(order.phone)]
    if order.telegram_username:
        contact.append(f"@{esc(order.telegram_username)}")
    point = map_link(order)
    lines += [
        "",
        " · ".join(contact),
        f"📍 {esc(order.address)}"
        + (f' — <a href="{point}">{st(lang, "map_point")}</a>' if point else ""),
        f"🕐 {esc(slot(order, lang))}",
        payment_line(order, lang),
    ]
    if order.comment:
        lines.append(f"📝 {esc(order.comment)}")
    return "\n".join(lines)


# откуда пришла отмена — по-русски её передают вызывающие, переводим здесь
SOURCES = {"в панели": "src_panel", "на кассе": "src_regos"}


def staff_canceled(order: Order, source: str, lang: str = "ru") -> str:
    """source — откуда пришла отмена: «в панели», «на кассе». Сотруднику важно
    знать, чьё это действие: своё или кассы."""
    where = st(lang, SOURCES[source]) if source in SOURCES else esc(source)
    text = (st(lang, "canceled", number=esc(order.number), source=where) + "\n"
            f"{esc(order.customer_name)} · {esc(order.phone)} · {money(order.total, lang)}")
    if order.paid_at is not None:
        # возвраты на пилоте ручные: без этой строки про деньги покупателя забудут
        text += "\n" + st(lang, "canceled_paid")
    return text


def staff_chats(db) -> list[StaffChat]:
    return list(db.scalars(select(StaffChat).where(StaffChat.active)))


def queue_staff(db, order: Order | None, text) -> list[Notification]:
    """Кладёт сообщение во все чаты сотрудников. Коммит — на вызывающем коде.

    text — строка или функция от языка: тогда каждому чату — на его языке.
    """
    if not enabled():
        return []
    rows = []
    for chat in staff_chats(db):
        body = text(chat.language if chat.language in LANGS else "ru") if callable(text) else text
        row = Notification(
            # для сообщения сотрудникам адресат известен сразу: это сам чат,
            # искать его по подписке покупателя не нужно
            telegram_id=chat.chat_id, chat_id=chat.chat_id,
            order_id=order.id if order is not None else None, kind="staff", text=body,
        )
        db.add(row)
        rows.append(row)
    return rows


def send_many(notification_ids: list[int]) -> None:
    """Фоновая отправка нескольких сообщений — обычно это рассылка сотрудникам."""
    for notification_id in notification_ids:
        send_one(notification_id)


# ------------------------------------------------------------------ очередь


def deliver(db, row: Notification) -> bool:
    """Пытается отправить одно сообщение. True — ушло."""
    # Доступ проверяем в момент отправки, а не когда клали в очередь: если его
    # успели отозвать, чат не должен получить заказ, «уже выписанный» на него
    chat = db.get(StaffChat, row.chat_id)
    if chat is None or not chat.active:
        return False

    try:
        send_message(chat.chat_id, row.text)
    except NotifyError as exc:
        row.error = str(exc)[:500]
        if exc.permanent:
            # бота заблокировали или выгнали из группы: слать туда больше
            # некуда, пока чат не включат заново
            chat.active = False
            log.info("Чат %s недоступен для бота: %s", chat.chat_id, exc)
        else:
            row.attempts += 1
            log.warning("Сообщение %s не ушло: %s", row.id, exc)
        return False

    row.sent_at = utcnow()
    row.error = None
    return True


def send_one(notification_id: int) -> None:
    """Отправка одного сообщения фоном, сразу после события.

    Своя сессия базы: та, в которой создавался заказ, к этому моменту закрыта.
    Ошибки наружу не выпускаем — заказ уже оформлен, и молчащий Telegram
    не повод отвечать покупателю отказом. Что не ушло, доберёт поток очереди.
    """
    from app.db import SessionLocal

    if not enabled():
        return
    db = SessionLocal()
    try:
        row = db.get(Notification, notification_id)
        if row is not None and row.sent_at is None:
            deliver(db, row)
            db.commit()
    except Exception:                       # noqa: BLE001
        log.exception("Сбой отправки сообщения %s", notification_id)
    finally:
        db.close()


def pending(db) -> list[Notification]:
    """Что ещё не ушло и что ещё имеет смысл отправлять."""
    return list(db.scalars(
        select(Notification)
        .where(
            Notification.sent_at.is_(None),
            Notification.attempts < MAX_ATTEMPTS,
            Notification.created_at > utcnow() - MAX_AGE,
        )
        .order_by(Notification.id)
        .limit(100)
    ))


def sweep() -> int:
    """Проход по очереди. Возвращает, сколько сообщений ушло."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        rows = pending(db)
        sent = 0
        for row in rows:
            if deliver(db, row):
                sent += 1
                # Telegram разрешает около сообщения в секунду на чат и 30
                # в секунду всего; рассылок у нас нет, но пауза дешевле, чем
                # разбираться потом с ошибкой 429
                time.sleep(0.05)
        if rows:
            db.commit()
        return sent
    finally:
        db.close()


def worker() -> None:
    while True:
        try:
            sweep()
        except Exception:                   # noqa: BLE001
            # поток обязан пережить любую ошибку: иначе уведомления тихо
            # прекратятся, а служба останется живой, и никто не заметит
            log.exception("Сбой разбора очереди уведомлений")
        time.sleep(SWEEP_SECONDS)


_started = False


def start_worker() -> None:
    """Запускается при старте приложения.

    Поток, а не задача asyncio: внутри обычные синхронные обращения к базе
    и к Telegram, и блокировать ими цикл событий, из которого отвечают
    покупателю, незачем.
    """
    global _started
    if _started or not enabled():
        return
    _started = True
    threading.Thread(target=worker, name="notify", daemon=True).start()
    log.info("Информационный бот включён")
