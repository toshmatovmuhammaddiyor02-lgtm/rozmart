"""Синхронизация каталога витрины с REGOS.

Заменяет ручной импорт из Excel (scripts/import_catalog.py).

Про модель данных. В REGOS нет витринного товара с фасовками: каждая фасовка —
отдельная номенклатурная позиция со своим кодом. Скажем, «Экстра варёная» на
витрине — это четыре позиции REGOS: RZ VAR. EKSTRA 400GR, 455GR, 700GR и 855GR.
Группировку «одна карточка, несколько весов» раньше давал файл сопоставления,
который вели руками. Здесь она живёт в самой базе: Variant.external_code хранит
код REGOS, а к какому Product относится фасовка — решает контент-менеджер.
Синхронизация эту связь не переписывает, иначе ручная работа по витрине
затиралась бы при каждом запуске.

Про ключ сопоставления. Variant.external_code — это поле code номенклатуры
REGOS, дополненное нулями до шести знаков («003098» для code 3098). Именно так
его записал первый импорт из Excel, и менять формат нельзя: иначе синхронизация
не узнает уже заведённые позиции и заведёт им дубли. Внутренний item.id для
этого не годится — в выгрузке Excel его не было.

Что делает синхронизация:
  * известная фасовка (external_code уже есть) — обновляет цену, штрихкод,
    доступность. Витринное название, фото и привязку к карточке не трогает;
  * новая позиция — заводит скрытую карточку с одной фасовкой (BR-34):
    товар публикуется вручную после того, как ему задали название и фото;
  * позиция пропала из REGOS или осталась без цены — снимает с публикации,
    но не удаляет: на неё могут ссылаться оформленные заказы (BR-09).
"""
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category, Product, Setting, Variant
from app.regos import config, prices
from app.regos.client import RegosClient

log = logging.getLogger(__name__)

# Категория, куда падают новинки до разбора контент-менеджером.
INBOX_CATEGORY = "Новые из REGOS"
INBOX_SORT_ORDER = 999


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    hidden: int = 0
    price_changes: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    skipped_no_price: int = 0

    def summary(self) -> str:
        return (
            f"создано {self.created}, обновлено {self.updated}, скрыто {self.hidden}, "
            f"без цены {self.skipped_no_price}, изменений цены {len(self.price_changes)}"
        )


def external_code(item: dict) -> str:
    """Код номенклатуры REGOS в том виде, в каком он лежит в базе витрины:
    шесть знаков с ведущими нулями. Так его записал импорт из Excel."""
    return f"{int(item['code']):06d}"


def _price_to_sum(value) -> int | None:
    """Цена REGOS -> целые сумы (BR-10).

    В REGOS цена — decimal, на витрине сумы целыми числами. Ноль и отрицательное
    значение считаем отсутствием цены: фасовка без цены не публикуется, а ноль
    исказил бы «цену от» в каталоге (BR-37).
    """
    if value is None:
        return None
    price = int(round(float(value)))
    return price if price > 0 else None


def _weight_from_name(name: str) -> str:
    """Фасовка из наименования REGOS: «7UP MOXITO 0.449L» -> «0.449L».

    Грубая эвристика для новинок — контент-менеджер всё равно правит руками.
    Нужна только чтобы фасовка не осталась пустой строкой.
    """
    tail = name.strip().split()[-1] if name.strip() else ""
    return tail if any(ch.isdigit() for ch in tail) else "—"


def _weight_to_grams(weight: str) -> int:
    """Вес в граммах для сортировки фасовок в карточке: «455 г» строкой
    сортируется бессмысленно. Не распознали — 0, порядок задаст человек."""
    digits = "".join(ch for ch in weight if ch.isdigit())
    if not digits:
        return 0
    grams = int(digits)
    low = weight.lower()
    if "kg" in low or "кг" in low:
        grams *= 1000
    return grams


def _inbox_category(db: Session) -> Category:
    category = db.scalar(select(Category).where(Category.name == INBOX_CATEGORY))
    if category is None:
        category = Category(name=INBOX_CATEGORY, sort_order=INBOX_SORT_ORDER)
        db.add(category)
        db.flush()
    return category


SETTING_GROUPS = "regos_item_groups"


def selected_groups(db: Session) -> list[int]:
    """Группы номенклатуры, отобранные администратором в панели.

    Настройка живёт в базе, а не в переменной окружения: её меняет
    контент-менеджер, и перезапускать ради этого службу незачем. Значение
    из окружения остаётся как начальное — им удобно задать состав при первой
    установке, пока в панель ещё никто не заходил.
    """
    row = db.get(Setting, SETTING_GROUPS)
    if row is None:
        return list(config.ITEM_GROUP_IDS)
    try:
        return [int(x) for x in json.loads(row.value)]
    except (TypeError, ValueError):
        log.warning("Настройка %s испорчена, беру весь каталог", SETTING_GROUPS)
        return []


