"""Настройки подключения к REGOS.

Ключ интеграции равносилен паролю от учётной системы: он открывает весь каталог,
цены, остатки и чеки. Поэтому берётся только из окружения и в репозиторий не едет.
"""
import os

# Базовый адрес шлюза. Ключ — последний сегмент URL из карточки локальной
# интеграции в кабинете: Настройки -> Интеграции -> поле «API».
GATEWAY = "https://integration.regos.uz/gateway/out/{key}/v1"

REGOS_KEY = os.getenv("REGOS_KEY", "")

# Магазин, из которого берём каталог и остатки. В REGOS цена привязана
# не к складу, а к типу цены, и на каждую точку заведён свой тип:
#   склад 1 SAMPI  -> тип цены 1      склад 5 RDB    -> тип цены 5
#   склад 4 ASKIYA -> тип цены 4      склад 6 SAGBAN -> тип цены 6
# Пара обязана быть согласованной, иначе витрина покажет цены другой точки.
# Метод Item/GetExt без price_type_id молча берёт первый тип по id — то есть
# цены SAMPI для любого склада. Поэтому оба значения всегда передаются явно.
#
# Оба выбираются в панели («Состав каталога»), окружение — только начальные
# значения: склад см. stock_id(), вид цены — app/regos/prices.py.
STOCK_ID = int(os.getenv("REGOS_STOCK_ID", "5"))
PRICE_TYPE_ID = int(os.getenv("REGOS_PRICE_TYPE_ID", "5"))

SETTING_STOCK = "regos_stock_id"


def stock_id(db) -> int:
    """Склад витрины: с него остатки, на него уходят заказы, его кассы их видят.

    Хранится в настройках, а не в окружении: магазин, который собирает
    заказы, меняют из панели без доступа к серверу и перезапуска.
    """
    from app.models import Setting

    row = db.get(Setting, SETTING_STOCK)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return STOCK_ID


def save_stock(db, value: int) -> None:
    from app.models import Setting

    row = db.get(Setting, SETTING_STOCK)
    if row is None:
        db.add(Setting(name=SETTING_STOCK, value=str(value)))
    else:
        row.value = str(value)

# Группы номенклатуры REGOS, попадающие в витрину. Пусто — берём весь каталог.
# В базе 8050 позиций, из них с ценой 7151: тянуть всё на витрину колбасного
# магазина смысла нет, поэтому список групп задаётся явно.
ITEM_GROUP_IDS = [
    int(g) for g in os.getenv("REGOS_ITEM_GROUPS", "").replace(" ", "").split(",") if g
]

# Куда отправлять оформленные заказы (DocOrderDelivery/AddFull).
# from_id — источник заказа из справочника DeliveryFrom. На момент подключения
# в REGOS заведён только «Uzum tezkor» (id 1): для ROZMART нужен свой источник,
# создаётся через DeliveryFrom/Add. Пока не создан — выгрузка заказов выключена.
ORDER_FROM_ID = int(os.getenv("REGOS_ORDER_FROM_ID", "0"))
ORDER_DELIVERY_TYPE_ID = int(os.getenv("REGOS_ORDER_DELIVERY_TYPE_ID", "0"))

# Способы оплаты витрины -> PaymentType в REGOS.
PAYMENT_TYPE_IDS = {
    "cash": int(os.getenv("REGOS_PAYMENT_CASH_ID", "1")),      # Наличные
    "online": int(os.getenv("REGOS_PAYMENT_ONLINE_ID", "2")),  # Пласт. карта
}


def is_configured() -> bool:
    """Есть ли ключ. Без него синхронизация не запускается, но витрина работает:
    каталог остаётся тем, что уже лежит в базе."""
    return bool(REGOS_KEY)


def orders_enabled() -> bool:
    """Готова ли выгрузка заказов. Требует заведённого источника в REGOS."""
    return is_configured() and ORDER_FROM_ID > 0


def endpoint() -> str:
    if not REGOS_KEY:
        raise RuntimeError(
            "Не задан REGOS_KEY. Взять в кабинете REGOS: Настройки -> Интеграции -> "
            "открыть интеграцию -> поле «API», ключ это последний сегмент URL."
        )
    return GATEWAY.format(key=REGOS_KEY)
