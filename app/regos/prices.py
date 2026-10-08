"""Цены витрины: вид цены REGOS и своя цена товара.

Цена фасовки на витрине (`Variant.price`) собирается из двух:

* `regos_price` — цена из REGOS по выбранному виду цены. Её обновляет
  синхронизация каталога каждые полчаса;
* `manual_price` — своя цена, заданная администратором в панели. Если она есть,
  действует она, а синхронизация её не трогает — иначе ручная правка слетала бы
  при следующем проходе.

Вид цены. В REGOS их одиннадцать: розничные по магазинам (SAMPI, ASKIYA, RDB,
просто «Розничная цена») и служебные с решёткой (#ECO, #BOCHKA…). Разница
ощутимая: «Барбекю Бараньи» — 57 000 по ценам SAMPI и 65 000 по ценам RDB.
Выбор хранится в настройках (Setting), а не в переменной окружения: меняет его
администратор из панели, без доступа к серверу и перезапуска.

Склад при этом не меняется: остатки и касса, куда уходят заказы, — склад
витрины (config.stock_id, тоже выбирается в панели). Вид цены другого магазина
покажет на витрине его цены, но заказ уйдёт на кассу склада витрины — с ценами
витрины в документе.
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Product, Setting, Variant
from app.regos import config
from app.regos.client import RegosClient

log = logging.getLogger(__name__)

SETTING = "regos_price_type_id"

# сколько кодов спрашивать одним вызовом Item/GetExt
CHUNK = 100

# Верхняя граница своей цены: защита от лишнего нуля. Самая дорогая позиция
# витрины стоит около 300 000 сумов; миллион за колбасу — почти наверняка опечатка.
MAX_PRICE = 10_000_000


def current(db: Session) -> int:
    """Вид цены, по которому сейчас работает витрина."""
    row = db.get(Setting, SETTING)
    if row is not None and row.value.isdigit():
        return int(row.value)
    return config.PRICE_TYPE_ID


def save(db: Session, price_type_id: int) -> None:
    row = db.get(Setting, SETTING)
    if row is None:
        db.add(Setting(name=SETTING, value=str(price_type_id)))
    else:
        row.value = str(price_type_id)


def effective(variant: Variant) -> int | None:
    """Цена, по которой продаёт витрина: своя, если задана, иначе из REGOS."""
    return variant.manual_price if variant.manual_price is not None else variant.regos_price


def apply_regos_price(variant: Variant, regos_price: int | None) -> None:
    """Записывает цену из REGOS и пересчитывает цену витрины.

    Фасовка без цены покупателю бесполезна — снимается с публикации, но не
    удаляется: на неё могут ссылаться заказы (BR-09). Со своей ценой фасовка
    продаётся, даже если в REGOS цены по этому виду нет.
    """
    variant.regos_price = regos_price
    variant.price = effective(variant)
    variant.is_active = variant.price is not None


def set_manual(variant: Variant, price: int | None) -> None:
    """Своя цена фасовки. None — вернуть цену REGOS."""
    variant.manual_price = price
    variant.price = effective(variant)
    variant.is_active = variant.price is not None


def _to_sum(value) -> int | None:
    """REGOS отдаёт цену дробью; ноль и отрицательное — «цены нет»."""
    if value in (None, ""):
        return None
    price = int(round(float(value)))
    return price if price > 0 else None


def fetch_types(client: RegosClient) -> list[dict]:
    """Виды цен REGOS: [{id, name}], удалённые пропускаем."""
    result = client.call("PriceType/Get", {"limit": 200, "offset": 0})
    rows = result.get("result") if isinstance(result, dict) else result
    return [{"id": r["id"], "name": r["name"]}
            for r in rows or [] if not r.get("deleted_mark")]


def fetch_prices(client: RegosClient, price_type_id: int,
                 codes: list[str], stock_id: int | None = None) -> dict[str, int | None]:
    """Цены нужных позиций по виду цены: код -> цена или None.

    Весь каталог (8050 позиций, полторы минуты) ради витрины из нескольких
    десятков товаров не нужен: фильтр `code In` отдаёт только наши. Код
    в фильтре — число, без ведущих нулей, как его хранит REGOS.
    """
    found: dict[str, int | None] = {code: None for code in codes}
    for start in range(0, len(codes), CHUNK):
        chunk = codes[start:start + CHUNK]
        result = client.call("Item/GetExt", {
            "limit": len(chunk), "offset": 0,
            "filters": [
                {"Field": "stock_id", "Operator": "Equal",
                 "Value": str(stock_id or config.STOCK_ID)},
                {"Field": "price_type_id", "Operator": "Equal", "Value": str(price_type_id)},
                {"Field": "code", "Operator": "In",
                 "Value": ",".join(str(int(code)) for code in chunk)},
            ],
        })
        rows = result.get("result") if isinstance(result, dict) else result
        for row in rows or []:
            code = f"{int(row['item']['code']):06d}"
            found[code] = _to_sum(row.get("price"))
    return found


def storefront_variants(db: Session) -> list[Variant]:
    """Фасовки опубликованных товаров — то, что видит покупатель."""
    products = db.scalars(
        select(Product).where(Product.is_active).options(selectinload(Product.variants))
    ).all()
    return [v for p in products for v in p.variants]


def _plan(db: Session, client: RegosClient, price_type_id: int):
    """Фасовки витрины, их цены по новому виду и сводка — за один запрос к REGOS.

    Сводка считается по цене, которую увидит покупатель: у фасовок со своей
    ценой она не изменится, какой бы вид цены ни выбрали.
    """
    variants = storefront_variants(db)
    prices = fetch_prices(client, price_type_id, [v.external_code for v in variants],
                          config.stock_id(db))
    rows, stats = [], {"total": len(variants), "same": 0, "up": 0, "down": 0,
                       "lost": 0, "manual": 0}
    for v in variants:
        new_regos = prices.get(v.external_code)
        new_price = v.manual_price if v.manual_price is not None else new_regos
        if v.manual_price is not None:
            stats["manual"] += 1
        if new_price is None:
            stats["lost"] += 1
            change = "пропадёт с витрины"
        elif v.price is None or new_price == v.price:
            stats["same"] += 1
            change = "без изменений" if v.price is not None else "появится"
        elif new_price > v.price:
            stats["up"] += 1
            change = "дороже"
        else:
            stats["down"] += 1
            change = "дешевле"
        rows.append({"variant_id": v.id, "product": v.product.name, "weight": v.weight,
                     "now": v.price, "then": new_price, "change": change,
                     "manual": v.manual_price is not None})
    return variants, prices, {"price_type_id": price_type_id, "stats": stats, "rows": rows}


def compare(db: Session, client: RegosClient, price_type_id: int) -> dict:
    """Что станет с витриной при другом виде цены. Ничего не меняет."""
    return _plan(db, client, price_type_id)[2]


def switch(db: Session, client: RegosClient, price_type_id: int) -> dict:
    """Переключает витрину на другой вид цены.

    Фасовки на витрине получают новые цены сразу — одним запросом к REGOS.
    Скрытые товары подтянутся на ближайшей синхронизации каталога (раз
    в полчаса): она берёт вид цены из той же настройки.
    """
    variants, prices, summary = _plan(db, client, price_type_id)
    for variant in variants:
        apply_regos_price(variant, prices.get(variant.external_code))
    save(db, price_type_id)
    db.commit()
    log.info("Вид цены витрины: %s. %s", price_type_id, summary["stats"])
    return summary