def save_groups(db: Session, group_ids: list[int]) -> None:
    row = db.get(Setting, SETTING_GROUPS)
    value = json.dumps(sorted(set(int(g) for g in group_ids)))
    if row is None:
        db.add(Setting(name=SETTING_GROUPS, value=value))
    else:
        row.value = value
    db.commit()


def fetch_items(client: RegosClient, groups: list[int] | None = None,
                price_type_id: int | None = None, stock_id: int | None = None) -> list[dict]:
    """Каталог магазина с ценами и остатками.

    stock_id и price_type_id передаются всегда: без price_type_id REGOS молча
    подставит первый тип цены по id, то есть цены чужой точки. Вид цены —
    тот, что выбран в панели (app/regos/prices.py), по умолчанию из окружения.
    """
    filters = [
        {"Field": "stock_id", "Operator": "Equal", "Value": str(stock_id or config.STOCK_ID)},
        {"Field": "price_type_id", "Operator": "Equal",
         "Value": str(price_type_id or config.PRICE_TYPE_ID)},
    ]
    if groups:
        filters.append({
            "Field": "group_id",
            "Operator": "In",
            "Value": ",".join(str(g) for g in groups),
        })

    return list(client.paginate("Item/GetExt", {"filters": filters}))


def fetch_full_catalog(client: RegosClient) -> tuple[dict[str, str], set[str]]:
    """Второй проход по каталогу: штрихкоды и полный перечень кодов REGOS.

    Нужен по двум причинам. Во-первых, Item/GetExt не отдаёт штрихкоды — они
    есть только в Item/Get. Во-вторых, по нему определяется, что позиция из
    учётной системы действительно исчезла: судить об этом по выборке Item/GetExt
    нельзя, потому что она сужена фильтром по складу и группам, и любая позиция
    вне фильтра выглядела бы удалённой.

    Возвращает (штрихкоды по коду, множество всех живых кодов).
    """
    barcodes: dict[str, str] = {}
    codes: set[str] = set()
    for item in client.paginate("Item/Get", {
        "filters": [{"Field": "deleted_mark", "Operator": "Equal", "Value": "false"}]
    }):
        code = external_code(item)
        codes.add(code)
        barcode = item.get("base_barcode") or (item.get("barcode_list") or "").split(",")[0]
        if barcode:
            barcodes[code] = barcode.strip()
    return barcodes, codes


def sync_catalog(
    db: Session,
    client: RegosClient,
    *,
    with_barcodes: bool = True,
    commit: bool = True,
) -> SyncReport:
    """Приводит каталог витрины в соответствие с REGOS. Повторный запуск безопасен.

    commit=False оставляет изменения незафиксированными — так работает --dry-run:
    отчёт собирается на реальных данных, но в базу ничего не попадает.
    """
    report = SyncReport()

    groups = selected_groups(db)
    log.info("группы номенклатуры: %s", groups or "все")
    price_type_id = prices.current(db)
    stock_id = config.stock_id(db)
    log.info("склад: %s, вид цены: %s", stock_id, price_type_id)
    items = fetch_items(client, groups, price_type_id, stock_id)
    log.info("REGOS отдал позиций: %s", len(items))

    # Полный перечень кодов нужен, чтобы отличить «позиция удалена из REGOS»
    # от «позиция не попала в фильтр по складу и группам». Без него снимать
    # товары с публикации нельзя — скроется половина живой витрины.
    barcodes, live_codes = fetch_full_catalog(client) if with_barcodes else ({}, None)

    existing = {v.external_code: v for v in db.scalars(select(Variant))}
    seen: set[str] = set()

    for row in items:
        item = row["item"]
        code = external_code(item)
        seen.add(code)

        price = _price_to_sum(row.get("price"))
        if price is None:
            report.skipped_no_price += 1

        variant = existing.get(code)
        if variant is None:
            variant = _create_variant(db, item, price, barcodes.get(code))
            report.created += 1
            continue

        if variant.regos_price != price:
            report.price_changes.append((item["name"], variant.regos_price, price))
        # Пишется только цена REGOS. Своя цена администратора остаётся
        # в силе — синхронизация её не видит и не трогает. Фасовку без
        # итоговой цены снимаем с публикации, но не удаляем (BR-09)
        prices.apply_regos_price(variant, price)
        variant.regos_group_id = (item.get("group") or {}).get("id")
        if barcode := barcodes.get(code):
            variant.barcode = barcode
        apply_fiscal(variant, item)
        report.updated += 1

    # Витрина бывает шире выбранных групп: товар опубликовали, потом группу
    # сняли с отбора (или отметили родительскую группу, а товары лежат
    # в подгруппах) — и в выборку выше он не попадает. Цена и коды для чека
    # нужны у всего, что продаёт витрина, поэтому такие берём отдельным
    # запросом по их кодам. Группы решают только, какие новинки приезжают.
    # 7–8 октября 2026 отмеченной была одна «Сыр» без своих позиций, и цены
    # витрины сутки не обновлялись вовсе
    fill_fiscal(db, client, [v for v in prices.storefront_variants(db)
                             if v.external_code not in seen], price_type_id, report)

    # Исчезнувшие из REGOS — скрыть. Не удалять: на фасовку ссылаются
    # оформленные заказы (BR-09), а внешний ключ стоит на RESTRICT.
    # Сверяем с полным перечнем кодов, а не с выборкой: позиция вне фильтра
    # по складу и группам жива, просто не относится к этой витрине.
    if live_codes is not None:
        for code, variant in existing.items():
            if code not in live_codes and variant.is_active:
                variant.is_active = False
                report.hidden += 1
    elif any(code not in seen for code in existing):
        log.warning("--no-barcodes: снятие с публикации пропущено, "
                    "без полного перечня кодов отличить удалённые позиции нельзя")

    if commit:
        db.commit()
    log.info("Синхронизация каталога: %s", report.summary())
    return report


