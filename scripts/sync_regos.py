"""Синхронизация каталога витрины с REGOS.

Заменяет scripts/import_catalog.py — импорт из Excel с диска разработчика.

Запуск:
    set REGOS_KEY=<ключ из карточки интеграции>
    .venv\\Scripts\\python.exe -m scripts.sync_regos

Полезные ключи:
    --dry-run     показать, что изменится, и ничего не писать
    --check       только проверить связь и тариф
    --no-barcodes пропустить второй проход за штрихкодами (быстрее вдвое)

Повторный запуск безопасен: позиция опознаётся по коду REGOS, витринные
название, фото и привязка к карточке не затираются.
"""
import argparse
import logging
import sys

from app.db import SessionLocal, init_db
from app.regos import config
from app.regos import sync_run
from app.regos.catalog_sync import sync_catalog
from app.regos.client import RegosClient, RegosError, account_info


def check(client: RegosClient) -> int:
    info = account_info(client)
    params = info.get("tariff_parameters", {})
    print(f"аккаунт : {info.get('api_login')}")
    print(f"тариф   : {info.get('tariff_name')}, до {info.get('paid_until')}, "
          f"{info.get('status')}")
    for name in ("api_access", "webhook_able"):
        state = "включён" if params.get(name, {}).get("is_enable") else "ВЫКЛЮЧЕН"
        print(f"{name:14}: {state}")
    if not params.get("api_access", {}).get("is_enable"):
        print("\nБез api_access синхронизация работать не будет.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Синхронизация каталога с REGOS")
    parser.add_argument("--dry-run", action="store_true",
                        help="показать изменения, ничего не записывая")
    parser.add_argument("--check", action="store_true",
                        help="только проверить связь и тариф")
    parser.add_argument("--no-barcodes", action="store_true",
                        help="не тянуть штрихкоды вторым проходом")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not config.is_configured():
        print("Не задан REGOS_KEY. Взять в кабинете REGOS: Настройки -> Интеграции -> "
              "открыть интеграцию -> поле «API», ключ это последний сегмент URL.",
              file=sys.stderr)
        return 2

    client = RegosClient()
    try:
        if args.check:
            return check(client)

        print(f"склад {config.STOCK_ID}, тип цены {config.PRICE_TYPE_ID}"
              + (f", группы {config.ITEM_GROUP_IDS}" if config.ITEM_GROUP_IDS else ""))

        init_db()
        if not args.dry_run:
            # через sync_run: не пересечься с кнопкой в панели и оставить
            # панели итог — когда обновлялось и что поменялось
            try:
                result = sync_run.run("по расписанию", client, with_barcodes=not args.no_barcodes)
            except sync_run.Busy:
                print("Обновление уже идёт (запущено из панели) — этот запуск пропущен")
                return 0
            for change in result["price_changes"]:
                print(f"  цена: {change['name']}: {change['was']} -> {change['now']}")
            print(f"создано {result['created']}, обновлено {result['updated']}, "
                  f"скрыто {result['hidden']}, без цены {result['no_price']}, "
                  f"изменений цены {result['price_changed']}")
            return 0
        db = SessionLocal()
        try:
            report = sync_catalog(db, client, with_barcodes=not args.no_barcodes, commit=False)
            db.rollback()
            print("(dry-run: изменения отменены, в базу ничего не записано)")
            for name, was, now in report.price_changes[:20]:
                print(f"  цена: {name}: {was} -> {now}")
            if len(report.price_changes) > 20:
                print(f"  … и ещё {len(report.price_changes) - 20}")
            print(report.summary())
        finally:
            db.close()
    except RegosError as exc:
        print(f"REGOS отказал: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"Сбой обмена: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
