"""Оформление заказа."""
from datetime import timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import i18n, notify, order_status, payments, photos, zones
from app.db import get_db
from app.models import Counter, Order, OrderItem, Setting, Variant
from app.profile import save_customer
from app.regos import prices
from app.schemas import OrderIn, OrderItemOut, OrderOut
from app.telegram import TelegramUser, buyer, current_user, require_user

router = APIRouter(prefix="/api", tags=["Заказы"])

# BR-11: стоимость доставки фиксированная и единая для зоны обслуживания.
# Меняет её администратор в панели (FR-13.6), значение лежит в настройках.
# Здесь — только цена по умолчанию, пока в панели её не задавали
DELIVERY_PRICE = 12000
DELIVERY_SETTING = "delivery_price"
# защита от лишнего нуля: доставка по городу дороже миллиона — опечатка
MAX_DELIVERY_PRICE = 1_000_000


def delivery_price(db: Session) -> int:
    """Стоимость доставки для новых заказов. Оформленные хранят свою (BR-10)."""
    row = db.get(Setting, DELIVERY_SETTING)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return DELIVERY_PRICE


def save_delivery_price(db: Session, price: int) -> None:
    row = db.get(Setting, DELIVERY_SETTING)
    if row is None:
        db.add(Setting(name=DELIVERY_SETTING, value=str(price)))
    else:
        row.value = str(price)

STATUS_BY_PAYMENT = order_status.BY_PAYMENT

# розница: больше сотни одинаковых пачек — это уже опт, такие заказы идут через оператора
MAX_ITEM_QUANTITY = 99

# Состояния описаны в app/order_status.py — оттуда же их берёт панель оператора.
# Держать здесь свой список нельзя: ровно так покупатель однажды увидел
# «В работе» вместо «Отменён».


ORDER_COUNTER = "order_number"
ORDER_NUMBER_BASE = 8000


def build_order_number(value: int) -> str:
    """RB-8001, RB-8002 … Номер присваивается один раз и не меняется (BR-05)."""
    return f"RB-{value}"


def next_order_number(db: Session) -> str:
    """Выдаёт следующий номер заказа. Атомарно и до вставки строки.

    UPDATE ... RETURNING берёт на строке счётчика блокировку до конца
    транзакции, поэтому два одновременных заказа получают разные номера,
    а не спорят за один. Раньше номер выводился из id строки, из-за чего до
    вставки в number лежала пустая строка — на PostgreSQL одновременные заказы
    ломались об уникальный индекс по ней.

    Счётчик заводится при первом заказе и начинается выше уже существующих
    номеров: база могла быть заполнена, когда нумерация шла от id.
    """
    value = db.scalar(
        update(Counter)
        .where(Counter.name == ORDER_COUNTER)
        .values(value=Counter.value + 1)
        .returning(Counter.value)
    )
    if value is None:
        start = max(ORDER_NUMBER_BASE, (db.scalar(select(func.max(Order.id))) or 0) + ORDER_NUMBER_BASE)
        db.add(Counter(name=ORDER_COUNTER, value=start + 1))
        db.flush()
        value = start + 1
    return build_order_number(value)


ITEMS_WITH_PHOTO = selectinload(Order.items).selectinload(OrderItem.variant).selectinload(
    Variant.product
)


def find_by_client_key(db: Session, client_key: str) -> Order | None:
    return db.scalar(
        select(Order).where(Order.client_key == client_key).options(ITEMS_WITH_PHOTO)
    )


def item_photo(item: OrderItem) -> str | None:
    """Фото товара для строки заказа.

    Берётся из карточки, а не из самой строки: название и цена в заказе
    зафиксированы на момент оформления (BR-09), а фото — просто изображение
    той же вещи, и показывать устаревшее незачем. Если карточку успели удалить,
    остаёмся без картинки, но заказ всё равно читается.
    """
    variant = item.variant
    photo = variant.product.photo if variant is not None and variant.product else None
    return photos.url(photo)