def _create_variant(db: Session, item: dict, price: int | None, barcode: str | None) -> Variant:
    """Новая позиция из REGOS — скрытая карточка с одной фасовкой.

    BR-34: товар публикуется вручную, после того как ему задали витринное
    название и фото. Складское наименование покупателю не показывается (BR-36),
    но кладётся в name как черновик, чтобы менеджер понимал, что перед ним.
    """
    weight = _weight_from_name(item["name"])
    product = Product(
        category_id=_inbox_category(db).id,
        name=item["name"],
        is_active=False,
    )
    db.add(product)
    db.flush()

    variant = Variant(
        product_id=product.id,
        external_code=external_code(item),
        weight=weight,
        weight_grams=_weight_to_grams(weight),
        regos_group_id=(item.get("group") or {}).get("id"),
        price=price,
        regos_price=price,
        barcode=barcode,
        is_active=False,
    )
    apply_fiscal(variant, item)
    db.add(variant)
    return variant


def fill_fiscal(db: Session, client: RegosClient, variants: list[Variant],
                price_type_id: int, report: SyncReport | None = None) -> int:
    """МХИК, код упаковки и НДС для фасовок вне выборки — по их кодам.

    С report (синхронизация каталога) — ещё и цена REGOS, как у фасовок
    из выборки. Без него (карточка товара в панели) цены не трогаем.
    """
    by_code = {v.external_code: v for v in variants}
    codes = list(by_code)
    stock_id = config.stock_id(db)
    filled = 0
    for start in range(0, len(codes), prices.CHUNK):
        chunk = codes[start:start + prices.CHUNK]
        result = client.call("Item/GetExt", {
            "limit": len(chunk), "offset": 0,
            "filters": [
                {"Field": "stock_id", "Operator": "Equal", "Value": str(stock_id)},
                {"Field": "price_type_id", "Operator": "Equal", "Value": str(price_type_id)},
                {"Field": "code", "Operator": "In",
                 "Value": ",".join(str(int(code)) for code in chunk)},
            ],
        })
        rows = result.get("result") if isinstance(result, dict) else result
        for row in rows or []:
            variant = by_code.get(external_code(row["item"]))
            if variant is not None:
                apply_fiscal(variant, row["item"])
                filled += 1
                if report is not None:
                    price = _price_to_sum(row.get("price"))
                    if price is None:
                        report.skipped_no_price += 1
                    if variant.regos_price != price:
                        report.price_changes.append((row["item"]["name"], variant.regos_price, price))
                    prices.apply_regos_price(variant, price)
                    variant.regos_group_id = (row["item"].get("group") or {}).get("id")
                    report.updated += 1
    if variants:
        log.info("коды для чека по фасовкам витрины вне выборки: %s из %s", filled, len(variants))
    return filled


def apply_fiscal(variant: Variant, item: dict) -> None:
    """Реквизиты для фискального чека: МХИК (в REGOS — icps), код упаковки
    и НДС. Нужны при оплате через Payme — без них чек не уйдёт в налоговую.

    Пустое из REGOS не затирает уже известное: код упаковки заполняют
    в REGOS вручную, и сбой одного прохода не должен его стереть.
    """
    if mxik := str(item.get("icps") or "").strip():
        variant.mxik = mxik
    if package := str(item.get("package_code") or "").strip():
        variant.package_code = package
    vat = item.get("vat") or {}
    if vat.get("value") is not None:
        variant.vat_percent = int(round(float(vat["value"]))) if vat.get("enabled", True) else 0
