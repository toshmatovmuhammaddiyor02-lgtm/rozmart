import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.admin import router as admin_router
from app.admin_users import router as admin_users_router
from app import admin_auth, notify, payments
from app.bot import router as bot_router
from app.catalog import router as catalog_router
from app.courier_bot import router as courier_router
from app.db import get_db, init_db
from app.geocode import router as geocode_router
from app.orders import router as orders_router
from app.payme import router as payme_router
from app.paynet import router as paynet_router
from app.click import router as click_router
from app.profile import router as profile_router
from app.regos import orders_sync
from app.seed import seed_catalog
from app.shop_bot import router as shop_router
from sqlalchemy.orm import Session
from app.telegram import check_configuration

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Свои сообщения уровня INFO иначе не доходят до журнала: uvicorn
    # настраивает только собственные логгеры, а корневой остаётся без
    # обработчика и с уровнем WARNING. Из-за этого фоновые потоки — отправка
    # сообщений и синхронизация статусов — работали бы совершенно молча,
    # и о том, что они сделали, узнать было бы неоткуда.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    check_configuration()
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    seed_catalog()
    # уведомления покупателю уходят отдельным потоком: см. app/notify.py
    notify.start_worker()
    # и ещё один поток забирает статусы заказов из REGOS: кассир меняет их там,
    # а покупатель смотрит сюда
    orders_sync.start_worker()
    # и третий отменяет заказы, не оплаченные картой вовремя
    payments.start_worker()
    yield


# Схема API наружу не отдаётся: /docs и /openapi.json показывают посторонним
# устройство сервиса — какие есть методы и какие поля они принимают. Данные они
# не выдают (методы требуют подписи Telegram), но и подсказывать незачем.
# Для разработки включается переменной API_DOCS=1.
DOCS_ENABLED = os.getenv("API_DOCS") == "1"

app = FastAPI(
    title="ROZMART Mini App",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/docs" if DOCS_ENABLED else None,
    redoc_url="/redoc" if DOCS_ENABLED else None,
    openapi_url="/openapi.json" if DOCS_ENABLED else None,
)

app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")


@app.exception_handler(StarletteHTTPException)
async def localized_http_exception(request: Request, exc: StarletteHTTPException):
    """Ошибка для покупателя — на языке приложения (заголовок X-Lang).
    Сервер пишет их по-русски, узбекские варианты — в app/i18n.py."""
    from app import i18n

    lang = i18n.lang_of(request.headers.get("X-Lang"))
    if lang != "ru" and isinstance(exc.detail, str):
        exc = StarletteHTTPException(exc.status_code, i18n.translate(exc.detail, lang),
                                     headers=getattr(exc, "headers", None))
    return await http_exception_handler(request, exc)

app.include_router(admin_router)
app.include_router(admin_users_router)
app.include_router(bot_router)
app.include_router(catalog_router)
app.include_router(courier_router)
app.include_router(geocode_router)
app.include_router(orders_router)
app.include_router(payme_router)
app.include_router(paynet_router)
app.include_router(click_router)
app.include_router(profile_router)
app.include_router(shop_router)


@app.get("/", include_in_schema=False)
def index():
    """Клиентское приложение.

    Браузер внутри Telegram держит скрипт в кэше и после обновления показывает
    старую версию. Поэтому саму страницу запрещаем кэшировать, а к скрипту
    дописываем время его правки: у новой версии другой адрес, и кэш промахнётся.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = int((STATIC_DIR / "app.js").stat().st_mtime)
    return HTMLResponse(
        html.replace("__V__", str(version)),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/admin", include_in_schema=False)
def admin_page(request: Request, db: Session = Depends(get_db)):
    """Административная панель.

    Без действующей сессии отправляем на форму входа, а не показываем пустой
    каркас панели: данные он всё равно не получит (методы отвечают 401), но
    выглядит это так, будто внутрь пустили.

    Исключение — заход из Telegram: там вход подтверждается подписью в заголовке,
    а заголовки при обычном переходе по ссылке не отправляются, и проверить их
    здесь нечем. Поэтому витрина ведёт на /admin?tg=1, и такой переход
    пропускается: сама панель тут же проверит подпись и, если человек не
    администратор, ничего ему не покажет.
    """
    if request.query_params.get("tg") != "1" and admin_auth.session_user(request, db) is None:
        return RedirectResponse("/admin/login", status_code=303)
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    version = int((STATIC_DIR / "admin.js").stat().st_mtime)
    return HTMLResponse(
        html.replace("__V__", str(version)),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Иконка вкладки: браузер ищет её в корне сайта сам, на любой странице."""
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/admin/login", include_in_schema=False)
def admin_login_page():
    """Форма входа. Отдаётся всем: скрывать её незачем, доступ решает проверка
    пароля, а не невидимость страницы."""
    return HTMLResponse(
        (STATIC_DIR / "login.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/health")
def health():
    return {"status": "ok"}
