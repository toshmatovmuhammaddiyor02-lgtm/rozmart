"""Административная панель: товары и заказы (экраны SA-01 и SA-04).

Вход — тот же, что у покупателя: подпись Telegram. Отдельных паролей нет
намеренно, их пришлось бы хранить, восстанавливать и отзывать. Админом
человека делает попадание его Telegram-id в ADMIN_TELEGRAM_IDS.

Главная задача панели — разобрать то, что приехало из REGOS. Учётная система
не знает про витрину: в ней каждая фасовка это отдельная позиция с складским
названием вроде «RZ VAR. EKSTRA 400GR». Человек собирает из таких позиций
витринную карточку: даёт название, фото, категорию и сводит фасовки вместе.
"""
import logging
import os
import re
import shutil
from datetime import timezone
from pathlib import Path

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, Request,
    Response, UploadFile,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app import admin_auth, notify, order_status, payments, photos, telegram
from app.db import get_db
from app.models import (AdminUser, Category, Notification, Order, Product,
                        StaffChat, Variant)
from app.telegram import TelegramUser, current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["Администрирование"])

# один источник правды о том, где лежат картинки — см. app/photos.py
STATIC_IMG = photos.DIR

# Кто допущен в панель. Пусто — панель закрыта для всех: пустой список прав
# безопаснее, чем случайно открытая настройка.
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_TELEGRAM_IDS", "").replace(" ", "").split(",") if x
}



# Состояния и переходы — из общего модуля: витрина показывает покупателю
# те же названия, и разойтись они больше не могут.
ORDER_STATUSES = order_status.TEXT
ALLOWED_TRANSITIONS = order_status.TRANSITIONS

MAX_PHOTO_BYTES = 4 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def require_admin(
    request: Request,
    user: TelegramUser | None = Depends(current_user),
    account: AdminUser | None = Depends(admin_auth.session_user),
) -> TelegramUser:
    """Пускает двумя путями: по паролю и по подписи Telegram.

    Вход по паролю нужен, чтобы панель открывалась в обычном браузере
    с компьютера. Вход через Telegram оставлен: администратору с телефона
    заново вводить пароль незачем, подпись там уже есть.

    В режиме разработки (TELEGRAM_AUTH_DISABLED=1) панель открыта: на своей
    машине ни Telegram, ни заведённых учёток нет. Переменная задаётся явно,
    а без неё приложение вообще откажется стартовать без токена бота, так что
    случайно открытой на сервере эта дверь не окажется.

    Роль. Вошедший по паролю видит только разделы своей роли
    (admin_auth.ROLES): адрес запроса относится к разделу, и чужой раздел —
    403. Вошедшие через Telegram из ADMIN_TELEGRAM_IDS — администраторы.
    """
    if account is not None:
        if not admin_auth.allowed(account.role, request.method, request.url.path):
            raise HTTPException(status_code=403, detail="Этот раздел вашей роли недоступен")
        return TelegramUser(id=0, name=account.full_name or account.username)
    if telegram.AUTH_DISABLED:
        return user or TelegramUser(id=0, name="разработка")
    if user is not None and user.id in ADMIN_IDS:
        return user
    # 401, а не 404: панель открыта в браузере, и клиенту надо показать форму
    # входа, а не делать вид, что страницы не существует
    raise HTTPException(status_code=401, detail="Войдите в панель")


class LoginIn(BaseModel):
    username: str = Field(min_length=2, max_length=60)
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def do_login(
    data: LoginIn,
    response: Response,
    db: Session = Depends(get_db),
):
    """Вход по логину и паролю. Заводит сессию в куке."""
    user = admin_auth.login(db, response, data.username, data.password)
    return {"ok": True, "username": user.username, "name": user.full_name}


@router.post("/logout")
def do_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    admin_auth.logout(db, request, response)
    return {"ok": True}


@router.get("/whoami")
def whoami(
    user: TelegramUser | None = Depends(current_user),
    account: AdminUser | None = Depends(admin_auth.session_user),
):
    """Кто открыл панель. Нужен, чтобы человека вообще можно было в неё впустить:
    свой Telegram-id иначе взять неоткуда, а без него список администраторов
    не заполнить. Ничего чужого метод не сообщает — только ваш собственный id.
    """
    if account is not None:
        role = account.role if account.role in admin_auth.ROLES else ""
        return {"authorized": True, "admin": True, "id": None,
                "name": account.full_name or account.username, "via": "password",
                "username": account.username, "role": role,
                "role_title": admin_auth.ROLES.get(role, {}).get("title", "нет роли"),
                "sections": admin_auth.sections_of(role),
                "min_password": admin_auth.MIN_PASSWORD}
    if user is None:
        return {"authorized": False, "admin": telegram.AUTH_DISABLED, "id": None,
                "hint": "Войдите по логину или откройте панель из Telegram",
                "sections": list(admin_auth.SECTIONS) if telegram.AUTH_DISABLED else []}
    admin = user.id in ADMIN_IDS or telegram.AUTH_DISABLED
    return {"authorized": True, "admin": admin,
            "id": user.id, "name": user.name, "via": "telegram",
            "role": "admin" if admin else "", "role_title": "Администратор" if admin else "",
            "sections": list(admin_auth.SECTIONS) if admin else []}


# ---------------------------------------------------------------- модели ответа


class VariantRow(BaseModel):
    id: int
    external_code: str        # код REGOS, служебный — виден только администратору
    weight: str
    price: int | None             # цена на витрине
    regos_price: int | None = None    # цена из REGOS по выбранному виду цены
    manual_price: int | None = None   # своя цена, если задана
    barcode: str | None
    is_active: bool
    # реквизиты фискального чека Payme и Click: обычно приходят из REGOS,
    # но их можно вписать и здесь, пока бухгалтерия не заполнила в REGOS
    mxik: str | None = None
    package_code: str | None = None
    vat_percent: int | None = None


class ProductRow(BaseModel):
    id: int
    name: str
    category_id: int
    category: str
    photo: str | None
    is_active: bool
    variant_count: int
    min_price: int | None
    sellable: bool            # есть ли хоть одна фасовка с ценой


class ProductDetail(ProductRow):
    description: str | None
    composition: str | None
    shelf_life: str | None
    variants: list[VariantRow]


class ProductPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    category_id: int | None = None
    description: str | None = Field(default=None, max_length=2000)
    composition: str | None = Field(default=None, max_length=2000)
    shelf_life: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None


class OrderRow(BaseModel):
    id: int
    regos_document_id: int | None = None
    number: str
    status: str
    status_text: str
    created_at: str
    customer_name: str
    phone: str
    total: int
    item_count: int


