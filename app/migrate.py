"""Досоздание недостающих колонок.

Полноценных миграций в проекте нет: раньше база пересоздавалась с нуля, потому
что лежала в SQLite и ничего ценного не содержала. На сервере так больше нельзя
— там заказы покупателей и разобранный вручную каталог.

Alembic под три колонки избыточен, но и молча оставлять базу без новых полей
нельзя: create_all() создаёт недостающие таблицы и НЕ добавляет колонки
в существующие. Приложение поднимется и упадёт позже, на первом же запросе,
причём в неочевидном месте.

Здесь — список изменений, которые нужно донести до уже созданной базы.
Каждое проверяется по факту наличия колонки, поэтому запуск безопасен
и повторяем. Когда изменений станет больше десятка, это надо заменить
на Alembic, а не растить дальше.
"""
import logging

from sqlalchemy import inspect, text

from app.db import engine

log = logging.getLogger(__name__)

# (таблица, колонка, тип). Тип пишется совместимым синтаксисом: одно и то же
# определение должно приниматься и SQLite, и PostgreSQL.
COLUMNS = [
    ("variants", "regos_group_id", "INTEGER"),
    ("orders", "regos_document_id", "INTEGER"),
    ("orders", "regos_error", "TEXT"),
    ("orders", "regos_attempts", "INTEGER DEFAULT 0 NOT NULL"),
    ("orders", "telegram_username", "VARCHAR(64)"),
    # таблица подписчиков появилась раньше, чем в ней понадобилось @имя
    ("notify_subscribers", "username", "VARCHAR(64)"),
    # очередь сообщений появилась раньше, чем сообщения сотрудникам
    ("notifications", "chat_id", "BIGINT"),
    # @имя переехало к чатам, когда бот стал служебным
    ("staff_chats", "username", "VARCHAR(64)"),
    # точка на карте: и у заказа, и в профиле покупателя
    ("orders", "lat", "DOUBLE PRECISION"),
    ("orders", "lon", "DOUBLE PRECISION"),
    ("customers", "lat", "DOUBLE PRECISION"),
    ("customers", "lon", "DOUBLE PRECISION"),
    # дом, подъезд и квартира отдельными полями
    ("customers", "house", "VARCHAR(20)"),
    ("customers", "entrance", "VARCHAR(20)"),
    ("customers", "flat", "VARCHAR(20)"),
    # оплата картой
    ("orders", "paid_at", "TIMESTAMP"),
    ("orders", "payment_charge_id", "VARCHAR(128)"),
    ("orders", "provider_charge_id", "VARCHAR(128)"),
    ("orders", "paid_amount", "INTEGER"),
    ("orders", "checkout_at", "TIMESTAMP"),
    # цена из REGOS и своя цена витрины — отдельно; вид цены у заказа
    ("variants", "regos_price", "INTEGER"),
    ("variants", "manual_price", "INTEGER"),
    ("orders", "price_type_id", "INTEGER"),
    # фискальный чек Payme: МХИК, код упаковки, НДС
    ("variants", "mxik", "VARCHAR(20)"),
    ("variants", "package_code", "VARCHAR(20)"),
    ("variants", "vat_percent", "INTEGER"),
    # фискальный чек из SetFiscalData
    ("payme_transactions", "fiscal_qr_url", "TEXT"),
    ("payme_transactions", "fiscal_data", "TEXT"),
    # фискальный чек онлайн-оплаты — в заказ REGOS, чтобы касса не печатала второй
    ("orders", "fiscal_qr_url", "TEXT"),
    ("orders", "fiscal_qr_regos_at", "TIMESTAMP"),
    ("orders", "fiscal_qr_regos_error", "TEXT"),
    ("orders", "fiscal_qr_regos_tries", "INTEGER DEFAULT 0 NOT NULL"),
    # бот доставщиков: кто взял заказ и когда доставил
    ("orders", "courier_id", "BIGINT"),
    ("orders", "courier_name", "VARCHAR(200)"),
    ("orders", "courier_taken_at", "TIMESTAMP"),
    ("orders", "courier_prev_status", "VARCHAR(20)"),
    ("orders", "delivered_at", "TIMESTAMP"),
    ("couriers", "language", "VARCHAR(5)"),
    ("staff_chats", "language", "VARCHAR(5)"),
    # причина отказа доставщика
    ("orders", "courier_drop_note", "TEXT"),
    ("orders", "courier_drop_by", "VARCHAR(200)"),
    ("orders", "courier_drop_at", "TIMESTAMP"),
    ("couriers", "pending_drop_order_id", "INTEGER"),
    ("couriers", "pending_drop_message_id", "BIGINT"),
    # выбранный покупателем способ онлайн-оплаты
    ("orders", "pay_provider", "VARCHAR(20)"),
]

# Разовые правки данных после добавления колонок. Каждая безопасна при повторе.
DATA_FIXES = [
    # до разделения цен в price лежала цена REGOS — переносим её туда, где ей
    # теперь место. Строки со своей ценой не трогаем: там price — ручная
    ("перенос цен REGOS",
     "UPDATE variants SET regos_price = price "
     "WHERE regos_price IS NULL AND manual_price IS NULL AND price IS NOT NULL"),
]

INDEXES = [
    ("ix_variants_regos_group_id", "variants", "regos_group_id"),
    ("ix_orders_regos_document_id", "orders", "regos_document_id"),
    ("ix_orders_courier_id", "orders", "courier_id"),
]

# Уникальные индексы отдельно: на них держится защита от двойного проведения
# платежа, и создаются они другой командой. NULL уникальности не мешает —
# и в PostgreSQL, и в SQLite неоплаченных заказов может быть сколько угодно.
UNIQUE_INDEXES = [
    ("ux_orders_payment_charge_id", "orders", "payment_charge_id"),
]


def apply() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, ddl in COLUMNS:
            if table not in existing_tables:
                continue                      # таблицу целиком создаст create_all
            columns = {c["name"] for c in inspector.get_columns(table)}
            if column in columns:
                continue
            log.info("Добавляю колонку %s.%s", table, column)
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))

        for name, table, column in INDEXES:
            if table not in existing_tables:
                continue
            indexes = {i["name"] for i in inspect(engine).get_indexes(table)}
            if name in indexes:
                continue
            log.info("Создаю индекс %s", name)
            conn.execute(text(f"CREATE INDEX {name} ON {table} ({column})"))

        for name, table, column in UNIQUE_INDEXES:
            if table not in existing_tables:
                continue
            found = inspect(engine)
            # индекс мог появиться и сам: create_all создаёт его для новой таблицы
            indexes = {i["name"] for i in found.get_indexes(table)}
            uniques = {tuple(u["column_names"]) for u in found.get_unique_constraints(table)}
            if name in indexes or (column,) in uniques or any(
                    i.get("unique") and i["column_names"] == [column]
                    for i in found.get_indexes(table)):
                continue
            log.info("Создаю уникальный индекс %s", name)
            conn.execute(text(f"CREATE UNIQUE INDEX {name} ON {table} ({column})"))

        for label, sql in DATA_FIXES:
            changed = conn.execute(text(sql)).rowcount
            if changed:
                log.info("%s: %s строк", label, changed)