def to_out(order: Order) -> OrderOut:
    return OrderOut(
        number=order.number,
        status=order.status,
        status_text=order_status.text(order.status),
        # в базе время без пояса, но оно всегда UTC: помечаем явно, иначе
        # телефон покажет его как местное и промахнётся на пять часов
        created_at=order.created_at.replace(tzinfo=timezone.utc),
        delivery_slot=order.delivery_slot,
        payment_method=order.payment_method,
        address=order.address,
        phone=order.phone,
        comment=order.comment,
        goods_total=order.goods_total,
        delivery_price=order.delivery_price,
        total=order.total,
        paid=order.paid_at is not None,
        pay_provider=order.pay_provider,
        pay_until=(until.replace(tzinfo=timezone.utc)
                   if (until := payments.pay_until(order)) else None),
        items=[
            OrderItemOut(
                product_name=i.product_name, weight=i.weight, price=i.price,
                quantity=i.quantity, photo=item_photo(i),
            )
            for i in order.items
        ],
    )


@router.get("/delivery-zone")
def delivery_zone(
    lat: float | None = None,
    lon: float | None = None,
    db: Session = Depends(get_db),
    x_lang: str | None = Header(default=None, alias="X-Lang"),
):
    """Доставляем ли в эту точку — приложение спрашивает, как только покупатель
    поставил булавку, чтобы не заполнять форму зря."""
    lang = i18n.lang_of(x_lang)
    ok, why, district = zones.check(db, lat, lon, lang)
    return {"enabled": bool(zones.allowed(db)), "ok": ok, "message": why,
            "district": zones.name(district, lang) if district else None}


@router.get("/delivery-zone/map")
def delivery_zone_map(response: Response, db: Session = Depends(get_db)):
    """Районы доставки для карты в приложении — чтобы было видно, куда возим."""
    codes = zones.allowed(db)
    # меняется редко, но после правки в панели должно доехать быстро
    response.headers["Cache-Control"] = "public, max-age=300"
    return {"enabled": bool(codes), "districts": zones.map_shapes(tuple(codes))}


