"""Формат данных, которые API отдаёт клиентскому приложению.

Служебные поля учётной системы (код REGOS, складское наименование) сюда
намеренно не попадают — клиенту они не показываются (BR-36).
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class CategoryOut(BaseModel):
    id: int
    name: str
    product_count: int
    photo: str | None   # обложка: снимок первого товара категории


class VariantOut(BaseModel):
    id: int
    weight: str
    price: int


class ProductListOut(BaseModel):
    """Карточка в списке каталога: один товар, цена «от» (BR-37)."""

    id: int
    # категорию отдаём вместе с товаром: по ней приложение восстанавливает
    # выбранный отбор, не спрашивая сервер второй раз
    category_id: int
    name: str
    photo: str | None
    min_price: int
    weights: list[str]


class ProductDetailOut(BaseModel):
    id: int
    name: str
    category: str
    photo: str | None
    description: str | None
    composition: str | None
    shelf_life: str | None
    variants: list[VariantOut]


# ---------- заказ ----------

# Предлагается один срок — NEW (с 12.10.2026); прежние принимаются от
# приложения, открытого до обновления, и остаются в старых заказах
DeliverySlot = Literal["Завтра до конца рабочего дня", "Как можно скорее", "Сегодня вечером",
                       "Завтра утром"]
PaymentMethod = Literal["cash", "online"]


class OrderItemIn(BaseModel):
    """Клиент присылает только что и сколько: цену считает сервер."""

    variant_id: int
    quantity: int = Field(ge=1, le=99)


class ContactIn(BaseModel):
    """Данные покупателя. Одни и те же при заказе и при правке профиля."""

    customer_name: str = Field(min_length=2, max_length=200)
    phone: str = Field(min_length=9, max_length=30)
    # улица с ориентиром: то, что приходит с карты или пишется руками
    address: str = Field(min_length=5, max_length=500)
    # Дом, подъезд и квартира отдельными полями. Раньше всё это писали одной
    # строкой, и половина заказов приходила без квартиры: в длинной строке
    # её просто забывали. Номер дома приходит с карты, остальное — руками.
    house: str | None = Field(default=None, max_length=20)
    entrance: str | None = Field(default=None, max_length=20)
    flat: str | None = Field(default=None, max_length=20)
    # Точка на карте. Необязательна: карта есть не у всех под рукой, и адрес
    # строкой остаётся главным. Границы проверяем, чтобы в базу не попало
    # что попало из подменённого запроса — вплоть до NaN.
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    # согласие на обработку данных. Спрашивается один раз, дальше уже дано
    consent: bool = False

    @field_validator("lat", "lon")
    @classmethod
    def round_point(cls, value):
        # шести знаков хватает на точность около 10 см: всё, что дальше, —
        # шум от пальца на экране, и в базе ему делать нечего
        return None if value is None else round(value, 6)

    @field_validator("phone")
    @classmethod
    def check_phone(cls, value: str) -> str:
        digits = "".join(c for c in value if c.isdigit())
        if not digits.startswith("998") or len(digits) != 12:
            raise ValueError("Номер должен быть узбекским: +998 и 9 цифр")
        return f"+{digits}"

    # before: иначе длину проверят до обрезки и строка из одних пробелов пройдёт
    @field_validator("customer_name", "address", "house", "entrance", "flat",
                     mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    def full_address(self) -> str:
        """Адрес одной строкой — для курьера, кассы и учётной системы.

        Собирается на сервере, а не на телефоне: строка уходит и в REGOS,
        и в сообщение сотрудникам, и в карточку заказа, и выглядеть везде
        должна одинаково.
        """
        parts = [self.address]
        if self.house:
            parts.append(f"дом {self.house}")
        if self.entrance:
            parts.append(f"подъезд {self.entrance}")
        if self.flat:
            parts.append(f"кв. {self.flat}")
        return ", ".join(parts)


class ProfileOut(BaseModel):
    name: str
    phone: str
    address: str
    house: str | None = None
    entrance: str | None = None
    flat: str | None = None
    lat: float | None = None
    lon: float | None = None
    consent: bool


class OrderIn(ContactIn):
    delivery_slot: DeliverySlot
    payment_method: PaymentMethod
    # способ онлайн-оплаты: какой выбрал покупатель. Для наличных не нужен
    pay_provider: Literal["telegram", "payme", "paynet", "click"] | None = None
    comment: str | None = Field(default=None, max_length=500)
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    # ключ попытки оформления: повтор с тем же ключом вернёт уже созданный заказ
    client_key: str | None = Field(default=None, min_length=8, max_length=64)
    # итог, который видел покупатель. Разошёлся с расчётом сервера — не создаём
    # заказ молча по другой цене, а просим обновить корзину
    expected_total: int | None = Field(default=None, ge=0)


class OrderItemOut(BaseModel):
    product_name: str
    weight: str
    price: int
    quantity: int
    photo: str | None = None


class OrderOut(BaseModel):
    number: str
    status: str
    status_text: str      # то же состояние словами покупателя
    created_at: datetime
    delivery_slot: str
    payment_method: str
    # куда и кому везли — покупатель смотрит это в карточке своего заказа,
    # чтобы проверить адрес и вспомнить, что просил в комментарии
    address: str
    phone: str
    comment: str | None = None
    goods_total: int
    delivery_price: int
    total: int
    items: list[OrderItemOut]
    # оплачен ли картой: по этому приложение решает, показывать ли «Оплатить»
    paid: bool = False
    # до какого времени неоплаченный заказ живёт — потом отменяется сам
    pay_until: datetime | None = None
    # способ онлайн-оплаты, выбранный покупателем: какую оплату открыть по «Оплатить»
    pay_provider: str | None = None