class OrderDetail(OrderRow):
    regos_document_id: int | None = None
    regos_error: str | None = None
    regos_attempts: int = 0
    # подключён ли покупатель к информационному боту. Оператору важно знать:
    # если нет, о смене состояния он узнает, только открыв приложение
    # @имя в Telegram, если оно у покупателя есть: по нему оператор напишет,
    # когда человек не берёт трубку
    telegram_username: str | None = None
    # точка на карте, если покупатель её поставил при оформлении
    lat: float | None = None
    lon: float | None = None
    # оплата картой: когда, сколько и под каким номером платёж у Telegram и Click
    paid_at: str | None = None
    paid_amount: int | None = None
    payment_charge_id: str | None = None
    provider_charge_id: str | None = None
    # ждёт оплаты картой — подтвердить его вручную нельзя
    awaiting_payment: bool = False
    # покупатель прямо сейчас в окне оплаты: отменить можно, но деньги
    # могут успеть списаться — тогда нужен возврат
    checkout_open: bool = False
    # доставщик из бота доставщиков: кто взял, когда, когда доставил
    courier_name: str | None = None
    courier_taken_at: str | None = None
    delivered_at: str | None = None
    # последний отказ доставщика и его причина
    courier_drop_note: str | None = None
    courier_drop_by: str | None = None
    # через какую систему платили и ссылка на её фискальный чек: по ней
    # панель рисует QR и печатает чек для пакета с заказом
    pay_provider: str | None = None
    fiscal_qr_url: str | None = None
    address: str
    delivery_slot: str
    comment: str | None
    payment_method: str
    goods_total: int
    delivery_price: int
    items: list[dict]


class StatusPatch(BaseModel):
    status: str


# ---------------------------------------------------------------- SA-01 товары


def _min_price(product: Product) -> int | None:
    prices = [v.price for v in product.variants if v.price is not None]
    return min(prices) if prices else None


def _row(product: Product) -> ProductRow:
    price = _min_price(product)
    return ProductRow(
        id=product.id,
        name=product.name,
        category_id=product.category_id,
        category=product.category.name,
        photo=photos.url(product.photo),
        is_active=product.is_active,
        variant_count=len(product.variants),
        min_price=price,
        sellable=price is not None,
    )


