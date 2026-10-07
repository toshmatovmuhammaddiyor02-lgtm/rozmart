from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее время в UTC без пояса.

    В колонках лежит наивное время, и весь код считает его UTC. Полагаться на
    server_default=func.now() для этого нельзя: PostgreSQL возвращает время
    в поясе сервера, а он не обязан быть UTC — на боевой машине стоял
    Europe/Amsterdam, и заказы записывались на два часа вперёд.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, false, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Category(Base):
    """Категория витрины: копчёные колбасы, варёные, сосиски, деликатесы."""

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")


class Product(Base):
    """Витринная карточка товара. Цены и веса не содержит — они в фасовках."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    # RESTRICT: категорию с товарами удалить нельзя — иначе товары осиротеют
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    composition: Mapped[str | None] = mapped_column(Text)
    shelf_life: Mapped[str | None] = mapped_column(String(100))
    photo: Mapped[str | None] = mapped_column(String(200))
    # BR-34: товар из REGOS создаётся скрытым, публикуется после заполнения названия и фото
    is_active: Mapped[bool] = mapped_column(default=False, server_default=false())

    category: Mapped["Category"] = relationship()
    variants: Mapped[list["Variant"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="Variant.weight_grams",
    )


class Variant(Base):
    """Фасовка товара: конкретный вес со своей ценой и кодом в REGOS."""

    __tablename__ = "variants"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    external_code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    # группа номенклатуры в REGOS. Нужна, чтобы администратор мог отбирать
    # товары по группам учётной системы и убирать лишние из витрины
    regos_group_id: Mapped[int | None] = mapped_column(index=True)
    weight: Mapped[str] = mapped_column(String(50))
    # для сортировки фасовок в карточке: «455 г» строкой сортируется бессмысленно
    weight_grams: Mapped[int] = mapped_column(default=0, server_default="0")
    # BR-10: сумы целыми числами. NULL допустим — часть позиций REGOS не имеет
    # цены до получения прайс-листа. Дефолта 0 нет намеренно: ноль исказил бы
    # «цену от» в каталоге (BR-37). Фасовка без цены не публикуется.
    price: Mapped[int | None]
    # price — цена, по которой продаёт витрина. Её собирают из двух:
    # regos_price — как пришла из REGOS по выбранному виду цены;
    # manual_price — своя цена, заданная администратором. Если она есть,
    # действует она. Синхронизация обновляет только regos_price: ручная правка
    # иначе слетала бы через полчаса, при следующем проходе. См. app/regos/prices.py
    regos_price: Mapped[int | None]
    manual_price: Mapped[int | None]
    barcode: Mapped[str | None] = mapped_column(String(50))
    # Для фискального чека при оплате через Payme: код МХИК (ИКПУ), код
    # упаковки и НДС. Приходят из REGOS с синхронизацией каталога. Без них
    # чек не попадёт в налоговую — см. app/payme.py
    mxik: Mapped[str | None] = mapped_column(String(20))
    package_code: Mapped[str | None] = mapped_column(String(20))
    vat_percent: Mapped[int | None]
    is_active: Mapped[bool] = mapped_column(default=False, server_default=false())

    product: Mapped["Product"] = relationship(back_populates="variants")


class AdminUser(Base):
    """Администратор панели. Пароль хранится хешем PBKDF2, см. app/admin_auth.py."""

    __tablename__ = "admin_users"

    username: Mapped[str] = mapped_column(String(60), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AdminSession(Base):
    """Открытая сессия панели.

    Сессии лежат в базе, а не в подписанной куке, чтобы их можно было отозвать:
    смена пароля закрывает все открытые сессии, иначе украденная кука работала
    бы до истечения срока.
    """

    __tablename__ = "admin_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(60), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Setting(Base):
    """Настройки, которыми управляет администратор из панели.

    В переменных окружения им не место: там значения задаёт тот, у кого есть
    доступ к серверу, и на каждое изменение нужен перезапуск службы. Выбор
    групп номенклатуры, попадающих в витрину, меняет контент-менеджер.
    """

    __tablename__ = "settings"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Counter(Base):
    """Счётчик номеров заказов.

    Номер нельзя выводить из id строки: id известен только после вставки, а до
    этого в number приходится класть пустую строку. На SQLite это сходит с рук,
    потому что запись сериализуется, а на PostgreSQL два одновременных заказа
    сталкиваются на уникальном индексе по одной и той же пустой строке —
    и второй покупатель получает ошибку на ровном месте.

    Отдельный счётчик снимает проблему: номер выдаётся атомарно, до вставки,
    и заказ сразу пишется со своим окончательным номером (BR-05).
    """

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[int]


class Customer(Base):
    """Покупатель. Заводится при первом заказе, чтобы телефон и адрес
    не приходилось набирать заново каждый раз."""

    __tablename__ = "customers"

    # ключ — аккаунт Telegram, своих идентификаторов не заводим
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text)
    # дом, подъезд и квартира — отдельно, чтобы подставлять их в следующий заказ
    # и чтобы их не приходилось выискивать в одной длинной строке
    house: Mapped[str | None] = mapped_column(String(20))
    entrance: Mapped[str | None] = mapped_column(String(20))
    flat: Mapped[str | None] = mapped_column(String(20))
    # точка на карте, если покупатель её поставил. Адрес строкой она не заменяет:
    # по координатам курьер находит дом, а по тексту — подъезд и квартиру
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    # FR-1.13: согласие на обработку данных берётся один раз и фиксируется.
    # Поле обязательное — значит записи о покупателе без согласия не бывает
    consent_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class Order(Base):
    """Заказ клиента. Статусы — по машине состояний из спецификации, раздел 3.2."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # BR-05: номер уникален и не меняется, его видит клиент и по нему ищет оператор
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20))
    # ключ попытки оформления с телефона клиента. Повторная отправка того же
    # заказа (двойной тап, обрыв связи, ретрай) вернёт уже созданный заказ.
    client_key: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    # аккаунт Telegram, из которого оформили заказ: по нему покупатель видит
    # свои заказы. BigInteger — идентификаторы Telegram давно вышли за 2 млрд
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # @имя покупателя, если оно у него есть. Нужно оператору и курьеру: по нему
    # можно написать человеку в Telegram, когда он не берёт трубку. Имя можно
    # сменить в любой момент, поэтому оно сохраняется вместе с заказом — как
    # было на момент оформления, а не подтягивается заново
    telegram_username: Mapped[str | None] = mapped_column(String(64))

    customer_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(30))
    address: Mapped[str] = mapped_column(Text)
    # точка на карте на момент заказа. Профиль покупателя потом меняется,
    # а везти надо было туда, куда он показал при оформлении
    lat: Mapped[float | None]
    lon: Mapped[float | None]
    delivery_slot: Mapped[str] = mapped_column(String(60))
    comment: Mapped[str | None] = mapped_column(Text)
    payment_method: Mapped[str] = mapped_column(String(20))
    # способ онлайн-оплаты, выбранный покупателем: payme, paynet или telegram.
    # Только подсказка, какую оплату открыть: оплатить можно любым включённым
    pay_provider: Mapped[str | None] = mapped_column(String(20))

    # вид цены REGOS, по которому считался заказ. Уходит в документ REGOS:
    # администратор может переключить вид цены, а заказ должен остаться тем,
    # что видел покупатель
    price_type_id: Mapped[int | None]

    # BR-10: суммы целыми числами в сумах, зафиксированы на момент оформления
    goods_total: Mapped[int]
    delivery_price: Mapped[int]
    total: Mapped[int]

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, server_default=func.now()
    )

    # Оплата картой через Telegram Payments. Всё заполняется только по сообщению
    # об успешной оплате от Telegram — ни по слову приложения, ни по кнопке
    # оператора: правда о деньгах бывает только у платёжной системы.
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    # идентификатор платежа в Telegram. Уникален: повторная доставка того же
    # уведомления об оплате — обычное дело, и второй раз провести её нельзя
    payment_charge_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    # идентификатор у провайдера (Click) — по нему ищут платёж в их кабинете
    provider_charge_id: Mapped[str | None] = mapped_column(String(128))
    paid_amount: Mapped[int | None]
    # когда платёжная система последний раз спросила «можно ли оплатить».
    # Пока окно оплаты открыто, неоплаченный заказ не отменяем по таймауту:
    # иначе деньги спишутся за заказ, которого уже нет
    checkout_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Доставщик, который взял заказ в боте доставщиков (app/courier_bot.py).
    # Взять может только один: захват — условная запись «пока никто не взял».
    # courier_prev_status — в каком состоянии заказ был до захвата: отказавшись,
    # доставщик возвращает его туда, и заказ снова виден всем
    courier_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    courier_name: Mapped[str | None] = mapped_column(String(200))
    courier_taken_at: Mapped[datetime | None] = mapped_column(DateTime)
    courier_prev_status: Mapped[str | None] = mapped_column(String(20))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)
    # последний отказ доставщика: кто и почему. Причину он пишет в боте,
    # иначе отказ не проходит — магазину нужно знать, что с заказом не так
    courier_drop_note: Mapped[str | None] = mapped_column(Text)
    courier_drop_by: Mapped[str | None] = mapped_column(String(200))
    courier_drop_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Выгрузка в REGOS. Отправка идёт после оформления и вне запроса покупателя:
    # недоступная учётная система не должна мешать людям заказывать.
    # id документа — признак «уже выгружен», по нему же видно, что искать в REGOS.
    regos_document_id: Mapped[int | None] = mapped_column(index=True)
    regos_error: Mapped[str | None] = mapped_column(Text)
    regos_attempts: Mapped[int] = mapped_column(default=0, server_default="0")

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderItem(Base):
    """Позиция заказа. Наименование и цена сохраняются на момент заказа (BR-09):
    последующее изменение каталога не должно менять уже оформленный заказ."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    # RESTRICT: фасовку, по которой есть заказы, удалять нельзя
    variant_id: Mapped[int] = mapped_column(ForeignKey("variants.id", ondelete="RESTRICT"))
    product_name: Mapped[str] = mapped_column(String(200))
    weight: Mapped[str] = mapped_column(String(50))
    price: Mapped[int]
    quantity: Mapped[int]

    order: Mapped["Order"] = relationship(back_populates="items")
    # фасовка нужна, чтобы показать покупателю фото товара в его заказе.
    # Название и цена берутся из самой строки — они зафиксированы на момент
    # заказа (BR-09), а фото не фиксируем: это та же вещь, снятая заново
    variant: Mapped["Variant"] = relationship()


class PaynetTransaction(Base):
    """Платёж через Paynet (app/paynet.py).

    У Paynet платёж проводится одним вызовом — PerformTransaction, без
    «создать, потом провести», как у Payme. Состояния — по протоколу Paynet:
    1 проведён, 2 отменён. Неудачные попытки (не тот заказ, не та сумма)
    не записываются: Paynet повторяет их с тем же номером транзакции,
    и повтор должен пройти.
    """

    __tablename__ = "paynet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)        # наш providerTrnId
    # номер транзакции у Paynet: 11–20 цифр, храним строкой
    paynet_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    order_number: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(BigInteger)          # в тийинах
    state: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime)


class ClickTransaction(Base):
    """Платёж через Click SHOP API (app/click.py).

    Click спрашивает нас дважды: Prepare — можно ли оплатить, Complete —
    деньги списаны (или не списались). Наш id — merchant_prepare_id и он же
    merchant_confirm_id. Состояния: 0 подготовлен, 1 проведён, -1 отменён
    (Click сообщил об ошибке оплаты), -2 деньги пришли, а заказ их уже не
    ждал — платёж возвращаем, -3 возврат не удался, нужен человек.
    """

    __tablename__ = "click_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    click_trans_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # номер платежа в Click — по нему фискализация и возврат
    click_paydoc_id: Mapped[int | None] = mapped_column(BigInteger)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    order_number: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(BigInteger)          # в сумах
    state: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime)
    # чек в налоговую: None — не отправляли, ok, error; что ответил Click
    fiscal_status: Mapped[str | None] = mapped_column(String(10))
    fiscal_note: Mapped[str | None] = mapped_column(Text)
    fiscal_attempts: Mapped[int] = mapped_column(default=0)
    fiscal_at: Mapped[datetime | None] = mapped_column(DateTime)


class PaymeTransaction(Base):
    """Транзакция Payme (Merchant API).

    Payme ведёт платёж сам и спрашивает нас по шагам: можно ли, создай,
    проведи, отмени. Состояние транзакции — по протоколу Payme:
    1 создана, 2 проведена, -1 отменена до проведения, -2 отменена после.
    Времена — миллисекунды Unix, как их ждёт Payme в ответах.

    Отдельно от заказа: по одному заказу транзакций может быть несколько
    (покупатель бросил оплату и начал заново), а Payme сверяет каждую.
    """

    __tablename__ = "payme_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # идентификатор транзакции у Payme. Уникален: Payme повторяет запросы
    payme_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # SET NULL: удалённый заказ не должен уносить историю платежей
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    # номер заказа, как его прислал Payme, — на случай, если заказ удалят
    order_number: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(BigInteger)          # в тийинах
    payme_time: Mapped[int] = mapped_column(BigInteger)      # время у Payme
    create_time: Mapped[int] = mapped_column(BigInteger)
    perform_time: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_time: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[int]
    reason: Mapped[int | None]
    # Фискальный чек из налоговой — Payme присылает его методом SetFiscalData
    # после проведения (и отдельно на возврат). QR-ссылка ведёт на чек в ОФД
    fiscal_qr_url: Mapped[str | None] = mapped_column(Text)
    fiscal_data: Mapped[str | None] = mapped_column(Text)   # всё, что прислали, JSON


class Courier(Base):
    """Доставщик — человек, который написал боту доставщиков.

    Писать боту может кто угодно, поэтому запись сначала без доступа:
    в карточке заказа телефон и адрес покупателя. Доступ включает
    администратор в панели — так же, как у служебного бота.
    """

    __tablename__ = "couriers"

    # личный чат с ботом: его id совпадает с id пользователя Telegram
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str | None] = mapped_column(String(64))
    # язык Telegram у доставщика: "uz" или "ru". Нужен для сообщений не в ответ
    # на его действие — «заказ отменён» из панели или с кассы
    language: Mapped[str | None] = mapped_column(String(5))
    # доставщик нажал «Отменить» и бот ждёт от него причину: какой заказ
    # и какое сообщение с карточкой потом поправить
    pending_drop_order_id: Mapped[int | None]
    pending_drop_message_id: Mapped[int | None] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(default=False, server_default=false())
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StaffChat(Base):
    """Чат, куда уходят все заказы: личный чат сотрудника или общая группа.

    Отдельная таблица, а не флаг у подписчика: группа — это не человек,
    у неё нет telegram_id пользователя, а chat_id отрицательный. И права
    у этих двух подписок разные: покупателю приходит только его заказ,
    сюда — все, вместе с телефоном и адресом. Поэтому попасть сюда можно
    только по коду из админ-панели.
    """

    __tablename__ = "staff_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    title: Mapped[str | None] = mapped_column(String(200))
    # @имя, если оно есть: по нему администратор узнаёт человека в списке
    username: Mapped[str | None] = mapped_column(String(64))
    # 'private' — личный чат сотрудника, 'group' — общая группа
    kind: Mapped[str] = mapped_column(String(20), default="private")
    # язык сообщений: "ru" или "uz". В личном чате — по Telegram сотрудника,
    # в группе — того, кто добавил бота; переключается в панели
    language: Mapped[str | None] = mapped_column(String(5))
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(default=True, server_default=true())


class Notification(Base):
    """Очередь сообщений покупателю.

    Сообщение не отправляется прямо из обработчика запроса по двум причинам:
    недоступный Telegram задержал бы ответ покупателю, а перезапуск службы
    посреди отправки потерял бы сообщение молча. Строка живёт до отправки,
    отправляет её отдельный поток.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    # чат из staff_chats, куда уходит сообщение
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    # SET NULL: удаление заказа не должно уносить с собой историю переписки
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(30))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
