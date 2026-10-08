"""Обновление каталога из REGOS: по таймеру и кнопкой в панели.

Таймер (rozmart-sync.timer, scripts/sync_regos.py) и кнопка — разные процессы,
а два обновления разом друг другу мешают: оба заводят новинки, и одна и та же
позиция REGOS получила бы две карточки. Поэтому одновременно идёт только одно —
это держит блокировка файла в каталоге приложения. /tmp не годится: у служб
PrivateTmp, и каждая видела бы свой файл.

Итог последнего обновления хранится в настройке sync:last — по нему панель
показывает, когда каталог обновлялся и что поменялось.
"""
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.db import SessionLocal
from app.models import Setting
from app.regos.catalog_sync import sync_catalog
from app.regos.client import RegosClient

try:
    import fcntl
except ImportError:  # Windows разработчика: там один процесс, хватает замка в памяти
    fcntl = None

log = logging.getLogger(__name__)

LOCK_FILE = Path(__file__).resolve().parents[2] / ".sync.lock"
SETTING_LAST = "sync:last"
# сколько изменений цены показывать в панели: целиком список нужен редко,
# а настройка не должна разрастаться
PRICE_CHANGES_SHOWN = 15

_lock = threading.Lock()


class Busy(Exception):
    """Обновление уже идёт — в этом процессе или в соседнем."""


@contextmanager
def exclusive():
    if not _lock.acquire(blocking=False):
        raise Busy
    handle = None
    try:
        if fcntl is not None:
            handle = open(LOCK_FILE, "a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy from None
        yield
    finally:
        if handle is not None:
            handle.close()        # закрытие файла снимает и блокировку
        _lock.release()


def running() -> bool:
    if _lock.locked():
        return True
    if fcntl is None:
        return False
    try:
        with open(LOCK_FILE, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    return False


def last(db) -> dict | None:
    row = db.get(Setting, SETTING_LAST)
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def _save(db, result: dict) -> None:
    value = json.dumps(result, ensure_ascii=False)
    row = db.get(Setting, SETTING_LAST)
    if row is None:
        db.add(Setting(name=SETTING_LAST, value=value))
    else:
        row.value = value
    db.commit()


def run(source: str, client: RegosClient | None = None, *, with_barcodes: bool = True) -> dict:
    """Одно обновление каталога. Busy — если другое уже идёт.

    Сбой REGOS записывается в итог и пробрасывается дальше: таймеру нужен
    код выхода, панели — текст ошибки.
    """
    with exclusive():
        started = time.monotonic()
        result = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "source": source}
        db = SessionLocal()
        try:
            try:
                report = sync_catalog(db, client or RegosClient(), with_barcodes=with_barcodes)
            except Exception as exc:
                db.rollback()
                result.update(ok=False, error=str(exc)[:300],
                              seconds=round(time.monotonic() - started))
                _save(db, result)
                raise
            result.update(
                ok=True,
                seconds=round(time.monotonic() - started),
                created=report.created,
                updated=report.updated,
                hidden=report.hidden,
                no_price=report.skipped_no_price,
                price_changed=len(report.price_changes),
                price_changes=[{"name": name, "was": was, "now": now}
                               for name, was, now in report.price_changes[:PRICE_CHANGES_SHOWN]],
            )
            _save(db, result)
            return result
        finally:
            db.close()


def start(source: str) -> threading.Thread | None:
    """Запуск из панели — в фоне: полный проход по каталогу REGOS идёт
    дольше, чем стоит держать открытым HTTP-запрос. None — уже идёт."""
    if running():
        return None

    def work():
        try:
            run(source)
        except Busy:
            log.info("Обновление из REGOS: уже идёт, кнопка пропущена")
        except Exception:
            log.exception("Обновление из REGOS кнопкой не удалось")

    thread = threading.Thread(target=work, name="regos-sync", daemon=True)
    thread.start()
    return thread