@router.post("/orders", response_model=OrderOut, status_code=201)
def create_order(
    data: OrderIn,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: TelegramUser | None = Depends(buyer),
    x_lang: str | None = Header(default=None, alias="X-Lang"),
):
    """Создаёт заказ. Цены берутся из базы, а не из запроса клиента."""
    telegram_id = user.id if user else None
    client_key = data.client_key
    if client_key:
        already = find_by_client_key(db, client_key)
        if already is not None:
            # свой ключ — возвращаем тот же заказ вместо второго такого же.
            # чужой — просто не занимаем его: подставив украденный ключ, чужой
            # заказ не прочитать, а свой оформить можно
            if already.telegram_id == telegram_id:
                return to_out(already)
            client_key = None

    # Оплату картой проверяет сервер, а не витрина: скрыть кнопку — не защита.
    # Пока токен тестовый, платить картой могут только тестировщики — иначе
    # любой «оплатит» заказ тестовой картой и получит товар даром
    pay_provider = None
    if data.payment_method == "online":
        allowed = payments.providers_for(telegram_id)
        pay_provider = data.pay_provider or (allowed[0] if allowed else None)
        if pay_provider not in allowed:
            raise HTTPException(
                status_code=400,
                detail="Оплата картой пока недоступна. Выберите оплату наличными.",
            )

    # Зона доставки — по точке на карте (app/zones.py). Проверяет сервер:
    # предупреждение в приложении — подсказка, а не защита
    ok, why, _ = zones.check(db, data.lat, data.lon, i18n.lang_of(x_lang))
    if not ok:
        raise HTTPException(status_code=400, detail=why)

    quantities: dict[int, int] = {}
    for item in data.items:
        quantities[item.variant_id] = quantities.get(item.variant_id, 0) + item.quantity

    # ограничение из схемы — на строку, а одинаковые строки мы складываем
    if any(quantity > MAX_ITEM_QUANTITY for quantity in quantities.values()):
        raise HTTPException(
            status_code=400,
            detail=f"Не больше {MAX_ITEM_QUANTITY} штук одной позиции в заказе",
        )

    variants = db.scalars(
        select(Variant)
        .where(Variant.id.in_(quantities))
        .options(selectinload(Variant.product))
    ).all()

    found = {v.id for v in variants}
    missing = set(quantities) - found
    if missing:
        raise HTTPException(status_code=400, detail="В корзине есть товары, которых больше нет")

    order = Order(
        number=next_order_number(db),
        status=STATUS_BY_PAYMENT[data.payment_method],
        client_key=client_key,
        telegram_id=telegram_id,
        telegram_username=user.username if user else None,
        customer_name=data.customer_name,
        phone=data.phone,
        # в заказе адрес лежит одной строкой: его читают курьер, касса и REGOS
        address=data.full_address(),
        lat=data.lat,
        lon=data.lon,
        delivery_slot=data.delivery_slot,
        comment=data.comment,
        payment_method=data.payment_method,
        pay_provider=pay_provider,
        price_type_id=prices.current(db),
        goods_total=0,
        delivery_price=delivery_price(db),
        total=0,
    )

    goods_total = 0
    for variant in variants:
        # проданной считается только опубликованная фасовка с ценой: иначе можно
        # оформить скрытый товар, зная его номер
        if not variant.is_active or variant.price is None or not variant.product.is_active:
            raise HTTPException(
                status_code=400,
                detail=f"«{variant.product.name}» сейчас недоступен для заказа",
            )
        quantity = quantities[variant.id]
        goods_total += variant.price * quantity
        order.items.append(
            OrderItem(
                variant_id=variant.id,
                product_name=variant.product.name,
                weight=variant.weight,
                price=variant.price,
                quantity=quantity,
            )
        )

    order.goods_total = goods_total
    order.total = goods_total + order.delivery_price

    # покупатель подтверждает конкретную сумму: если каталог успел измениться,
    # оформляем не молча по новой цене, а возвращаем его в корзину
    if data.expected_total is not None and data.expected_total != order.total:
        raise HTTPException(
            status_code=409,
            detail="Цены изменились. Проверьте корзину и подтвердите заказ заново.",
        )

    # запоминаем покупателя, чтобы в следующий раз не набирал то же самое.
    # Одной транзакцией с заказом: заказ без согласия на обработку не создаётся.
    # Уже сохранённый профиль заказ не трогает — его меняют только вручную
    if user:
        save_customer(db, user.id, data, update_existing=False)

    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        # два одинаковых запроса пришли одновременно: заказ уже создал первый
        db.rollback()
        already = find_by_client_key(db, client_key) if client_key else None
        if already is None or already.telegram_id != telegram_id:
            raise
        return to_out(already)

    # Заказ — сотрудникам магазина в Telegram: с контактами и адресом, потому
    # что по этому сообщению его и собирают. Кладём в очередь и отправляем
    # фоном: покупатель должен увидеть экран с номером заказа сразу, а не
    # после того, как ответит Telegram.
    # Заказ с оплатой картой — только после оплаты (app/shop_bot.py): собирать
    # неоплаченный незачем, а пометка «оплачено» появится там же
    staff = ([] if order.payment_method == "online"
             else notify.queue_staff(db, order, lambda lang: notify.staff_new_order(order, lang)))
    if staff:
        db.commit()
        background.add_task(notify.send_many, [row.id for row in staff])

    # По умолчанию заказ передаёт оператор кнопкой в панели: он сперва смотрит
    # на заказ. Если в панели включена автоотправка — уходит сам, но всё равно
    # после ответа покупателю: недоступная учётная система не должна мешать
    # оформлению. Не получилось — заказ останется в панели с текстом ошибки.
    from app.regos.orders_push import push_one
    background.add_task(push_one, order.id)
    return to_out(order)