@router.get("/products", response_model=list[ProductRow])
def list_products(
    status: str = Query("new", pattern="^(new|published|ready|all)$"),
    q: str | None = None,
    category_id: int | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Товары для разбора.

    status:
      new       — не опубликованные, то есть приехавшие из REGOS и ещё не разобранные
      ready     — не опубликованные, но с ценой: их можно публиковать хоть сейчас
      published — уже на витрине
      all       — всё подряд
    """
    stmt = select(Product).options(
        selectinload(Product.variants), selectinload(Product.category)
    )
    if status == "published":
        stmt = stmt.where(Product.is_active)
    elif status in ("new", "ready"):
        stmt = stmt.where(~Product.is_active)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if q:
        # ищем и по витринному названию, и по коду REGOS: администратор чаще
        # приходит из учётной системы с кодом на руках
        like = f"%{q.strip()}%"
        codes = select(Variant.product_id).where(Variant.external_code.ilike(like))
        stmt = stmt.where(or_(Product.name.ilike(like), Product.id.in_(codes)))

    rows = [_row(p) for p in db.scalars(stmt.order_by(Product.id))]
    if status == "ready":
        rows = [r for r in rows if r.sellable]
    return rows[offset:offset + limit]


@router.get("/products/count")
def products_count(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Счётчики для вкладок — считаются в базе, а не выкачиванием всех строк."""
    total = db.scalar(select(func.count(Product.id)))
    published = db.scalar(select(func.count(Product.id)).where(Product.is_active))
    return {"total": total, "published": published, "hidden": total - published}


# когда последний раз спрашивали REGOS о кодах фасовки: id -> time.time()
_fiscal_asked: dict[int, float] = {}
FISCAL_ASK_EVERY = 600


def refresh_fiscal(db: Session, variants) -> None:
    """Недостающие ИКПУ, код упаковки и НДС — из REGOS, при открытии карточки.

    Синхронизация берёт коды только у выбранных групп и у витрины, а карточку
    открывают и до публикации — из «Новых из REGOS». Спрашиваем одним запросом
    по кодам фасовок и не чаще раза в FISCAL_ASK_EVERY: кода упаковки в REGOS
    часто нет вовсе, и дёргать REGOS при каждом открытии незачем.
    REGOS недоступен — карточка открывается как есть.
    """
    import time

    from app.regos import catalog_sync, config as regos_config, prices
    from app.regos.client import RegosClient

    now = time.time()
    missing = [v for v in variants
               if (not v.mxik or not v.package_code or v.vat_percent is None)
               and now - _fiscal_asked.get(v.id, 0) > FISCAL_ASK_EVERY]
    if not missing or not regos_config.is_configured():
        return
    for v in missing:
        _fiscal_asked[v.id] = now
    try:
        catalog_sync.fill_fiscal(db, RegosClient(), missing, prices.current(db))
        db.commit()
    except Exception as exc:                # noqa: BLE001
        db.rollback()
        log.warning("Коды для чека из REGOS не получены: %s", exc)


@router.get("/products/{product_id}", response_model=ProductDetail)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    refresh_fiscal(db, product.variants)
    base = _row(product).model_dump()
    return ProductDetail(
        **base,
        description=product.description,
        composition=product.composition,
        shelf_life=product.shelf_life,
        variants=[
            VariantRow(
                id=v.id, external_code=v.external_code, weight=v.weight,
                price=v.price, regos_price=v.regos_price, manual_price=v.manual_price,
                barcode=v.barcode, is_active=v.is_active,
                mxik=v.mxik, package_code=v.package_code, vat_percent=v.vat_percent,
            )
            for v in sorted(product.variants, key=lambda v: v.weight_grams)
        ],
    )


@router.patch("/products/{product_id}", response_model=ProductDetail)
def edit_product(
    product_id: int,
    data: ProductPatch,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Правка витринной карточки.

    Цены и остатки здесь не меняются намеренно: они приезжают из REGOS и любая
    ручная правка будет затёрта ближайшей синхронизацией.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")

    fields = data.model_dump(exclude_unset=True)
    if (category_id := fields.get("category_id")) is not None:
        if db.get(Category, category_id) is None:
            raise HTTPException(status_code=400, detail="Категория не найдена")

    if fields.get("is_active"):
        # BR-34 и BR-37: публикуем только то, что покупатель сможет купить
        if _min_price(product) is None:
            raise HTTPException(
                status_code=400,
                detail="Нельзя опубликовать: ни у одной фасовки нет цены из REGOS",
            )
        if not (fields.get("name") or product.name).strip():
            raise HTTPException(status_code=400, detail="Нельзя опубликовать без названия")

    for key, value in fields.items():
        setattr(product, key, value)
    db.commit()
    db.refresh(product)
    return get_product(product_id, db)


@router.post("/products/{product_id}/photo", response_model=ProductDetail)
def upload_photo(
    product_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Фото карточки.

    Файл называется по id товара, а не по коду REGOS: код — служебные данные
    учётной системы, и в публичном адресе картинки ему делать нечего (BR-36).
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")

    suffix = PHOTO_TYPES.get(file.content_type or "")
    if suffix is None:
        raise HTTPException(status_code=400, detail="Нужен JPEG, PNG или WebP")

    STATIC_IMG.mkdir(parents=True, exist_ok=True)
    previous = product.photo
    target = STATIC_IMG / f"{product_id}{suffix}"
    size = 0
    with target.open("wb") as out:
        while chunk := file.file.read(64 * 1024):
            size += len(chunk)
            if size > MAX_PHOTO_BYTES:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=400, detail="Файл больше 4 МБ")
            out.write(chunk)

    product.photo = target.name
    db.commit()
    # прежний файл удаляем, только если он другого формата: иначе имя совпадает
    # и мы бы стёрли только что записанную картинку
    if previous and previous != target.name:
        photos.remove(previous)
    return get_product(product_id, db)


@router.delete("/products/{product_id}/photo", response_model=ProductDetail)
def delete_photo(
    product_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Убирает фотографию с карточки.

    Товар при этом с витрины не снимается: карточка без картинки выглядит хуже,
    но продаётся. Решение публиковать её или нет остаётся за администратором.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Товар не найден")
    previous = product.photo
    product.photo = None
    db.commit()
    photos.remove(previous)
    return get_product(product_id, db)


@router.post("/variants/{variant_id}/move", response_model=ProductDetail)
def move_variant(
    variant_id: int,
    target_product_id: int = Query(..., description="куда переносим фасовку"),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Переносит фасовку в другую карточку.

    Так из нескольких позиций REGOS собирается одна витринная карточка:
    «Экстра варёная» — это четыре позиции по 400, 455, 700 и 855 грамм.
    Опустевшая карточка удаляется, иначе в списке копятся пустышки.
    """
    variant = db.get(Variant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="Фасовка не найдена")
    target = db.get(Product, target_product_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Карточка-получатель не найдена")
    if variant.product_id == target_product_id:
        raise HTTPException(status_code=400, detail="Фасовка уже в этой карточке")

    source = db.get(Product, variant.product_id)
    variant.product_id = target_product_id
    db.flush()

    remaining = db.scalar(
        select(func.count(Variant.id)).where(Variant.product_id == source.id)
    )
    if remaining == 0:
        db.delete(source)
    db.commit()
    return get_product(target_product_id, db)


# ------------------------------------------------------------- категории


@router.get("/categories")
def list_categories(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    counts = dict(
        db.execute(
            select(Product.category_id, func.count(Product.id)).group_by(Product.category_id)
        ).all()
    )
    return [
        {"id": c.id, "name": c.name, "sort_order": c.sort_order,
         "product_count": counts.get(c.id, 0)}
        for c in db.scalars(select(Category).order_by(Category.sort_order, Category.id))
    ]


# ---------------------------------------------------------------- SA-04 заказы


def _order_row(order: Order) -> dict:
    return {
        "id": order.id,
        "number": order.number,
        "status": order.status,
        "status_text": ORDER_STATUSES.get(order.status, order.status),
        # пометка пояса обязательна: строку без неё JavaScript разбирает как
        # местное время, и заказ показывается со сдвигом на часовой пояс
        "created_at": order.created_at.replace(tzinfo=timezone.utc).isoformat(),
        "customer_name": order.customer_name,
        "phone": order.phone,
        "total": order.total,
        "item_count": len(order.items),
        "regos_document_id": order.regos_document_id,
    }


@router.get("/orders", response_model=list[OrderRow])
def list_orders(
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Заказы, свежие сверху. Поиск по номеру, имени и телефону."""
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id.desc())
    if status:
        stmt = stmt.where(Order.status == status)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Order.number.ilike(like),
            Order.customer_name.ilike(like),
            Order.phone.ilike(like),
        ))
    return [OrderRow(**_order_row(o)) for o in db.scalars(stmt.limit(limit).offset(offset))]


@router.get("/orders/{order_id}", response_model=OrderDetail)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return OrderDetail(
        **_order_row(order),
        telegram_username=order.telegram_username,
        lat=order.lat,
        lon=order.lon,
        paid_at=order.paid_at.replace(tzinfo=timezone.utc).isoformat() if order.paid_at else None,
        paid_amount=order.paid_amount,
        payment_charge_id=order.payment_charge_id,
        provider_charge_id=order.provider_charge_id,
        awaiting_payment=payments.awaiting_payment(order),
        checkout_open=payments.checkout_open(order),
        courier_name=order.courier_name,
        courier_taken_at=(order.courier_taken_at.replace(tzinfo=timezone.utc).isoformat()
                          if order.courier_taken_at else None),
        delivered_at=(order.delivered_at.replace(tzinfo=timezone.utc).isoformat()
                      if order.delivered_at else None),
        courier_drop_note=order.courier_drop_note,
        courier_drop_by=order.courier_drop_by,
        regos_error=order.regos_error,
        regos_attempts=order.regos_attempts,
        pay_provider=order.pay_provider,
        fiscal_qr_url=order.fiscal_qr_url,
        address=order.address,
        delivery_slot=order.delivery_slot,
        comment=order.comment,
        payment_method=order.payment_method,
        goods_total=order.goods_total,
        delivery_price=order.delivery_price,
        items=[
            {"product_name": i.product_name, "weight": i.weight,
             "price": i.price, "quantity": i.quantity, "sum": i.price * i.quantity}
            for i in order.items
        ],
    )


@router.patch("/orders/{order_id}/status", response_model=OrderDetail)
def set_order_status(
    order_id: int,
    data: StatusPatch,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Перевод заказа в следующее состояние.

    Переходы ограничены: без этого выполненный заказ случайным нажатием
    вернётся в сборку, и отчёты по выручке разойдутся с фактом.
    """
    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if data.status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="Неизвестное состояние")
    # Неоплаченный заказ с оплатой картой подтверждать нельзя: подтверждённый
    # уходит кассиру, и тот отдаст товар, за который никто не платил.
    # Подтверждает такой заказ только уведомление об оплате от Telegram
    if data.status == "CONFIRMED" and payments.awaiting_payment(order):
        raise HTTPException(
            status_code=409,
            detail="Заказ не оплачен картой. Он подтвердится сам, когда придёт оплата",
        )
    allowed = ALLOWED_TRANSITIONS.get(order.status, set())
    if data.status not in allowed:
        current = ORDER_STATUSES.get(order.status, order.status)
        raise HTTPException(
            status_code=409,
            detail=f"Из состояния «{current}» так перейти нельзя",
        )
    if not order_status.move(db, order, data.status):
        # пока оператор смотрел на карточку, заказ поменял кто-то другой:
        # касса, оплата или автоотмена. Вслепую поверх писать нельзя
        db.rollback()
        raise HTTPException(status_code=409,
                            detail="Заказ только что изменился. Обновите карточку")
    db.commit()
    db.refresh(order)
    if order.status == "CANCELED":
        # сотрудникам об отмене сообщаем отдельно: заказ мог быть уже в сборке
        staff = notify.queue_staff(db, order,
                                   lambda lang: notify.staff_canceled(order, "в панели", lang))
        if staff:
            db.commit()
            background.add_task(notify.send_many, [row.id for row in staff])
        # отменённый заказ надо снять и с кассы, иначе его там соберут и выдадут.
        # Тоже фоном: недоступный REGOS не повод отказывать оператору в отмене
        if order.regos_document_id:
            from app.regos.orders_push import cancel_one
            background.add_task(cancel_one, order_id)
        # заказ мог быть уже у доставщика в пути — пусть не везёт
        from app import courier_bot
        background.add_task(courier_bot.order_canceled, order)
    return get_order(order_id, db)


@router.post("/orders/{order_id}/push")
def push_order_to_regos(
    order_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Отправить заказ в REGOS вручную.

    Нужно, когда автоматическая выгрузка исчерпала попытки или оператор
    исправил причину отказа — например, дал товару код REGOS.
    """
    from app.regos import config as regos_config
    from app.regos.client import RegosClient, RegosError
    from app.regos.orders_push import (
        PUSHABLE, ConfirmError, OrderPushError, item_codes, operating_cash, push_order,
    )

    # та же блокировка строки, что у автоотправки: кнопка оператора и фоновая
    # выгрузка одного заказа не должны завести два документа у кассира
    order = db.get(Order, order_id, with_for_update=True)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.regos_document_id:
        return {"ok": True, "document_id": order.regos_document_id, "already": True}
    if order.status == "CANCELED":
        raise HTTPException(status_code=400, detail="Отменённый заказ в REGOS не передаётся")
    # Выгружается только то же, что и автоматически: PUSHABLE. Без этой проверки
    # неоплаченный заказ картой уходил кассиру по кнопке, синхронизация через
    # минуту переводила его в «Принят», и запрет ручного подтверждения
    # обходился одним нажатием
    if payments.awaiting_payment(order):
        raise HTTPException(status_code=409,
                            detail="Заказ не оплачен картой — в REGOS он уйдёт сам после оплаты")
    if order.status not in PUSHABLE:
        raise HTTPException(status_code=409, detail="Этот заказ ещё не подтверждён")


    order.regos_attempts += 1
    try:
        order.regos_document_id = push_order(
            RegosClient(), order, item_codes(db, [order]), operating_cash(db),
            regos_config.stock_id(db),
        )
        order.regos_error = None
        db.commit()
    except ConfirmError as exc:
        # заказ в REGOS уже есть: запоминаем документ, иначе повтор заведёт дубль
        order.regos_document_id = exc.document_id
        order.regos_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    except (OrderPushError, RegosError, RuntimeError, OSError) as exc:
        order.regos_error = str(exc)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "document_id": order.regos_document_id}


@router.delete("/orders/{order_id}")
def delete_order(
    order_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Удаляет заказ из витрины и снимает его в REGOS.

    Удалить документ в REGOS через API нельзя — метод есть, но отвечает
    ошибкой на любом документе (см. cancel_document). Поэтому там заказ
    отменяется: для кассира это то же самое — по отменённому он ничего
    не отгружает.

    Если отменить не удалось, заказ не удаляем и здесь. Иначе на кассе остался
    бы живой документ, о котором в витрине уже нечего вспомнить.

    Удаление необратимо: заказ уходит из истории вместе с составом и суммой.
    Это операция для тестовых и ошибочных заказов, а не способ убрать
    неудобную выручку из отчётов.
    """
    from app.regos.client import RegosClient, RegosError
    from app.regos.orders_push import cancel_document

    order = db.get(Order, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    if order.paid_at is not None:
        # удаление стёрло бы номер платежа, сумму и время — и возвращать деньги
        # стало бы не по чему. Оплаченный отменяют, а не удаляют
        raise HTTPException(status_code=409,
                            detail="Оплаченный заказ удалить нельзя — его можно только отменить")

    regos = "в REGOS не выгружался"
    if order.regos_document_id:
        try:
            done = cancel_document(RegosClient(), order.regos_document_id)
        except (RegosError, RuntimeError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Заказ не удалён: в REGOS его снять не удалось. {exc}",
            )
        regos = {
            "canceled": f"документ № {order.regos_document_id} отменён в REGOS",
            "already": f"документ № {order.regos_document_id} в REGOS уже был отменён",
            "missing": f"документа № {order.regos_document_id} в REGOS уже нет",
        }[done]

    number = order.number
    # неотправленные сообщения об этом заказе больше не нужны: покупателю
    # незачем получать «собираем ваш заказ» о том, чего уже нет
    db.query(Notification).filter(Notification.order_id == order_id).delete()
    db.delete(order)
    db.commit()
    log.info("Заказ %s удалён: %s", number, regos)
    return {"deleted": number, "regos": regos}


# --------------------------------------------- кому приходят заказы в Telegram


@router.get("/notify/chats")
def notify_chats(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Чаты, которые знает информационный бот, и у кого из них есть доступ.

    В списке два сорта записей: личные чаты тех, кто писал боту, и группы,
    куда бота добавили. Доступ к заказам — это флаг, а не факт знакомства:
    заказ с телефоном и адресом покупателя уходит только туда, где его
    включили здесь.
    """
    rows = [{
        "chat_id": chat.chat_id,
        "title": chat.title or str(chat.chat_id),
        "username": chat.username,
        "kind": chat.kind,
        "access": chat.active,
        "language": chat.language if chat.language in notify.LANGS else "ru",
    } for chat in db.scalars(select(StaffChat))]
    rows.sort(key=lambda r: (not r["access"], r["kind"] != "group", r["title"].lower()))
    return {"enabled": notify.enabled(), "bot": notify.bot_username(), "chats": rows}


@router.post("/notify/chats/{chat_id}")
def grant_notify_access(
    chat_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Включает чату получение всех заказов."""
    chat = db.get(StaffChat, chat_id)
    if chat is None:
        raise HTTPException(
            status_code=404,
            detail="Бот не знает этот чат. Попросите написать боту или добавить его в группу.",
        )
    chat.active = True
    db.commit()
    log.info("Чату %s (%s) выдан доступ к заказам", chat_id, chat.title)
    return {"chat_id": chat_id, "access": True}


class ChatLanguageIn(BaseModel):
    language: str = Field(pattern="^(ru|uz)$")


@router.patch("/notify/chats/{chat_id}/language")
def set_chat_language(
    chat_id: int,
    data: ChatLanguageIn,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Язык сообщений о заказах в этом чате. Для группы — единственный
    способ его сменить: пишут в неё люди с разными языками Telegram."""
    chat = db.get(StaffChat, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Такого чата нет в списке")
    chat.language = data.language
    db.commit()
    log.info("Чат %s (%s): язык сообщений %s", chat_id, chat.title, data.language)
    return {"chat_id": chat_id, "language": chat.language}


@router.delete("/notify/chats/{chat_id}")
def revoke_notify_access(
    chat_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Отключает чат от заказов. Запись не удаляем — чтобы можно было включить
    обратно, не прося человека снова писать боту."""
    chat = db.get(StaffChat, chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Такого чата нет в списке")
    chat.active = False
    db.commit()
    log.info("Чат %s (%s) отключён от заказов", chat_id, chat.title)
    return {"chat_id": chat_id, "access": False}


# --------------------------------------------------------------- доставщики


@router.get("/couriers")
def couriers(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Кто писал боту доставщиков и у кого есть доступ к заказам."""
    from app import courier_bot
    from app.models import Courier

    busy = dict(db.execute(
        select(Order.courier_id, func.count()).where(
            Order.courier_id.is_not(None), Order.status == "DELIVERING")
        .group_by(Order.courier_id)).all())
    rows = [{
        "telegram_id": c.telegram_id, "name": c.name, "username": c.username,
        "access": c.active, "in_delivery": busy.get(c.telegram_id, 0),
    } for c in db.scalars(select(Courier))]
    rows.sort(key=lambda r: (not r["access"], r["name"].lower()))
    bot = ""
    if courier_bot.enabled():
        try:
            bot = courier_bot.call("getMe", {}).get("username", "")
        except notify.NotifyError:
            bot = ""
    return {"enabled": courier_bot.enabled(), "bot": bot, "couriers": rows}


@router.post("/couriers/{telegram_id}")
def grant_courier(telegram_id: int, db: Session = Depends(get_db),
                  _: TelegramUser = Depends(require_admin)):
    """Доставщик начинает видеть заказы — с телефоном и адресом покупателя."""
    from app import courier_bot
    from app.models import Courier

    courier = db.get(Courier, telegram_id)
    if courier is None:
        raise HTTPException(status_code=404,
                            detail="Бот не знает этого человека. Попросите написать боту.")
    courier.active = True
    db.commit()
    log.info("Доставщику %s (%s) выдан доступ", telegram_id, courier.name)
    courier_bot.access_granted(courier)
    return {"telegram_id": telegram_id, "access": True}


@router.delete("/couriers/{telegram_id}")
def revoke_courier(telegram_id: int, background: BackgroundTasks, db: Session = Depends(get_db),
                   _: TelegramUser = Depends(require_admin)):
    """Отключает доставщика. Его заказы в пути становятся свободными:
    иначе они висели бы «в доставке» ни у кого."""
    from app import courier_bot
    from app.models import Courier

    courier = db.get(Courier, telegram_id)
    if courier is None:
        raise HTTPException(status_code=404, detail="Такого доставщика нет в списке")
    courier.active = False
    db.commit()
    released = courier_bot.release_all(db, telegram_id)
    for number in released:
        order = db.scalar(select(Order).where(Order.number == number))
        courier_bot.staff(db, background, order, lambda lang, number=number: notify.st(
            lang, "courier_off", courier=notify.esc(courier.name), number=notify.esc(number)))
    log.info("Доставщик %s отключён, освобождены заказы: %s", telegram_id, released)
    return {"telegram_id": telegram_id, "access": False, "released": released}


@router.post("/regos/photos")
def pull_regos_photos(
    apply: bool = Query(False, description="false — только посчитать"),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Подтягивает фотографии товаров из REGOS.

    Только для карточек, у которых своего фото нет: снятое и обрезанное
    вручную лучше того, что лежит в учётной системе, и затирать его нельзя.

    Кнопкой, а не при синхронизации каталога: это десятки скачиваний,
    а картинки в REGOS меняются раз в год.
    """
    from app.regos import config
    from app.regos.client import RegosError
    from app.regos.photos_sync import run

    if not config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    try:
        return run(db, apply=apply)
    except (RegosError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# ------------------------------------------------------------------ цены


class PricePatch(BaseModel):
    # None — вернуть цену REGOS. Границы — защита от опечатки лишним нулём
    price: int | None = Field(default=None, ge=1, le=10_000_000)


@router.patch("/variants/{variant_id}/price", response_model=ProductDetail)
def set_variant_price(
    variant_id: int,
    data: PricePatch,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Своя цена фасовки на витрине.

    Синхронизация с REGOS её не трогает: обновляет только цену REGOS рядом.
    Уже оформленные заказы не меняются — цена в них зафиксирована (BR-09).
    """
    from app.regos import prices

    variant = db.get(Variant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="Фасовка не найдена")
    prices.set_manual(variant, data.price)
    db.commit()
    log.info("Фасовка %s: своя цена %s (REGOS %s)",
             variant.external_code, data.price, variant.regos_price)
    return get_product(variant.product_id, db)


class FiscalPatch(BaseModel):
    mxik: str = Field(default="", max_length=40)
    package_code: str = Field(default="", max_length=40)


@router.patch("/variants/{variant_id}/fiscal", response_model=ProductDetail)
def set_variant_fiscal(
    variant_id: int,
    data: FiscalPatch,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """ИКПУ и код упаковки фасовки — для фискального чека Payme и Click.

    Синхронизация с REGOS пустым не затирает (catalog_sync.apply_fiscal),
    а заполненное в REGOS ставит вместо вписанного здесь: главный источник —
    REGOS, панель — пока там пусто.
    """
    variant = db.get(Variant, variant_id)
    if variant is None:
        raise HTTPException(status_code=404, detail="Фасовка не найдена")
    mxik = re.sub(r"\s+", "", data.mxik or "")
    package = re.sub(r"\s+", "", data.package_code or "")
    if mxik and not re.fullmatch(r"[0-9]{17}", mxik):
        raise HTTPException(status_code=400, detail="ИКПУ — 17 цифр")
    if package and not re.fullmatch(r"[0-9]{1,20}", package):
        raise HTTPException(status_code=400, detail="Код упаковки — только цифры, до 20")
    variant.mxik = mxik or None
    variant.package_code = package or None
    db.commit()
    log.info("Фасовка %s: ИКПУ %s, код упаковки %s", variant.external_code,
             variant.mxik or "—", variant.package_code or "—")
    return get_product(variant.product_id, db)


# ------------------------------------------------------- онлайн-оплата: вкл/выкл

PROVIDER_TITLES = {"payme": "Payme", "paynet": "Paynet", "click": "Click", "telegram": "Telegram"}
SHOWN_FIRST = ("payme", "paynet", "click")


def _payment_switches() -> dict:
    off = payments.switched_off()
    shown = [p for p in SHOWN_FIRST if p in payments.PROVIDERS] + \
        [p for p in payments.PROVIDERS if p not in SHOWN_FIRST]
    return {"providers": [{
        "provider": p,
        "title": PROVIDER_TITLES.get(p, p),
        "on": p not in off,
        # ключи заданы — способ может работать; нет — покупатель видит «Скоро»
        "configured": payments.configured(p),
        "test": payments.is_test(p),
        "testers_only": payments.is_test(p) or payments.pilot(p),
    } for p in shown]}


@router.get("/payments")
def payment_switches(request: Request, _: TelegramUser = Depends(require_admin)):
    """Способы онлайн-оплаты, их переключатели и ключи (секреты — хвостом)."""
    base = os.getenv("PUBLIC_URL", "").strip().rstrip("/") or str(request.base_url).rstrip("/")
    return {**_payment_switches(), "config": payments.config_view(),
            "endpoints": {"payme": base + "/payme", "paynet": base + "/paynet",
                          "click": [base + "/click/prepare", base + "/click/complete"]},
            "regos_payment_types": regos_payment_types()}


# формы оплаты REGOS для выбора в панели: справочник меняется редко
_regos_types = {"at": 0.0, "rows": []}


def regos_payment_types() -> list[dict]:
    """Формы оплаты из REGOS (PaymentType/Get), которые можно выбрать на кассе.
    REGOS недоступен — пустой список: панель покажет поле без выбора."""
    import time

    from app.regos import config as regos_config
    from app.regos.client import RegosClient

    if time.time() - _regos_types["at"] < 300 and _regos_types["rows"]:
        return _regos_types["rows"]
    if not regos_config.is_configured():
        return []
    try:
        rows = RegosClient().call("PaymentType/Get", {}) or []
    except Exception as exc:                # noqa: BLE001
        log.warning("Формы оплаты REGOS не получены: %s", exc)
        return _regos_types["rows"]
    if not isinstance(rows, list):
        rows = []
    _regos_types["rows"] = [
        {"id": r.get("id"), "name": r.get("name"), "cash": bool(r.get("is_cash"))}
        for r in rows if isinstance(r, dict)
        and str(r.get("enabled", "")).lower() in ("true", "frontoffice")]
    _regos_types["at"] = time.time()
    return _regos_types["rows"]


class PaymentConfigIn(BaseModel):
    # только то, что меняем; секрет пустой строкой не стирается — его не трогают
    values: dict[str, str]


@router.put("/payments/config")
def set_payment_config(data: PaymentConfigIn, request: Request, db: Session = Depends(get_db),
                       _: TelegramUser = Depends(require_admin)):
    """Ключи и режимы оплаты. Применяются за несколько секунд, без перезапуска."""
    values = {}
    for name, raw in data.values.items():
        if name not in payments.CONFIG_FIELDS:
            raise HTTPException(status_code=400, detail=f"Неизвестная настройка {name}")
        value = (raw or "").strip()
        if name in payments.MASKED and not value:
            continue                        # пустое поле секрета и ID кассы — «не менять»
        if name == "PAYME_MERCHANT_ID" and value and not re.fullmatch(r"[0-9a-f]{24}", value):
            raise HTTPException(status_code=400,
                                detail="ID кассы Payme — 24 символа: цифры и латинские a–f")
        if name == "PAYNET_SERVICE_ID" and value and not value.isdigit():
            raise HTTPException(status_code=400, detail="Номер сервиса Paynet — только цифры")
        if name in ("CLICK_SERVICE_ID", "CLICK_MERCHANT_ID", "CLICK_MERCHANT_USER_ID") \
                and value and not value.isdigit():
            raise HTTPException(status_code=400, detail="ID в Click — только цифры")
        if name.startswith("FEE_") and value:
            try:
                fee = float(value.replace(",", "."))
            except ValueError:
                fee = -1
            if not 0 <= fee <= 20:
                raise HTTPException(status_code=400, detail="Комиссия — число от 0 до 20, в процентах")
            value = str(fee).rstrip("0").rstrip(".") if "." in str(fee) else str(fee)
        if name.startswith("REGOS_PAY_") and value and not value.isdigit():
            raise HTTPException(status_code=400, detail="Форма оплаты REGOS — номер из справочника")
        if name == "CLICK_TIN" and value and not re.fullmatch(r"[0-9]{9}|[0-9]{14}", value):
            raise HTTPException(status_code=400, detail="ИНН — 9 цифр (или ПИНФЛ — 14)")
        if name == "CLICK_DELIVERY_SPIC" and value and not re.fullmatch(r"[0-9]{17}", value):
            raise HTTPException(status_code=400, detail="ИКПУ — 17 цифр")
        if name == "TEST_USERS" and value and not re.fullmatch(r"[0-9]+(\s*,\s*[0-9]+)*", value):
            raise HTTPException(status_code=400,
                                detail="Тестировщики — Telegram-id цифрами через запятую")
        if name in ("PAYME_TEST", "PAYNET_TEST", "TESTERS_ONLY", "CLICK_PILOT") and value not in ("0", "1"):
            raise HTTPException(status_code=400, detail="Режим — 0 или 1")
        if name in payments.SECRETS and len(value) < 8:
            raise HTTPException(status_code=400, detail="Ключ или пароль короче 8 символов")
        # Ключ кассы, похожий на ID кассы, — почти наверняка вставили не то
        # (однажды так и вышло: боевой Payme перестал к нам пускаться)
        if name == "PAYME_KEY" and (re.fullmatch(r"[0-9a-f]{24}", value)
                                     or value == payments.PAYME_MERCHANT_ID):
            raise HTTPException(status_code=400,
                                detail="Это похоже на ID кассы, а не на ключ. Ключ — из кабинета "
                                       "Payme Business, поле «Ключ» в настройках кассы")
        values[name] = value
    payments.set_config(db, values)
    # что поменяли — в журнал, без значений секретов
    log.info("Настройки оплаты изменены: %s", ", ".join(
        f"{k}=…" if k in payments.SECRETS else f"{k}={v}" for k, v in values.items()))
    return payment_switches(request, _)


@router.post("/payments/paynet-password")
def new_paynet_password(request: Request, db: Session = Depends(get_db),
                        _: TelegramUser = Depends(require_admin)):
    """Новый пароль для Paynet. Показывается один раз — его передают Paynet."""
    import secrets as pysecrets
    password = pysecrets.token_urlsafe(18)
    payments.set_config(db, {"PAYNET_PASSWORD": password})
    log.info("Пароль Paynet сгенерирован заново")
    return {"password": password, **payment_switches(request, _)}


# ------------------------------------------------------- онлайн-платежи: отчёт

@router.get("/online-payments")
def online_payments(date_from: str | None = None, date_to: str | None = None,
                    db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Онлайн-платежи за период (сутки по Ташкенту): платежи, итоги, предупреждения."""
    from app import payments_report as report
    day_to = report.parse_day(date_to, report.today())
    day_from = report.parse_day(date_from, day_to)
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    if (day_to - day_from).days > 366:
        raise HTTPException(status_code=400, detail="Период — не больше года")
    return report.view(db, day_from, day_to)


@router.get("/online-payments.xlsx")
def online_payments_xlsx(date_from: str | None = None, date_to: str | None = None,
                         db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """То же — файлом Excel."""
    from fastapi.responses import Response as FileResponse

    from app import payments_report as report
    day_to = report.parse_day(date_to, report.today())
    day_from = report.parse_day(date_from, day_to)
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    if (day_to - day_from).days > 366:
        raise HTTPException(status_code=400, detail="Период — не больше года")
    name = f"onlayn-platezhi_{day_from:%Y-%m-%d}_{day_to:%Y-%m-%d}.xlsx"
    return FileResponse(report.xlsx(db, day_from, day_to),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})


class PaymentSwitchIn(BaseModel):
    on: bool


@router.put("/payments/{provider}")
def set_payment_switch(provider: str, data: PaymentSwitchIn, db: Session = Depends(get_db),
                       _: TelegramUser = Depends(require_admin)):
    """Включить или выключить способ оплаты для покупателей. Начатые оплаты
    и возвраты сервер принимает в любом случае."""
    if provider not in payments.PROVIDERS:
        raise HTTPException(status_code=404, detail="Такой способ оплаты не подключён")
    payments.set_switch(db, provider, data.on)
    log.info("Онлайн-оплата %s: %s", provider, "включена" if data.on else "выключена")
    return _payment_switches()





class DeliveryPriceIn(BaseModel):
    # 0 — бесплатная доставка
    price: int = Field(ge=0, le=1_000_000)


@router.get("/delivery-price")
def admin_delivery_price(db: Session = Depends(get_db),
                         _: TelegramUser = Depends(require_admin)):
    from app import orders as orders_api
    return {"price": orders_api.delivery_price(db), "default": orders_api.DELIVERY_PRICE}


@router.put("/delivery-price")
def set_delivery_price(data: DeliveryPriceIn, db: Session = Depends(get_db),
                       _: TelegramUser = Depends(require_admin)):
    """Стоимость доставки для новых заказов.

    Оформленные заказы не меняются: сумма в них зафиксирована (BR-10).
    Покупатель, у которого корзина открыта со старой ценой, при оформлении
    получит «цены изменились» и увидит новый итог — молча другую сумму
    с него не возьмут.
    """
    from app import orders as orders_api
    before = orders_api.delivery_price(db)
    orders_api.save_delivery_price(db, data.price)
    db.commit()
    log.info("Стоимость доставки: %s -> %s", before, data.price)
    return {"price": data.price, "default": orders_api.DELIVERY_PRICE}


class PriceTypeIn(BaseModel):
    price_type_id: int = Field(ge=1)


@router.get("/regos/price-types")
def price_types(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Виды цен REGOS и какой из них сейчас на витрине."""
    from app.regos import config, prices
    from app.regos.client import RegosClient, RegosError

    if not config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    try:
        types = prices.fetch_types(RegosClient())
    except (RegosError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"current": prices.current(db), "types": types, "stock_id": config.stock_id(db)}


@router.post("/regos/price-type")
def change_price_type(
    data: PriceTypeIn,
    apply: bool = Query(False, description="false — только показать, что изменится"),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Переключить витрину на другой вид цены REGOS.

    Сначала — предпросмотр (apply=false): сколько товаров подорожает,
    подешевеет и сколько пропадёт с витрины, потому что в этом виде цены
    для них нет. Применение — apply=true.
    """
    from app.regos import config, prices
    from app.regos.client import RegosClient, RegosError

    if not config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    client = RegosClient()
    try:
        known = {t["id"] for t in prices.fetch_types(client)}
        if data.price_type_id not in known:
            raise HTTPException(status_code=400, detail="Такого вида цены в REGOS нет")
        if apply:
            return {"applied": True, **prices.switch(db, client, data.price_type_id)}
        return {"applied": False, **prices.compare(db, client, data.price_type_id)}
    except (RegosError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/statuses")
def statuses(_: TelegramUser = Depends(require_admin)):
    return {"statuses": ORDER_STATUSES, "transitions": {
        k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()}}


# ------------------------------------------------- состав каталога и категории


class GroupsPatch(BaseModel):
    group_ids: list[int]


class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    sort_order: int = 0


@router.get("/regos/groups")
def regos_groups(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Группы номенклатуры REGOS и сколько из каждой уже заведено в витрине.

    Список тянется из учётной системы: он там меняется, и держать его копию
    значило бы показывать администратору устаревшее. Счётчики — из своей базы,
    по ним видно, что именно приедет, если группу включить.
    """
    from app.regos import config as regos_config
    from app.regos.catalog_sync import selected_groups
    from app.regos.client import RegosClient, RegosError

    local = dict(
        db.execute(
            select(Variant.regos_group_id, func.count(Variant.id))
            .group_by(Variant.regos_group_id)
        ).all()
    )
    chosen = set(selected_groups(db))

    groups: list[dict] = []
    error = None
    if regos_config.is_configured():
        try:
            groups = [
                {"id": g["id"], "name": g.get("path") or g.get("name") or str(g["id"]),
                 "selected": g["id"] in chosen, "in_catalog": local.get(g["id"], 0)}
                for g in RegosClient().paginate("ItemGroup/Get")
            ]
            groups.sort(key=lambda g: (-g["in_catalog"], g["name"]))
        except (RegosError, RuntimeError, OSError) as exc:
            # REGOS недоступен — панель всё равно должна открыться и показать,
            # что уже выбрано: иначе сбой учётной системы блокирует работу
            error = str(exc)
    else:
        error = "Не задан REGOS_KEY"

    return {"groups": groups, "selected": sorted(chosen),
            "unassigned": local.get(None, 0), "error": error}


@router.put("/regos/groups")
def set_regos_groups(
    data: GroupsPatch,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Какие группы REGOS попадают в витрину. Пустой список — весь каталог."""
    from app.regos.catalog_sync import save_groups

    save_groups(db, data.group_ids)
    return {"selected": sorted(set(data.group_ids))}


@router.get("/regos/sync")
def sync_state(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Идёт ли сейчас обновление каталога из REGOS и чем кончилось последнее."""
    from app.regos import sync_run

    return {"running": sync_run.running(), "last": sync_run.last(db), "every_minutes": 30}


@router.post("/regos/sync", status_code=202)
def sync_now(user: TelegramUser = Depends(require_admin)):
    """Обновить каталог из REGOS сейчас, не дожидаясь таймера.

    То же, что делает таймер раз в 30 минут: цены, новинки (скрытыми
    карточками), коды для чека, снятие удалённого в REGOS. Идёт в фоне;
    итог — в GET /regos/sync.
    """
    from app.regos import config as regos_config, sync_run

    if not regos_config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    if sync_run.start(f"вручную · {user.name}") is None:
        raise HTTPException(status_code=409, detail="Обновление из REGOS уже идёт")
    log.info("Обновление каталога из REGOS запущено вручную: %s", user.name)
    return {"running": True}


class StockIn(BaseModel):
    stock_id: int = Field(gt=0)


def _rows(result):
    return (result.get("result") if isinstance(result, dict) else result) or []


@router.get("/regos/stocks")
def regos_stocks(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Склады REGOS, какой из них у витрины и сколько у каждого касс."""
    from app.regos import config as regos_config
    from app.regos.client import RegosClient, RegosError

    if not regos_config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    try:
        client = RegosClient()
        stocks = _rows(client.call("Stock/Get", {}))
        cash_count: dict[int, int] = {}
        for cash in _rows(client.call("OperatingCash/Get", {})):
            sid = (cash.get("stock") or {}).get("id")
            cash_count[sid] = cash_count.get(sid, 0) + 1
    except (RegosError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"REGOS не ответил: {exc}")
    return {
        "current": regos_config.stock_id(db),
        "stocks": [{"id": s["id"], "name": s.get("name") or f"Склад {s['id']}",
                    "cashes": cash_count.get(s["id"], 0)}
                   for s in stocks if not s.get("deleted_mark")],
    }


@router.post("/regos/stock")
def change_stock(
    data: StockIn,
    apply: bool = Query(False, description="false — только показать, что изменится"),
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_admin),
):
    """Переключить витрину на другой склад REGOS.

    От склада зависят остатки и то, куда уходят заказы: их видят только кассы
    этого склада. Предпросмотр (apply=false) показывает, сколько товаров
    витрины на новом складе есть в наличии. Касса для заказов, выбранная
    на старом складе, при переключении сбрасывается на общую очередь —
    касса чужого склада заказ не примет.
    """
    from app.regos import config as regos_config, prices
    from app.regos.client import RegosClient, RegosError
    from app.regos.orders_push import operating_cash, save_operating_cash

    if not regos_config.is_configured():
        raise HTTPException(status_code=400, detail="Не задан ключ REGOS")
    client = RegosClient()
    try:
        stocks = {s["id"]: s.get("name") or f"Склад {s['id']}"
                  for s in _rows(client.call("Stock/Get", {})) if not s.get("deleted_mark")}
        if data.stock_id not in stocks:
            raise HTTPException(status_code=400, detail="Такого склада в REGOS нет")
        variants = prices.storefront_variants(db)
        codes = [v.external_code for v in variants]

        def in_stock(stock_id):
            have = set()
            for start in range(0, len(codes), prices.CHUNK):
                chunk = codes[start:start + prices.CHUNK]
                for row in _rows(client.call("Item/GetExt", {
                    "limit": len(chunk), "offset": 0, "filters": [
                        {"Field": "stock_id", "Operator": "Equal", "Value": str(stock_id)},
                        {"Field": "price_type_id", "Operator": "Equal",
                         "Value": str(prices.current(db))},
                        {"Field": "code", "Operator": "In",
                         "Value": ",".join(str(int(c)) for c in chunk)}]})):
                    qty = row.get("quantity") or {}
                    common = (qty.get("common") if isinstance(qty, dict) else qty) or 0
                    if common > 0:
                        have.add(f"{int(row['item']['code']):06d}")
            return have

        current = regos_config.stock_id(db)
        now_have = in_stock(current) if codes else set()
        new_have = in_stock(data.stock_id) if codes else set()
    except (RegosError, RuntimeError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"REGOS не ответил: {exc}")

    names = {v.external_code: f"{v.product.name} {v.weight}" for v in variants}
    result = {
        "applied": False,
        "current": {"id": current, "name": stocks.get(current, f"Склад {current}"),
                    "in_stock": len(now_have)},
        "new": {"id": data.stock_id, "name": stocks[data.stock_id], "in_stock": len(new_have)},
        "total": len(codes),
        "missing": sorted(names[c] for c in codes if c not in new_have)[:10],
        "cash_reset": False,
    }
    if apply and data.stock_id != current:
        regos_config.save_stock(db, data.stock_id)
        db.commit()
        if operating_cash(db):
            save_operating_cash(db, 0)
            result["cash_reset"] = True
        result["applied"] = True
        log.info("Склад витрины: %s -> %s (%s)", current, data.stock_id, user.name)
    return result


class CashPatch(BaseModel):
    operating_cash_id: int = Field(ge=0)


@router.get("/regos/cash")
def get_cash(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Касса, на которую встают заказы, и кассы склада с последней сменой."""
    from app.regos import config as regos_config
    from app.regos.client import RegosClient, RegosError
    from app.regos.orders_push import cashes, operating_cash

    data = {"selected": operating_cash(db), "cashes": [], "error": None}
    if not regos_config.is_configured():
        data["error"] = "Не задан ключ REGOS"
        return data
    try:
        data["cashes"] = cashes(RegosClient(), regos_config.stock_id(db))
    except (RegosError, RuntimeError, OSError) as exc:
        data["error"] = f"REGOS не ответил: {exc}"
    return data


@router.put("/regos/cash")
def set_cash(
    data: CashPatch,
    db: Session = Depends(get_db),
    user: TelegramUser = Depends(require_admin),
):
    """Выбрать кассу для заказов. 0 — общая очередь склада.

    Действует на заказы, которые уйдут в REGOS после сохранения: уже
    выгруженные остаются, где стоят.
    """
    from app.regos import config as regos_config
    from app.regos.client import RegosClient, RegosError
    from app.regos.orders_push import cashes, save_operating_cash

    if data.operating_cash_id:
        try:
            known = {c["id"] for c in cashes(RegosClient(), regos_config.stock_id(db))}
        except (RegosError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=502, detail=f"REGOS не ответил: {exc}")
        if data.operating_cash_id not in known:
            raise HTTPException(status_code=400, detail="Такой кассы нет на складе магазина")
    save_operating_cash(db, data.operating_cash_id)
    log.info("Касса для заказов: %s (%s)", data.operating_cash_id or "общая очередь", user.name)
    return {"selected": data.operating_cash_id}


class AutoPushPatch(BaseModel):
    enabled: bool


@router.get("/regos/auto")
def get_auto_push(db: Session = Depends(get_db), _: TelegramUser = Depends(require_admin)):
    """Состояние автоотправки и готова ли она вообще работать."""
    from app.regos.orders_push import auto_push_enabled, auto_push_ready

    ready, reason = auto_push_ready(db)
    return {"enabled": auto_push_enabled(db), "ready": ready, "reason": reason}


@router.put("/regos/auto")
def set_auto_push(
    data: AutoPushPatch,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Включает или выключает отправку заказа в REGOS сразу после оформления."""
    from app.regos.orders_push import auto_push_ready, save_auto_push

    if data.enabled:
        ready, reason = auto_push_ready(db)
        if not ready:
            raise HTTPException(
                status_code=400,
                detail=f"Автоотправку включить нельзя: {reason}. "
                       "Иначе заказы будут уходить в никуда.",
            )
    save_auto_push(db, data.enabled)
    return {"enabled": data.enabled}


@router.post("/cleanup")
def cleanup(
    apply: bool = Query(False, description="false — только посчитать"),
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Убирает из витрины карточки, которые не относятся к выбранным группам.

    Удаляются только никогда не публиковавшиеся карточки: опубликованный товар
    админ снимает с витрины сам, осознанно. Если на фасовку ссылается заказ,
    внешний ключ стоит на RESTRICT и удаление не пройдёт — история заказов
    защищена самой схемой, а не аккуратностью этого кода.

    По умолчанию только считает. Удаление — apply=true.
    """
    from app.regos.catalog_sync import selected_groups

    groups = selected_groups(db)
    if not groups:
        raise HTTPException(
            status_code=400,
            detail="Группы не выбраны: удалять нечего, сейчас в витрину идёт весь каталог",
        )

    # карточка лишняя, если ни одна её фасовка не входит в выбранные группы
    keep = select(Variant.product_id).where(Variant.regos_group_id.in_(groups))
    stmt = select(Product).where(~Product.is_active, Product.id.notin_(keep))
    doomed = list(db.scalars(stmt))

    if not apply:
        return {"count": len(doomed), "applied": False,
                "sample": [p.name for p in doomed[:10]]}

    removed, blocked = 0, 0
    for product in doomed:
        try:
            db.delete(product)
            db.flush()
            removed += 1
        except Exception:               # noqa: BLE001 — на позицию ссылается заказ
            db.rollback()
            blocked += 1
    db.commit()
    return {"count": len(doomed), "removed": removed, "blocked": blocked, "applied": True}


@router.post("/categories")
def add_category(
    data: CategoryIn,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    if db.scalar(select(Category).where(Category.name == data.name)):
        raise HTTPException(status_code=400, detail="Категория с таким названием уже есть")
    category = Category(name=data.name.strip(), sort_order=data.sort_order)
    db.add(category)
    db.commit()
    return {"id": category.id, "name": category.name, "sort_order": category.sort_order}


@router.patch("/categories/{category_id}")
def edit_category(
    category_id: int,
    data: CategoryIn,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    other = db.scalar(select(Category).where(
        Category.name == data.name, Category.id != category_id))
    if other:
        raise HTTPException(status_code=400, detail="Категория с таким названием уже есть")
    category.name = data.name.strip()
    category.sort_order = data.sort_order
    db.commit()
    return {"id": category.id, "name": category.name, "sort_order": category.sort_order}


@router.delete("/categories/{category_id}")
def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    _: TelegramUser = Depends(require_admin),
):
    """Удаляет пустую категорию. С товарами — не даёт: внешний ключ стоит
    на RESTRICT, и товары иначе осиротели бы."""
    category = db.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Категория не найдена")
    count = db.scalar(
        select(func.count(Product.id)).where(Product.category_id == category_id)
    )
    if count:
        raise HTTPException(
            status_code=400,
            detail=f"В категории {count} товаров — сначала перенесите их в другую",
        )
    db.delete(category)
    db.commit()
    return {"deleted": category_id}