@router.post("/orders/{number}/invoice")
def order_invoice(
    number: str,
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_user),
    x_lang: str | None = Header(default=None, alias="X-Lang"),
):
    """Счёт на оплату заказа картой.

    Выставляется заново при каждом нажатии «Оплатить»: закрытое без оплаты
    окно — не повод оформлять заказ повторно.

    kind подсказывает приложению, как открыть ссылку: `telegram` — окном
    оплаты Telegram, `payme` — страницей Payme, `click` — страницей Click,
    `paynet` — что ввести в Paynet.

    provider — другой способ, чем выбранный при оформлении: не вышло через
    Payme — можно через Paynet. Выбор запоминается у заказа.
    """
    order = db.scalar(
        select(Order).where(Order.number == number).options(selectinload(Order.items))
    )
    # чужой заказ отвечает так же, как несуществующий: по ответу не должно
    # быть видно, что заказ с таким номером у кого-то есть
    if order is None or order.telegram_id != user.id:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.payment_method != "online":
        raise HTTPException(status_code=409, detail="Заказ оформлен с оплатой наличными")
    if order.paid_at is not None:
        raise HTTPException(status_code=409, detail="Заказ уже оплачен")
    if order.status != "NEW":
        raise HTTPException(status_code=409, detail="Заказ отменён — оплатить его нельзя")
    allowed = payments.providers_for(user.id)
    chosen = provider or order.pay_provider
    if chosen not in allowed:
        chosen = None if provider else (allowed[0] if allowed else None)
    if chosen is None:
        raise HTTPException(status_code=409, detail="Оплата картой сейчас недоступна")
    if order.pay_provider != chosen:
        order.pay_provider = chosen
        db.commit()
    if chosen == "paynet":
        # страницы оплаты у Paynet нет: покупатель платит в самом Paynet по
        # номеру заказа. Ссылка — если Paynet даст адрес оплаты сервиса
        from app import paynet
        digits = order.number.removeprefix(paynet.ORDER_PREFIX)
        link = (payments.PAYNET_PAY_URL.format(service=payments.PAYNET_SERVICE_ID, order=digits,
                                               amount=order.total)
                if payments.PAYNET_PAY_URL else None)
        return {"kind": "paynet", "link": link, "order": digits, "amount": order.total}
    if chosen == "click":
        from app import click
        return {"link": click.pay_url(order), "kind": "click"}
    if chosen == "payme":
        from app import payme
        from app import i18n
        return {"link": payme.checkout_url(order, i18n.lang_of(x_lang)), "kind": "payme"}
    try:
        return {"link": payments.create_invoice_link(order), "kind": "telegram"}
    except payments.PaymentError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/payment-options")
def payment_options(user: TelegramUser | None = Depends(current_user)):
    """Каким способом этому человеку можно платить. Витрина по этому ответу
    решает, показывать ли «Онлайн картой»: кнопка, которая ничего не делает,
    хуже её отсутствия."""
    allowed = payments.providers_for(user.id if user else None)
    return {
        "card": bool(allowed),
        # способы онлайн-оплаты, которые показать этому человеку, по порядку
        "providers": allowed,
        "provider": allowed[0] if allowed else payments.PROVIDER,
        "test": any(payments.is_test(p) for p in allowed),
        "unpaid_minutes": payments.UNPAID_MINUTES,
    }


@router.get("/my-orders", response_model=list[OrderOut])
def my_orders(
    response: Response,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_user),
):
    """Заказы этого покупателя, свежие сверху."""
    # ответ зависит от заголовка с подписью, промежуточные прокси его не учтут
    response.headers["Cache-Control"] = "no-store"
    orders = db.scalars(
        select(Order)
        .where(Order.telegram_id == user.id)
        .order_by(Order.id.desc())
        .limit(20)
        .options(ITEMS_WITH_PHOTO)
    )
    return [to_out(order) for order in orders]


@router.get("/delivery-price")
def get_delivery_price(response: Response, db: Session = Depends(get_db)):
    """Стоимость доставки, чтобы клиент показывал итог до оформления.

    Без кэша: цену меняют в панели, и старая в корзине дала бы отказ
    «цены изменились» при оформлении."""
    response.headers["Cache-Control"] = "no-store"
    return {"delivery_price": delivery_price(db)}
