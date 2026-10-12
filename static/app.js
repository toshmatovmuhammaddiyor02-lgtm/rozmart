"use strict";

// Витрина каталога, корзина и оформление заказа.
// Данные берутся из /api, вёрстка — в index.html.

const state = {
  categories: [],
  allProducts: [],    // весь каталог: по нему ищем, не дёргая сервер
  categoryId: null,   // null — показываем все товары
  query: "",          // что набрали в поиске
  product: null,      // открытая карточка
  variant: null,      // выбранная фасовка
  qty: 1,
  cart: [],           // {variantId, productId, name, weight, price, qty}
  profile: null,      // имя, телефон, адрес покупателя; null — вне Telegram
  savingProfile: false,
  cartNotice: "",     // что изменилось в корзине, пока её не открывали
  clientKey: null,    // ключ попытки оформления, чтобы повтор не создал второй заказ
  scrollY: 0,         // позиция каталога, чтобы вернуть её после карточки
  deliveryPrice: 0,   // приходит с сервера, чтобы итог совпал с заказом
  slot: "Завтра до конца рабочего дня",
  payment: "cash",
  sending: false,     // защита от повторной отправки заказа
  orders: [],         // загруженные заказы покупателя
  cardAvailable: false,  // можно ли этому покупателю платить картой — решает сервер
  unpaidMinutes: 30,     // через сколько минут неоплаченный заказ отменяется
  paying: false,         // окно оплаты уже открывается
  point: { f: null, p: null },   // точка на карте: f — в заказе, p — в профиле
  zone: null,                    // доставляем ли в точку заказа: ответ /api/delivery-zone
  // адрес, который мы сами подставили в поле. Нужен, чтобы отличить его
  // от набранного руками и не затереть чужой текст
  filled: { f: "", p: "" },
};

// приложение сворачивают и открывают заново — корзина не должна пропадать
const CART_KEY = "rozmart.cart.v1";
const MAX_QTY = 99;   // столько же принимает сервер

// Срок доставки сейчас один: магазин возит на следующий рабочий день
const SLOTS = [
  ["Завтра до конца рабочего дня", "до 18:00"],
];
// Способы оплаты: наличные — всегда, онлайн — те, что сервер включил
// для этого покупателя (state.providers). Код — то, что уходит на сервер
const PAYMENTS = [
  ["cash", "pay_cash", "ti-cash"],
  ["payme", "pay_payme", "ti-credit-card"],
  ["paynet", "pay_paynet", "ti-wallet"],
  ["click", "pay_click", "ti-credit-card"],
  ["telegram", "pay_online", "ti-credit-card"],
];
const PROVIDER_NAME = { payme: "Payme", paynet: "Paynet", click: "Click", telegram: "Telegram" };
// Плитки «Онлайн оплаты»: показываются всегда. Не включённый для покупателя
// способ — бледный, с пометкой «Скоро»: нажал — объясняем, что пока наличными.
// Логотипы — надписи в цветах самих сервисов: файлов логотипов у нас нет
const ONLINE = ["payme", "click", "paynet"];
const LOGO = {
  payme: `<span class="lg lg-payme"><b>pay</b><i>me</i></span>`,
  paynet: `<span class="lg lg-paynet">paynet</span>`,
  click: `<span class="lg lg-click">click</span>`,
  telegram: `<span class="lg"><i class="ti ti-brand-telegram"></i></span>`,
};

// ---------- язык ----------

/* Два языка: русский и узбекский (латиницей, как в узбекском Telegram).
 * Выбирается сам по языку Telegram покупателя, поменять можно в профиле —
 * выбор запоминается на телефоне. Названия товаров, категорий, адреса
 * приходят из базы и не переводятся. Тексты ошибок сервер присылает на том
 * языке, который мы ему называем в заголовке X-Lang. */
const LANG_KEY = "rozmart.lang";
const I18N = {
  ru: {
    all_goods: "Все товары", all_chip: "Все", no_goods: "Товаров нет", found: "Найдено",
    nothing_found: "Ничего не нашлось. Попробуйте другое слово.",
    catalog_fail: "Не удалось загрузить каталог. Проверьте подключение к интернету.",
    category_fail: "Не удалось загрузить товары категории.",
    product_fail: "Не удалось открыть товар.",
    packaging: "Фасовка", category: "Категория", composition: "Состав",
    shelf_life: "Срок годности", quantity: "Количество", description: "Описание",
    to_cart: "В корзину",
    cart_gone: "Часть товаров больше не продаётся — мы убрали их из корзины.",
    prices_updated: "Цены обновились.", cart_empty: "Корзина пуста",
    cart_hint: "Состав можно менять до оформления заказа",
    goods_n: "Товары ({n})", goods: "Товары", delivery: "Доставка", total: "Итого",
    pay_cash: "Наличными курьеру", pay_online: "Онлайн картой",
    pay_payme: "Картой через Payme", pay_paynet: "Через Paynet", pay_click: "Через Click",
    pay_with: "Оплатить через {name}",
    pay_online_group: "Онлайн оплата", soon: "Скоро",
    soon_note: "Оплата через {name} скоро появится. Пока можно оплатить наличными курьеру.",
    pick_online: "Выберите, чем оплатить онлайн: Payme, Click или Paynet.",
    confirm_order: "Подтвердить заказ",
    fill_fields: "Заполните имя, номер телефона в формате +998 XX XXX XX XX и адрес доставки.",
    need_consent: "Отметьте согласие на обработку данных.",
    sending: "Отправляем…",
    prices_changed: "Цены изменились. Проверьте корзину и подтвердите заказ заново.",
    relaunch: "Закройте и откройте приложение заново, вход в Telegram устарел.",
    order_failed: "Не удалось оформить заказ. Проверьте данные и попробуйте ещё раз.",
    order_sent: "Заказ отправлен. Найдите его в «Моих заказах».",
    send_failed: "Не удалось отправить заказ. Проверьте связь и попробуйте ещё раз.",
    cash_t: "Заказ принят", cash_s: "Мы уже собираем ваш заказ.<br>Курьер свяжется перед доставкой.",
    paid_t: "Оплачено, заказ принят",
    paid_s: "Деньги получены, магазин начал сборку.<br>Курьеру платить не нужно.",
    awaiting_t: "Заказ ждёт оплаты",
    awaiting_s: "Оплатите картой — после этого магазин начнёт сборку.",
    checking_t: "Проверяем оплату…", checking_s: "Это займёт несколько секунд.",
    slow_t: "Оплата проверяется",
    slow_s: "Как только банк подтвердит платёж, статус обновится в «Моих заказах».",
    expired_t: "Заказ отменён", expired_s: "Время на оплату вышло. Оформите заказ заново.",
    late_t: "Заказ отменён, а оплата прошла",
    late_s: "Магазин свяжется с вами, чтобы вернуть деньги или восстановить заказ.",
    external_t: "Оплатите на странице {name}",
    external_s: "Когда оплатите, вернитесь сюда — статус обновится сам.",
    paynet_t: "Оплатите в Paynet",
    paynet_s: "Откройте приложение Paynet, найдите <b>ROZMART</b> и введите номер заказа "
      + "{order}. Сумма — {sum}.<br>Когда оплатите, вернитесь сюда — статус обновится сам.",
    copy_number: "Скопировать номер заказа", copied: "Номер заказа скопирован",
    open_paynet: "Открыть Paynet",
    sum_paid: "Оплачено", sum_due: "К оплате", sum_courier: "Оплата курьеру",
    pay: "Оплатить", i_paid: "Я оплатил — проверить", payme_again: "Открыть {name} ещё раз",
    order_number: "Номер заказа",
    forget_q: "Удалить имя, телефон и адрес? Уже оформленные заказы останутся.",
    forget_fail: "Не удалось удалить. Проверьте связь.",
    house_n: "дом {n}", set_address: "Укажите адрес", deliver_time: "Доставим за 60–90 минут",
    addr_hint: "Чтобы не набирать при заказе",
    save: "Сохранить", saving: "Сохраняем…",
    save_fail: "Не удалось сохранить. Проверьте данные и связь.",
    loading: "Загружаем…", no_orders: "Вы ещё ничего не заказывали",
    orders_fail: "Не удалось загрузить заказы.", more: "подробнее ›",
    order_n: "Заказ {n}",
    pm_cash: "наличными курьеру", pm_paid: "картой — оплачено",
    pm_unpaid: "картой — не оплачен", pm_waiting: "картой — ждёт оплаты",
    order_items: "Состав заказа", where: "Куда", phone: "Телефон", time: "Время",
    payment: "Оплата", comment: "Комментарий",
    pay_until: "Без оплаты заказ отменится в {t}.",
    map_fail: "Карта не загрузилась. Проверьте связь или напишите адрес словами.",
    geo_settings: "Доступ к геопозиции для приложения выключен. Открыть настройки?",
    geo_fail: "Не удалось определить местоположение. Найдите дом на карте руками.",
    geo_phone: "Телефон не дал определить местоположение. Найдите дом на карте руками.",
    geo_denied: "Доступ к геопозиции запрещён. Разрешите его в настройках телефона "
      + "или найдите дом на карте руками.",
    addr_lookup: "Определяем адрес…",
    addr_unknown: "Адрес не определился — напишите его словами",
    map_hint: "Подвиньте карту так, чтобы булавка встала на ваш дом.",
    addr_house: "{a}, дом {h}",
    point_ok: "Адрес и дом взяты с карты", point_change: "изменить точку", point_drop: "убрать",
    tg_only: "Оплатить картой можно только в приложении Telegram.",
    tg_update: "Обновите Telegram, чтобы оплатить картой.",
    pay_open_fail: "Не удалось открыть оплату. Попробуйте ещё раз.",
    not_yet: "Оплата ещё не поступила. Если вы уже оплатили — подождите минуту.",
    pay_failed: "Оплата не прошла. Можно попробовать ещё раз.",
    admin_panel: "Панель",
    // разметка index.html
    search_ph: "Поиск по каталогу", product_details: "Детали товара", cart: "Корзина",
    to_checkout: "Оформить заказ", checkout: "Оформление заказа", your_name: "Ваше имя",
    name_ph: "Как к вам обращаться", phone_l: "Номер телефона", addr_l: "Адрес доставки",
    addr_ph: "Район, улица, дом, квартира", on_map: "Указать на карте", house: "Дом",
    entrance: "Подъезд", flat: "Квартира", comment_l: "Комментарий к заказу",
    comment_ph: "Например: позвонить за 10 минут", delivery_time: "Время доставки",
    pay_method: "Способ оплаты",
    consent: "Согласен на обработку моих данных — имени, телефона и адреса — для доставки заказа",
    profile: "Профиль", name: "Имя",
    profile_note: "Эти данные подставятся в следующий заказ. Уже оформленные не меняются.",
    forget: "Удалить мои данные", my_orders: "Мои заказы", back_catalog: "Вернуться в каталог",
    order: "Заказ", map_title: "Точка на карте", satellite: "Спутник", scheme: "Схема",
    my_location: "Моё местоположение", done_btn: "Готово", tab_catalog: "Каталог",
    tab_orders: "Заказы", language: "Язык",
  },
  uz: {
    all_goods: "Barcha mahsulotlar", all_chip: "Barchasi", no_goods: "Mahsulotlar yo'q",
    found: "Topildi",
    nothing_found: "Hech narsa topilmadi. Boshqa so'z bilan urinib ko'ring.",
    catalog_fail: "Katalogni yuklab bo'lmadi. Internet aloqasini tekshiring.",
    category_fail: "Kategoriya mahsulotlarini yuklab bo'lmadi.",
    product_fail: "Mahsulotni ochib bo'lmadi.",
    packaging: "Qadoq", category: "Kategoriya", composition: "Tarkibi",
    shelf_life: "Yaroqlilik muddati", quantity: "Miqdori", description: "Tavsif",
    to_cart: "Savatga",
    cart_gone: "Ba'zi mahsulotlar endi sotilmaydi — ularni savatdan olib tashladik.",
    prices_updated: "Narxlar yangilandi.", cart_empty: "Savat bo'sh",
    cart_hint: "Buyurtma berilgunga qadar tarkibini o'zgartirish mumkin",
    goods_n: "Mahsulotlar ({n})", goods: "Mahsulotlar", delivery: "Yetkazib berish", total: "Jami",
    pay_cash: "Kuryerga naqd pul", pay_online: "Karta orqali onlayn",
    pay_payme: "Payme orqali karta bilan", pay_paynet: "Paynet orqali", pay_click: "Click orqali",
    pay_with: "{name} orqali to'lash",
    pay_online_group: "Onlayn to'lov", soon: "Tez orada",
    soon_note: "{name} orqali to'lov tez orada qo'shiladi. Hozircha kuryerga naqd pul bilan "
      + "to'lashingiz mumkin.",
    pick_online: "Onlayn to'lov usulini tanlang: Payme, Click yoki Paynet.",
    confirm_order: "Buyurtmani tasdiqlash",
    fill_fields: "Ism, +998 XX XXX XX XX formatidagi telefon raqami va yetkazib berish "
      + "manzilini kiriting.",
    need_consent: "Ma'lumotlarni qayta ishlashga roziligingizni belgilang.",
    sending: "Yuborilmoqda…",
    prices_changed: "Narxlar o'zgardi. Savatni tekshirib, buyurtmani qayta tasdiqlang.",
    relaunch: "Ilovani yopib, qayta oching — Telegram orqali kirish eskirgan.",
    order_failed: "Buyurtma berib bo'lmadi. Ma'lumotlarni tekshirib, qayta urinib ko'ring.",
    order_sent: "Buyurtma yuborildi. Uni «Buyurtmalarim» bo'limida toping.",
    send_failed: "Buyurtmani yuborib bo'lmadi. Aloqani tekshirib, qayta urinib ko'ring.",
    cash_t: "Buyurtma qabul qilindi",
    cash_s: "Buyurtmangizni yig'ishni boshladik.<br>Kuryer yetkazishdan oldin bog'lanadi.",
    paid_t: "To'landi, buyurtma qabul qilindi",
    paid_s: "Pul qabul qilindi, do'kon yig'ishni boshladi.<br>Kuryerga to'lash shart emas.",
    awaiting_t: "Buyurtma to'lovni kutmoqda",
    awaiting_s: "Karta orqali to'lang — shundan so'ng do'kon yig'ishni boshlaydi.",
    checking_t: "To'lov tekshirilmoqda…", checking_s: "Bu bir necha soniya oladi.",
    slow_t: "To'lov tekshirilmoqda",
    slow_s: "Bank to'lovni tasdiqlashi bilan «Buyurtmalarim» bo'limida holat yangilanadi.",
    expired_t: "Buyurtma bekor qilindi",
    expired_s: "To'lov vaqti tugadi. Buyurtmani qaytadan bering.",
    late_t: "Buyurtma bekor qilingan, lekin to'lov o'tdi",
    late_s: "Do'kon pulni qaytarish yoki buyurtmani tiklash uchun siz bilan bog'lanadi.",
    external_t: "{name} sahifasida to'lang",
    external_s: "To'lagandan so'ng shu yerga qayting — holat o'zi yangilanadi.",
    paynet_t: "Paynet'da to'lang",
    paynet_s: "Paynet ilovasini oching, <b>ROZMART</b>ni toping va buyurtma raqamini kiriting: "
      + "{order}. Summa — {sum}.<br>To'lagandan so'ng shu yerga qayting — holat o'zi yangilanadi.",
    copy_number: "Buyurtma raqamini nusxalash", copied: "Buyurtma raqami nusxalandi",
    open_paynet: "Paynet'ni ochish",
    sum_paid: "To'landi", sum_due: "To'lanadi", sum_courier: "Kuryerga to'lov",
    pay: "To'lash", i_paid: "To'ladim — tekshirish", payme_again: "{name}'ni qayta ochish",
    order_number: "Buyurtma raqami",
    forget_q: "Ism, telefon va manzil o'chirilsinmi? Berilgan buyurtmalar saqlanib qoladi.",
    forget_fail: "O'chirib bo'lmadi. Aloqani tekshiring.",
    house_n: "{n}-uy", set_address: "Manzilni kiriting",
    deliver_time: "60–90 daqiqada yetkazamiz",
    addr_hint: "Buyurtmada qayta yozmaslik uchun",
    save: "Saqlash", saving: "Saqlanmoqda…",
    save_fail: "Saqlab bo'lmadi. Ma'lumotlar va aloqani tekshiring.",
    loading: "Yuklanmoqda…", no_orders: "Siz hali hech narsa buyurtma qilmagansiz",
    orders_fail: "Buyurtmalarni yuklab bo'lmadi.", more: "batafsil ›",
    order_n: "Buyurtma {n}",
    pm_cash: "kuryerga naqd pul", pm_paid: "karta orqali — to'langan",
    pm_unpaid: "karta orqali — to'lanmagan", pm_waiting: "karta orqali — to'lov kutilmoqda",
    order_items: "Buyurtma tarkibi", where: "Manzil", phone: "Telefon", time: "Vaqt",
    payment: "To'lov", comment: "Izoh",
    pay_until: "To'lanmasa, buyurtma soat {t} da bekor qilinadi.",
    map_fail: "Xarita yuklanmadi. Aloqani tekshiring yoki manzilni so'z bilan yozing.",
    geo_settings: "Ilova uchun geolokatsiyaga ruxsat o'chirilgan. Sozlamalarni ochaymi?",
    geo_fail: "Joylashuvni aniqlab bo'lmadi. Uyingizni xaritada o'zingiz toping.",
    geo_phone: "Telefon joylashuvni aniqlashga ruxsat bermadi. Uyingizni xaritada o'zingiz toping.",
    geo_denied: "Geolokatsiyaga ruxsat berilmagan. Telefon sozlamalarida ruxsat bering "
      + "yoki uyingizni xaritada o'zingiz toping.",
    addr_lookup: "Manzil aniqlanmoqda…",
    addr_unknown: "Manzil aniqlanmadi — uni so'z bilan yozing",
    map_hint: "Xaritani surib, belgini uyingiz ustiga qo'ying.",
    addr_house: "{a}, {h}-uy",
    point_ok: "Manzil va uy xaritadan olindi", point_change: "nuqtani o'zgartirish",
    point_drop: "olib tashlash",
    tg_only: "Karta orqali faqat Telegram ilovasida to'lash mumkin.",
    tg_update: "Karta orqali to'lash uchun Telegram'ni yangilang.",
    pay_open_fail: "To'lovni ochib bo'lmadi. Qayta urinib ko'ring.",
    not_yet: "To'lov hali tushmadi. Agar to'lagan bo'lsangiz — bir daqiqa kuting.",
    pay_failed: "To'lov o'tmadi. Qayta urinib ko'rishingiz mumkin.",
    admin_panel: "Panel",
    search_ph: "Katalog bo'yicha qidiruv", product_details: "Mahsulot haqida", cart: "Savat",
    to_checkout: "Buyurtma berish", checkout: "Buyurtmani rasmiylashtirish", your_name: "Ismingiz",
    name_ph: "Sizga qanday murojaat qilaylik", phone_l: "Telefon raqami",
    addr_l: "Yetkazib berish manzili", addr_ph: "Tuman, ko'cha, uy, xonadon",
    on_map: "Xaritada ko'rsatish", house: "Uy", entrance: "Podyezd", flat: "Xonadon",
    comment_l: "Buyurtmaga izoh", comment_ph: "Masalan: 10 daqiqa oldin qo'ng'iroq qiling",
    delivery_time: "Yetkazib berish vaqti", pay_method: "To'lov usuli",
    consent: "Buyurtmani yetkazib berish uchun ma'lumotlarim — ism, telefon va manzil — "
      + "qayta ishlanishiga roziman",
    profile: "Profil", name: "Ism",
    profile_note: "Bu ma'lumotlar keyingi buyurtmaga qo'yiladi. Berilgan buyurtmalar o'zgarmaydi.",
    forget: "Ma'lumotlarimni o'chirish", my_orders: "Buyurtmalarim",
    back_catalog: "Katalogga qaytish", order: "Buyurtma", map_title: "Xaritadagi nuqta",
    satellite: "Sun'iy yo'ldosh", scheme: "Sxema", my_location: "Mening joylashuvim",
    done_btn: "Tayyor", tab_catalog: "Katalog", tab_orders: "Buyurtmalar", language: "Til",
  },
};

// Значения срока доставки уходят на сервер по-русски — переводим только подпись
const SLOT_TEXT = {
  uz: { "Как можно скорее": "Imkon qadar tezroq", "Сегодня вечером": "Bugun kechqurun",
        "Завтра утром": "Ertaga ertalab", "60–90 минут": "60–90 daqiqa",
        "Завтра до конца рабочего дня": "Ertaga ish kuni oxirigacha", "до 18:00": "18:00 gacha" },
};
// Состояния заказа: сервер присылает код, название берём своё
const STATUS_TEXT = {
  ru: { NEW: "Ждёт оплаты", CONFIRMED: "Принят", ASSEMBLING: "Собирается",
        DELIVERING: "В доставке", DONE: "Выполнен", CANCELED: "Отменён" },
  uz: { NEW: "To'lov kutilmoqda", CONFIRMED: "Qabul qilindi", ASSEMBLING: "Yig'ilmoqda",
        DELIVERING: "Yetkazilmoqda", DONE: "Bajarildi", CANCELED: "Bekor qilindi" },
};

function detectLang() {
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved === "ru" || saved === "uz") return saved;
  } catch (error) {
    console.error(error);   // приватный режим — определим по Telegram
  }
  const user = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initDataUnsafe
    && window.Telegram.WebApp.initDataUnsafe.user;
  return user && /^uz/i.test(user.language_code || "") ? "uz" : "ru";
}

let LANG = detectLang();

function t(key, vars = {}) {
  const text = (I18N[LANG] && I18N[LANG][key]) || I18N.ru[key] || key;
  return text.replace(/\{(\w+)\}/g, (_, name) => (name in vars ? vars[name] : ""));
}

const slotText = (value) => (SLOT_TEXT[LANG] && SLOT_TEXT[LANG][value]) || value;
const statusText = (order) => STATUS_TEXT[LANG][order.status] || order.status_text;
const locale = () => (LANG === "uz" ? "uz-UZ" : "ru-RU");

/** «3 товара» / «3 ta mahsulot»: в узбекском множественного числа после
 *  числительного нет. kind — "goods" или "positions". */
function countText(n, kind = "goods") {
  if (LANG === "uz") return `${n} ta ${kind === "goods" ? "mahsulot" : "pozitsiya"}`;
  return `${n} ${kind === "goods" ? plural(n) : plural(n, "позиция", "позиции", "позиций")}`;
}

/** Тексты разметки: data-i18n — текст, data-i18n-ph — подсказка в поле,
 *  data-i18n-title — всплывающая подпись. */
function applyStatic() {
  document.documentElement.lang = LANG;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-ph]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPh);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
  document.querySelectorAll("[data-lang]").forEach((node) => {
    node.classList.toggle("on", node.dataset.lang === LANG);
  });
}

// каждый асинхронный запрос получает номер: ответ старого запроса не должен
// перерисовать экран поверх нового (быстрые тапы по категориям)
let requestId = 0;

const el = (id) => document.getElementById(id);

// данные приходят из админки, куда их вводит человек: экранируем всё,
// что подставляем в разметку, иначе кавычка или тег ломают страницу
const esc = (value) =>
  String(value).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function money(value) {
  return value.toLocaleString("ru-RU").replace(/\s/g, " ") + " UZS";
}

/** Подписанные данные Telegram. Сервер проверяет подпись сам — здесь мы их
 *  только передаём, доверять клиентской копии нельзя. */
function tgHeaders() {
  const tg = window.Telegram && window.Telegram.WebApp;
  // язык — чтобы сервер ответил на нём же: тексты ошибок для покупателя
  const headers = { "X-Lang": LANG };
  if (tg && tg.initData) headers["X-Telegram-Init-Data"] = tg.initData;
  return headers;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method || "GET",
    headers: options.body
      ? { "Content-Type": "application/json", ...tgHeaders() }
      : tgHeaders(),
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (response.status === 204) return null;   // удаление, отвечать нечем
  if (!response.ok) {
    const error = new Error(`${path} -> ${response.status}`);
    error.status = response.status;   // 404 «товара больше нет» отличаем от обрыва связи
    // текст сервера показываем только там, где он написан для покупателя
    const body = response.status < 500 ? await response.json().catch(() => null) : null;
    error.detail = body && typeof body.detail === "string" ? body.detail : null;
    throw error;
  }
  return response.json();
}

const PHOTO_STUB = `<i class="ti ti-photo" style="font-size:22px"></i>`;

function photo(url, height) {
  const inner = url ? `<img src="${esc(url)}" alt="">` : PHOTO_STUB;
  return `<div class="ph" style="height:${height}px">${inner}</div>`;
}

function fail(message, error) {
  console.error(error);
  el("products").innerHTML = `<div class="msg" style="grid-column:1/-1">${esc(message)}</div>`;
  el("list-count").textContent = "";
}

// битая картинка заменяется той же заглушкой, что и отсутствующая.
// Слушатель в фазе перехвата: событие error по картинкам не всплывает.
document.addEventListener(
  "error",
  (event) => {
    const img = event.target;
    if (img.tagName !== "IMG") return;
    // .ph — служебное место под фото, его заменяем целиком; на плитке категории
    // картинка лежит рядом с названием, там убираем только её
    if (img.parentElement && img.parentElement.classList.contains("ph")) {
      img.parentElement.innerHTML = PHOTO_STUB;
    } else {
      img.remove();
    }
  },
  true,
);

// ---------- каталог ----------

async function loadCatalog() {
  try {
    const [categories, products] = await Promise.all([
      api("/api/categories"),
      api("/api/products"),
    ]);
    state.categories = categories;
    state.allProducts = products;   // по этому списку работает поиск, без запросов

    renderCategories();
    // список рисуем с оглядкой на то, что покупатель уже выбрал: при обновлении
    // страницы он стоит в своей категории, и показывать ему весь каталог —
    // значит терять место, на котором он был
    showCurrentList();
  } catch (error) {
    fail(t("catalog_fail"), error);
  }
}

/** Показывает то, что покупатель выбрал сейчас: категорию или поиск.
 *
 * Нужна там, где список перерисовывается не по его действию, а сам: после
 * обновления каталога свайпом. Отдельного запроса по категории не делаем —
 * товар приносит с собой category_id, и весь каталог уже в памяти.
 */
function showCurrentList() {
  if (state.query) return search(state.query);

  const category = state.categories.find((c) => c.id === state.categoryId);
  if (state.categoryId !== null && !category) {
    // категорию убрали из витрины, пока покупатель в ней стоял
    state.categoryId = null;
    renderCategories();
  }
  const products = state.categoryId === null
    ? state.allProducts
    : state.allProducts.filter((p) => p.category_id === state.categoryId);

  el("list-title").textContent = category ? category.name : t("all_goods");
  renderProducts(products);
}

/** Лента категорий. Выбранная подсвечена, «Все» сбрасывает отбор. */
function renderCategories() {
  const all = `<span class="chip plain ${state.categoryId === null ? "on" : ""}"
      data-category="0">${t("all_chip")}</span>`;
  el("categories").innerHTML = all + state.categories
    .map((c) => `
      <span class="chip ${c.id === state.categoryId ? "on" : ""}" data-category="${c.id}">
        ${c.photo ? `<img src="${esc(c.photo)}" alt="">` : ""}${esc(c.name)}
      </span>`)
    .join("");
}

function renderProducts(products, empty = t("no_goods")) {
  el("list-count").textContent = countText(products.length);
  if (!products.length) {
    el("products").innerHTML = `<div class="msg" style="grid-column:1/-1">${esc(empty)}</div>`;
    return;
  }
  el("products").innerHTML = products
    .map((p) => {
      // «от 45 000» / «45 000 dan»: в узбекском предлог встаёт после числа
      const from = p.weights.length > 1;
      return `
        <div class="pcard" data-product="${p.id}">
          ${photo(p.photo, 128)}
          <div class="nm" style="margin-top:8px">${esc(p.name)}</div>
          <div class="sub">${esc(p.weights.join(" · "))}</div>
          <div class="row">
            <span class="prc" style="font-size:13.5px">${from && LANG === "ru" ? "от " : ""}${money(p.min_price)}${from && LANG === "uz" ? " dan" : ""}</span>
            <span class="qb" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:15px"></i></span>
          </div>
        </div>`;
    })
    .join("");
}

function plural(n, one = "товар", few = "товара", many = "товаров") {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/** «Ё» и «е» покупатель набирает как придётся, а товары у нас с «ё». */
const normalize = (text) => text.toLowerCase().replace(/ё/g, "е");

/** Убирает поиск, список не трогает: его рисует тот, кто вызвал. */
function clearSearch() {
  state.query = "";
  el("q").value = "";
  el("q-clear").classList.add("hidden");
}

/** Поиск идёт по уже загруженному каталогу: 24 товара, запрос к серверу лишний. */
function search(query) {
  // иначе запоздавший ответ по категории ляжет поверх результатов поиска
  requestId += 1;
  state.query = query.trim();
  el("q-clear").classList.toggle("hidden", !query);
  if (!state.allProducts.length) return;   // каталог не загрузился, искать не в чем

  if (state.categoryId !== null) {
    state.categoryId = null;   // ищем по всему каталогу, а не внутри категории
    renderCategories();        // перерисовываем ленту только когда она изменилась
  }
  if (!state.query) {
    el("list-title").textContent = t("all_goods");
    return renderProducts(state.allProducts);
  }

  const needle = normalize(state.query);
  el("list-title").textContent = t("found");
  renderProducts(
    state.allProducts.filter((p) => normalize(p.name).includes(needle)),
    t("nothing_found"),
  );
}

async function selectCategory(categoryId) {
  // выбрали категорию — поиск больше не действует, иначе список и поле врут
  if (state.query) clearSearch();
  // 0 — чип «Все»; повторное нажатие на выбранную категорию тоже сбрасывает отбор
  const next = categoryId === 0 || state.categoryId === categoryId ? null : categoryId;
  const my = ++requestId;
  try {
    const products = await api(`/api/products${next ? `?category_id=${next}` : ""}`);
    if (my !== requestId) return;   // пока ждали, пользователь выбрал другое
    state.categoryId = next;
    const category = state.categories.find((c) => c.id === next);
    el("list-title").textContent = category ? category.name : t("all_goods");
    renderCategories();
    renderProducts(products);
    window.scrollTo({ top: 0, behavior: "smooth" });   // чтобы лента осталась на виду
  } catch (error) {
    if (my === requestId) fail(t("category_fail"), error);
  }
}

// ---------- карточка товара ----------

async function openProduct(productId) {
  const my = ++requestId;
  try {
    const product = await api(`/api/products/${productId}`);
    if (my !== requestId) return;
    state.scrollY = window.scrollY;
    state.product = product;
    state.variant = product.variants[0];
    state.qty = 1;
    renderProduct();
    show("product");
  } catch (error) {
    if (my === requestId) fail(t("product_fail"), error);
  }
}

function renderProduct() {
  const p = state.product;
  const options = p.variants.length > 1
    ? `<div class="mut" style="margin:16px 0 8px">${t("packaging")}</div>
       <div style="display:flex;gap:8px;flex-wrap:wrap">
         ${p.variants.map((v) => `
           <span class="wopt ${v.id === state.variant.id ? "on" : ""}" data-variant="${v.id}">${esc(v.weight)}</span>`).join("")}
       </div>`
    : "";

  const extra = [
    [t("category"), p.category],
    [t("composition"), p.composition],
    [t("shelf_life"), p.shelf_life],
  ].filter(([, value]) => value);

  el("product-body").innerHTML = `
    ${photo(p.photo, 230)}
    <div class="card" style="margin-top:16px">
      <div style="font-size:19px;font-weight:600;line-height:1.3;margin-bottom:5px">${esc(p.name)}</div>
      <div class="row">
        <span class="mut">${esc(state.variant.weight)}</span>
        <span class="prc" style="font-size:21px" id="variant-price">${money(state.variant.price)}</span>
      </div>
      ${options}
      <div class="row" style="margin-top:16px">
        <span class="mut">${t("quantity")}</span>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="qb" id="minus" style="background:#F1F1F1"><i class="ti ti-minus" style="font-size:15px"></i></span>
          <span style="font-size:16px;min-width:22px;text-align:center" id="qty-value">${state.qty}</span>
          <span class="qb" id="plus" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:15px"></i></span>
        </div>
      </div>
    </div>
    ${p.description ? `<div class="card"><div class="mut" style="margin-bottom:6px">${t("description")}</div><div style="line-height:1.6">${esc(p.description)}</div></div>` : ""}
    ${extra.length ? `<div class="card" style="background:#F7F7F7">${extra.map(([k, v], i) => `
      <div class="row" ${i < extra.length - 1 ? 'style="margin-bottom:9px"' : ""}>
        <span class="mut">${esc(k)}</span><span style="font-size:13px;text-align:right">${esc(v)}</span></div>`).join("")}</div>` : ""}
  `;
  updateTotal();
}

/** Меняем только цифры: полный ре-рендер моргал бы фотографией на каждый «+». */
function updateTotal() {
  el("qty-value").textContent = state.qty;
  el("add").textContent = `${t("to_cart")} · ${money(state.variant.price * state.qty)}`;
}

// ---------- корзина ----------

function persistCart() {
  try {
    localStorage.setItem(CART_KEY, JSON.stringify({ cart: state.cart, clientKey: state.clientKey }));
  } catch (error) {
    console.error(error);   // приватный режим: корзина просто не переживёт перезапуск
  }
}

/** Состав изменился — значит это уже другой заказ, и ключ прошлой попытки
 *  оформления больше не годится. */
function saveCart() {
  state.clientKey = null;
  persistCart();
}

function loadSavedCart() {
  try {
    const saved = JSON.parse(localStorage.getItem(CART_KEY) || "null");
    if (!saved || !Array.isArray(saved.cart)) return;
    state.cart = saved.cart.filter((i) => i && i.variantId && i.productId && i.qty > 0);
    state.clientKey = saved.clientKey || null;
  } catch (error) {
    console.error(error);
  }
}

function addToCart() {
  const line = state.cart.find((item) => item.variantId === state.variant.id);
  if (line) {
    line.qty = Math.min(MAX_QTY, line.qty + state.qty);
  } else {
    state.cart.push({
      variantId: state.variant.id,
      productId: state.product.id,
      name: state.product.name,
      weight: state.variant.weight,
      price: state.variant.price,
      qty: state.qty,
    });
  }
  saveCart();
  show("catalog");
}

/** Корзина лежит на телефоне и могла пролежать до изменения каталога: перед
 *  показом сверяем цены и наличие, чтобы покупатель видел то же, что и сервер. */
/** Стоимость доставки с сервера: её меняют в панели. */
async function loadDeliveryPrice() {
  try {
    const { delivery_price: price } = await api("/api/delivery-price");
    state.deliveryPrice = price;
  } catch (error) {
    console.error(error);   // нет связи — остаётся прежняя, сервер всё равно сверит итог
  }
}

async function refreshCart() {
  if (!state.cart.length) return;
  // Заодно доставка: если её изменили в панели, пока корзина была открыта,
  // сервер откажет «цены изменились» — и здесь покупатель увидит новый итог
  await loadDeliveryPrice();
  const my = ++requestId;
  const ids = [...new Set(state.cart.map((i) => i.productId))];
  let loaded;
  try {
    loaded = await Promise.all(
      ids.map((id) =>
        api(`/api/products/${id}`).catch((e) => (e.status === 404 ? null : Promise.reject(e)))),
    );
  } catch (error) {
    console.error(error);   // связи нет — показываем как есть, сервер всё равно пересчитает
    return;
  }
  if (my !== requestId) return;   // пока сверяли, корзину открыли заново или ушли с неё

  const actual = new Map();
  for (const product of loaded.filter(Boolean)) {
    for (const v of product.variants) {
      actual.set(v.id, { name: product.name, weight: v.weight, price: v.price });
    }
  }

  const kept = state.cart.filter((i) => actual.has(i.variantId));
  const gone = state.cart.length - kept.length;
  const changed = kept.filter((i) => i.price !== actual.get(i.variantId).price).length;
  for (const line of kept) Object.assign(line, actual.get(line.variantId));
  state.cart = kept;

  state.cartNotice = gone
    ? t("cart_gone")
    : changed
      ? t("prices_updated")
      : "";
  if (gone || changed) saveCart();
}

const goodsTotal = () => state.cart.reduce((sum, i) => sum + i.price * i.qty, 0);
const cartCount = () => state.cart.reduce((sum, i) => sum + i.qty, 0);

function renderCartBadge() {
  const count = cartCount();
  el("tab-count").textContent = count;
  el("tab-count").classList.toggle("hidden", count === 0);
}

function renderCart() {
  renderCartBadge();   // счётчик на вкладке показывает то же число, что и этот экран
  const notice = state.cartNotice
    ? `<div class="err" style="margin:0 0 10px">${esc(state.cartNotice)}</div>`
    : "";
  el("to-checkout").disabled = !state.cart.length;

  if (!state.cart.length) {
    el("cart-body").innerHTML = notice + `<div class="msg">${t("cart_empty")}</div>`;
    return;
  }

  const lines = state.cart
    .map((item, index) => `
      <div class="card">
        <div class="line">
          <div style="flex:1">
            <div style="font-size:13px;line-height:1.35">${esc(item.name)} · ${esc(item.weight)}</div>
            <div class="prc" style="font-size:13px;margin-top:3px">${money(item.price)}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span class="qb" data-cart-minus="${index}" style="background:#F1F1F1"><i class="ti ti-minus" style="font-size:14px"></i></span>
            <span style="font-size:15px;min-width:20px;text-align:center">${item.qty}</span>
            <span class="qb" data-cart-plus="${index}" style="background:#E30613;color:#fff"><i class="ti ti-plus" style="font-size:14px"></i></span>
          </div>
          <i class="ti ti-trash del" data-cart-del="${index}"></i>
        </div>
      </div>`)
    .join("");

  el("cart-body").innerHTML = notice + lines + `<div class="card">${totalsRows()}</div>` + `
    <div class="mut" style="font-size:12px;text-align:center;line-height:1.6">
      <i class="ti ti-info-circle"></i> ${t("cart_hint")}
    </div>`;
}

/** Строки «товары / доставка / итого». Обёртку-карточку добавляет вызывающий. */
function totalsRows() {
  const goods = goodsTotal();
  return `
    <div class="row" style="margin-bottom:9px"><span class="mut">${t("goods_n", { n: cartCount() })}</span>
      <span style="font-size:13px">${money(goods)}</span></div>
    <div class="row" style="margin-bottom:11px"><span class="mut">${t("delivery")}</span>
      <span style="font-size:13px">${money(state.deliveryPrice)}</span></div>
    <div class="row" style="border-top:1px solid #EDEDED;padding-top:11px">
      <span style="font-size:15px;font-weight:600">${t("total")}</span>
      <span class="prc" style="font-size:17px">${money(goods + state.deliveryPrice)}</span></div>`;
}

// ---------- оформление ----------

function renderCheckout() {
  el("slots").innerHTML = SLOTS.map(([name, hint]) => `
    <div class="opt ${state.slot === name ? "on" : ""}" data-slot="${esc(name)}">
      <div><div style="font-size:14px">${esc(slotText(name))}</div>
        <div class="mut" style="font-size:11px">${esc(slotText(hint))}</div></div>
      <i class="ti ${state.slot === name ? "ti-circle-check" : "ti-circle"}"></i>
    </div>`).join("");

  // «Онлайн оплата» выбрана — способом (payme, paynet) или пока без него
  const online = state.payment !== "cash";
  const tiles = [...ONLINE, ...(state.providers || []).filter((p) => !ONLINE.includes(p))];
  el("payments").innerHTML = `
    <div class="opt ${!online ? "on" : ""}" data-payment="cash">
      <span style="font-size:14px"><i class="ti ti-cash" style="color:#E30613"></i> ${esc(t("pay_cash"))}</span>
      <i class="ti ${!online ? "ti-circle-check" : "ti-circle"}"></i>
    </div>
    <div class="opt ${online ? "on" : ""}" data-payment="online">
      <span style="font-size:14px"><i class="ti ti-credit-card" style="color:#E30613"></i> ${esc(t("pay_online_group"))}</span>
      <i class="ti ${online ? "ti-circle-check" : "ti-circle"}"></i>
    </div>
    ${online ? `<div class="ptiles">${tiles.map((p) => {
      const ready = (state.providers || []).includes(p);
      return `<div class="ptile ${state.payment === p ? "on" : ""} ${ready ? "" : "soon"}"
          data-pick="${esc(p)}" role="button">
        ${LOGO[p] || esc(PROVIDER_NAME[p] || p)}
        <div class="ptile-name">${esc(PROVIDER_NAME[p] || p)}</div>
        ${ready ? "" : `<span class="soon-badge">${esc(t("soon"))}</span>`}
      </div>`;
    }).join("")}</div>` : ""}`;

  el("checkout-total").innerHTML = totalsRows();
  el("submit-order").textContent = `${t("confirm_order")} · ${money(goodsTotal() + state.deliveryPrice)}`;
}

function showError(message) {
  const box = el("checkout-error");
  box.textContent = message;
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function submitOrder() {
  if (state.sending) return;
  el("checkout-error").classList.add("hidden");

  const fields = {
    "f-name": el("f-name").value.trim(),
    "f-phone": el("f-phone").value.trim(),
    "f-address": el("f-address").value.trim(),
  };
  const problems = badContactFields(fields, "f-");
  const needConsent = !el("agree-box").classList.contains("hidden") && !el("agree").checked;
  el("agree-box").classList.toggle("bad", needConsent);
  if (problems.length) {
    return showError(t("fill_fields"));
  }
  if (needConsent) return showError(t("need_consent"));
  if (state.zone && !state.zone.ok) return showError(state.zone.message);
  if (state.payment === "online") return showError(t("pick_online"));

  state.sending = true;
  el("submit-order").textContent = t("sending");
  try {
    const response = await fetch("/api/orders", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...tgHeaders() },
      body: JSON.stringify({
        customer_name: fields["f-name"],
        phone: fields["f-phone"],
        address: fields["f-address"],
        house: el("f-house").value.trim() || null,
        entrance: el("f-entrance").value.trim() || null,
        flat: el("f-flat").value.trim() || null,
        lat: state.point.f ? state.point.f.lat : null,
        lon: state.point.f ? state.point.f.lon : null,
        delivery_slot: state.slot,
        payment_method: state.payment === "cash" ? "cash" : "online",
        pay_provider: state.payment === "cash" ? null : state.payment,
        comment: el("f-comment").value.trim() || null,
        consent: el("agree").checked,
        items: state.cart.map((i) => ({ variant_id: i.variantId, quantity: i.qty })),
        client_key: state.clientKey,
        expected_total: goodsTotal() + state.deliveryPrice,
      }),
    });

    let body = null;
    try {
      body = await response.json();
    } catch (error) {
      console.error(error);   // прокси вернул HTML вместо ответа сервера
    }

    if (response.status === 409) {
      return openCart(t("prices_changed"));
    }
    if (response.status === 401) {
      // подпись Telegram живёт сутки: приложение провисело открытым дольше
      throw Object.assign(new Error(body && body.detail
        ? body.detail
        : t("relaunch")),
        { forUser: true });
    }
    if (!response.ok) {
      // текст сервера показываем только там, где он написан для покупателя
      const detail = response.status < 500 && body && typeof body.detail === "string"
        ? body.detail
        : t("order_failed");
      throw Object.assign(new Error(detail), { forUser: true });
    }

    state.cart = [];
    state.scrollY = 0;   // «Вернуться в каталог» — сверху, а не там, где смотрели товар
    saveCart();
    renderCartBadge();
    if (!body) {
      // заказ создан, но ответ пришёл не в том виде — второй раз отправлять нельзя
      throw Object.assign(new Error(t("order_sent")),
        { forUser: true });
    }
    state.orders = [body, ...(state.orders || []).filter((o) => o.number !== body.number)];
    renderDone(body);
    show("done");
    if (state.profile) loadProfile();   // сервер запомнил данные — подтянем их обратно
    // заказ с оплатой картой создан и ждёт оплаты — сразу открываем окно;
    // закроет без оплаты — на экране останется кнопка «Оплатить».
    // Повтор с тем же ключом мог вернуть уже оплаченный заказ — тогда окна нет
    if (awaitingPayment(body)) payOrder(body.number);
  } catch (error) {
    console.error(error);
    // обрыв связи даёт техническое «Failed to fetch» — покупателю такое не показываем
    showError(error.forUser
      ? error.message
      : t("send_failed"));
    renderCheckout();   // возвращаем кнопке сумму вместо «Отправляем…»
  } finally {
    state.sending = false;
  }
}

/* Экран после оформления. Режимы:
 *   cash     — наличные: заказ сразу принят;
 *   awaiting — картой, ещё не оплачен: кнопка «Оплатить»;
 *   checking — окно оплаты закрылось с «paid», ждём подтверждения сервера;
 *   paid     — сервер подтвердил оплату;
 *   slow     — подтверждение задерживается: скажем, где смотреть статус;
 *   external — покупатель ушёл платить на страницу Payme.
 * «Оплачено» показываем только по слову сервера — не по статусу окна. */
function renderDone(order, mode) {
  const card = order.payment_method === "online";
  mode = mode || (card ? (order.paid ? "paid" : "awaiting") : "cash");
  const view = {
    cash: ["ti-check", t("cash_t"), t("cash_s")],
    paid: ["ti-check", t("paid_t"), t("paid_s")],
    awaiting: ["ti-credit-card", t("awaiting_t"),
      `${t("awaiting_s")}<br>${esc(payUntilText(order))}`],
    checking: ["ti-loader-2", t("checking_t"), t("checking_s")],
    slow: ["ti-clock", t("slow_t"), t("slow_s")],
    expired: ["ti-x", t("expired_t"), t("expired_s")],
    late: ["ti-alert-triangle", t("late_t"), t("late_s")],
    external: ["ti-external-link", t("external_t", { name: PROVIDER_NAME[externalProvider()] }),
      `${t("external_s")}<br>${esc(payUntilText(order))}`],
    paynet: ["ti-wallet", t("paynet_t"),
      `${t("paynet_s", {
        order: `<b>${esc((state.paynet && state.paynet.order) || order.number)}</b>`,
        sum: `<b>${money((state.paynet && state.paynet.amount) || order.total)}</b>`,
      })}<br>${esc(payUntilText(order))}`],
  }[mode];
  const spin = mode === "checking" ? "animation:spin 1s linear infinite;" : "";
  const sumLabel = mode === "paid" ? t("sum_paid") : (card ? t("sum_due") : t("sum_courier"));

  el("done-body").innerHTML = `
    <div style="text-align:center">
      <div style="width:82px;height:82px;border-radius:50%;background:#E30613;color:#fff;display:flex;align-items:center;justify-content:center;font-size:40px;margin:0 auto 20px">
        <i class="ti ${view[0]}" style="${spin}"></i></div>
      <div style="font-size:23px;font-weight:600;margin-bottom:8px">${view[1]}</div>
      <div class="mut" style="margin-bottom:22px;line-height:1.65">${view[2]}</div>
    </div>
    ${mode === "awaiting" ? payButtons(order) : ""}
    ${mode === "paynet" ? `${state.paynet && state.paynet.link
        ? `<button class="btn" data-pay="${esc(order.number)}" data-provider="paynet" style="margin-bottom:10px">
            ${t("open_paynet")}</button>` : ""}
      <button class="btn" data-copy="${esc((state.paynet && state.paynet.order) || order.number)}"
        style="margin-bottom:10px;background:#F2F2F2;color:#1A1A1A">${t("copy_number")}</button>
      <button class="btn" data-check-pay="${esc(order.number)}" style="margin-bottom:14px">
        ${t("i_paid")}</button>` : ""}
    ${mode === "external" ? `<button class="btn" data-check-pay="${esc(order.number)}" style="margin-bottom:10px">
      ${t("i_paid")}</button>
      <button class="btn" data-pay="${esc(order.number)}" data-provider="${esc(externalProvider())}" style="margin-bottom:14px;background:#F2F2F2;color:#1A1A1A">
      ${t("payme_again", { name: PROVIDER_NAME[externalProvider()] })}</button>` : ""}
    <div class="card" style="background:#F7F7F7;text-align:center">
      <div class="mut" style="font-size:12px;margin-bottom:4px">${t("order_number")}</div>
      <div style="font-size:21px;font-weight:600;letter-spacing:.6px">${esc(order.number)}</div>
    </div>
    ${order.items.length ? `<div class="card" style="background:#F7F7F7">
      <div class="row" style="margin-bottom:9px"><span class="mut">${t("composition")}</span>
        <span style="font-size:13px">${countText(order.items.length, "positions")}</span></div>
      <div class="row" style="margin-bottom:9px"><span class="mut">${sumLabel}</span>
        <span class="prc" style="font-size:13px">${money(order.total)}</span></div>
      <div class="row"><span class="mut">${t("delivery")}</span>
        <span style="font-size:13px">${esc(slotText(order.delivery_slot))}</span></div>
    </div>` : ""}`;
}

// ---------- профиль ----------

function telegramName() {
  const user = window.Telegram && window.Telegram.WebApp.initDataUnsafe
    && window.Telegram.WebApp.initDataUnsafe.user;
  return user ? [user.first_name, user.last_name].filter(Boolean).join(" ") : "";
}

/** Забирает сохранённые данные покупателя и подставляет их в оформление. */
async function loadProfile() {
  try {
    state.profile = await api("/api/profile");
  } catch (error) {
    console.error(error);
    // без профиля галочку согласия негде поставить, а без неё сервер не примет
    // заказ — получился бы тупик. Считаем, что покупатель новый
    state.profile = { name: telegramName(), phone: "", address: "", house: "",
                    entrance: "", flat: "", lat: null, lon: null, consent: false };
  }
  applyProfile();
}

/** Подтверждение средствами Telegram, а вне его — обычным окном браузера. */
function ask(text, done) {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.showConfirm) return tg.showConfirm(text, done);
  done(window.confirm(text));
}

function forgetProfile() {
  ask(t("forget_q"), async (yes) => {
    if (!yes) return;
    try {
      await api("/api/profile", { method: "DELETE" });
      state.profile = { name: telegramName(), phone: "", address: "", house: "",
                    entrance: "", flat: "", lat: null, lon: null, consent: false };
      applyProfile();
      renderProfile();
    } catch (error) {
      console.error(error);
      el("profile-error").textContent = t("forget_fail");
      el("profile-error").classList.remove("hidden");
    }
  });
}

function applyProfile() {
  const p = state.profile;
  if (!p) return;

  el("f-name").value = p.name;
  el("f-phone").value = p.phone || "+998 ";
  el("f-address").value = p.address;
  // точку из профиля подставляем в заказ: обычно везут туда же, а поменять
  // её на другой адрес можно прямо в оформлении
  el("f-house").value = p.house || "";
  el("f-entrance").value = p.entrance || "";
  el("f-flat").value = p.flat || "";
  state.point.f = p.lat != null && p.lon != null ? { lat: p.lat, lon: p.lon } : null;
  renderPoint("f");
  checkZone();
  // согласие берётся один раз: уже дано — больше не спрашиваем
  el("agree-box").classList.toggle("hidden", p.consent);

  renderAddrBar();
}

/** Строка адреса над каталогом. */
function renderAddrBar() {
  const p = state.profile;
  if (!p) return;
  el("addr").classList.remove("hidden");
  // в шапке показываем адрес с домом: без него строка «Шарк Тонги улица»
  // выглядит как незаполненная
  const short = [p.address, p.house && t("house_n", { n: p.house })].filter(Boolean).join(", ");
  el("addr-t").textContent = short || t("set_address");
  el("addr-s").textContent = p.address ? t("deliver_time") : t("addr_hint");
}

function renderProfile() {
  const p = state.profile || { name: "", phone: "", address: "", consent: false };
  el("p-name").value = p.name;
  el("p-phone").value = p.phone || "+998 ";
  el("p-address").value = p.address;
  el("p-house").value = p.house || "";
  el("p-entrance").value = p.entrance || "";
  el("p-flat").value = p.flat || "";
  state.point.p = p.lat != null && p.lon != null ? { lat: p.lat, lon: p.lon } : null;
  renderPoint("p");
  el("p-agree-box").classList.toggle("hidden", p.consent);
  el("p-agree").checked = false;
  el("profile-error").classList.add("hidden");
  el("save-profile").textContent = t("save");
  el("forget").classList.toggle("hidden", !p.consent);
}

async function saveProfile() {
  if (state.savingProfile) return;   // двойное нажатие завело бы вторую запись
  const fields = {
    "p-name": el("p-name").value.trim(),
    "p-phone": el("p-phone").value.trim(),
    "p-address": el("p-address").value.trim(),
  };
  const problems = badContactFields(fields, "p-");
  const needConsent = !el("p-agree-box").classList.contains("hidden") && !el("p-agree").checked;
  el("p-agree-box").classList.toggle("bad", needConsent);
  if (problems.length || needConsent) {
    el("profile-error").textContent = problems.length
      ? t("fill_fields")
      : t("need_consent");
    return el("profile-error").classList.remove("hidden");
  }

  state.savingProfile = true;
  el("save-profile").textContent = t("saving");
  try {
    state.profile = await api("/api/profile", {
      method: "PUT",
      body: {
        customer_name: fields["p-name"],
        phone: fields["p-phone"],
        address: fields["p-address"],
        house: el("p-house").value.trim() || null,
        entrance: el("p-entrance").value.trim() || null,
        flat: el("p-flat").value.trim() || null,
        lat: state.point.p ? state.point.p.lat : null,
        lon: state.point.p ? state.point.p.lon : null,
        consent: true,
      },
    });
    applyProfile();
    show("catalog");
  } catch (error) {
    console.error(error);
    el("profile-error").textContent =
      error.detail || t("save_fail");
    el("profile-error").classList.remove("hidden");
    el("save-profile").textContent = t("save");
  } finally {
    state.savingProfile = false;
  }
}

/** Общая проверка имени, телефона и адреса: одинаковая в заказе и в профиле. */
function badContactFields(values, prefix) {
  const digits = values[`${prefix}phone`].replace(/\D/g, "");
  const problems = [];
  if (values[`${prefix}name`].length < 2) problems.push(`${prefix}name`);
  if (!digits.startsWith("998") || digits.length !== 12) problems.push(`${prefix}phone`);
  if (values[`${prefix}address`].length < 5) problems.push(`${prefix}address`);
  for (const id of Object.keys(values)) el(id).classList.toggle("bad", problems.includes(id));
  return problems;
}

// ---------- мои заказы ----------

async function openOrders() {
  show("orders");
  el("orders-body").innerHTML = `<div class="msg">${t("loading")}</div>`;
  try {
    const orders = await api("/api/my-orders");
    state.orders = orders;      // из этого списка открывается карточка заказа
    el("orders-body").innerHTML = orders.length
      ? orders.map(orderCard).join("")
      : `<div class="msg">${t("no_orders")}</div>`;
  } catch (error) {
    console.error(error);
    el("orders-body").innerHTML = `<div class="msg">${error.status === 401
      ? t("relaunch")
      : t("orders_fail")}</div>`;
  }
}

/* Путь заказа значками — то, ради чего покупатель и открывает «Мои заказы».
 *
 * Состояния те же, что у оператора (app/order_status.py), но показываем только
 * четыре: «ждёт оплаты» — это ещё не шаг пути, а его отсутствие, а отмена
 * выбивается из цепочки совсем, и рисовать её кружком в ряду нельзя. */
const TRACK = [
  { status: "CONFIRMED", icon: "ti-receipt" },
  { status: "ASSEMBLING", icon: "ti-package" },
  { status: "DELIVERING", icon: "ti-bike" },
  { status: "DONE", icon: "ti-check" },
];

function tracker(order, big = false) {
  const size = big ? " big" : "";
  if (order.status === "CANCELED") {
    return `<div class="track-off${size}"><i class="ti ti-x"></i> ${esc(statusText(order))}</div>`;
  }
  // сколько шагов уже пройдено: «ждёт оплаты» — ни одного
  const done = TRACK.findIndex((step) => step.status === order.status);
  const cells = TRACK.map((step, index) => {
    const on = index <= done;
    const line = index === 0 ? "" : `<span class="ln${index <= done ? " on" : ""}"></span>`;
    return `${line}<span class="st${on ? " on" : ""}"><i class="ti ${step.icon}"></i></span>`;
  }).join("");
  return `<div class="track${size}">${cells}</div>
          <div class="track-name${size}">${esc(statusText(order))}</div>`;
}

function orderCard(order) {
  const when = new Date(order.created_at).toLocaleString(locale(), {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });
  return `
    <div class="card ord-card" data-order="${esc(order.number)}">
      <div class="row" style="margin-bottom:7px">
        <span style="font-weight:600">${esc(order.number)}</span>
        <span class="prc">${money(order.total)}</span>
      </div>
      <div class="row">
        <span class="mut" style="font-size:12px">${esc(when)}</span>
      </div>
      ${tracker(order)}
      ${awaitingPayment(order) ? `
        <button class="btn" data-pay="${esc(order.number)}" style="padding:11px;margin:6px 0 8px">
          ${t("pay")} ${money(order.total)}</button>
        <div class="mut" style="font-size:12px;text-align:center;margin-bottom:6px">
          ${esc(payUntilText(order))}</div>` : ""}
      <div class="row">
        <span class="mut" style="font-size:12px">
          ${countText(order.items.length, "positions")}
          · ${esc(slotText(order.delivery_slot))}</span>
        <span class="mut" style="font-size:12px">${t("more")}</span>
      </div>
    </div>`;
}

/* Отдельный экран заказа. В списке видно только главное — номер, сумму и путь;
 * всё остальное открывается по нажатию, как в карточке товара. Раскрывающийся
 * список внутри карточки для этого не годился: состав, адрес и комментарий
 * на маленьком экране складываются в простыню, из которой ничего не выудить. */
function openOrder(number) {
  const order = (state.orders || []).find((o) => o.number === number);
  if (!order) return;
  el("order-ttl").textContent = t("order_n", { n: order.number });
  const when = new Date(order.created_at).toLocaleString(locale(), {
    day: "numeric", month: "long", hour: "2-digit", minute: "2-digit",
  });
  const card = order.payment_method === "online";
  const paymentText = !card ? t("pm_cash")
    : order.paid ? t("pm_paid")
    : order.status === "CANCELED" ? t("pm_unpaid")
    : t("pm_waiting");

  el("order-body").innerHTML = `
    <div class="card">
      <div class="row">
        <span class="mut" style="font-size:12.5px">${esc(when)}</span>
        <span class="ord-big prc">${money(order.total)}</span>
      </div>
      ${tracker(order, true)}
      ${awaitingPayment(order) ? `
        <button class="btn" data-pay="${esc(order.number)}" style="margin-top:6px">
          ${t("pay")} ${money(order.total)}</button>
        <div class="mut" style="font-size:12px;text-align:center;margin-top:8px">
          ${esc(payUntilText(order))}</div>` : ""}
    </div>

    <div class="card">
      <div class="mut" style="font-size:12.5px;margin-bottom:8px">${t("order_items")}</div>
      ${order.items.map(orderLine).join("")}
      <div class="row" style="font-size:13.5px;margin-top:10px">
        <span class="mut">${t("goods")}</span><span>${money(order.goods_total)}</span>
      </div>
      <div class="row" style="font-size:13.5px;margin-top:6px">
        <span class="mut">${t("delivery")}</span><span>${money(order.delivery_price)}</span>
      </div>
      <div class="row" style="margin-top:8px">
        <span style="font-weight:600">${t("total")}</span>
        <span class="prc" style="font-weight:600">${money(order.total)}</span>
      </div>
    </div>

    <div class="card">
      <div class="ord-row"><span class="k">${t("where")}</span>
        <span class="v">${esc(order.address)}</span></div>
      <div class="ord-row"><span class="k">${t("phone")}</span>
        <span class="v">${esc(order.phone)}</span></div>
      <div class="ord-row"><span class="k">${t("time")}</span>
        <span class="v">${esc(slotText(order.delivery_slot))}</span></div>
      <div class="ord-row"><span class="k">${t("payment")}</span>
        <span class="v">${paymentText}</span></div>
      ${order.comment ? `<div class="ord-row"><span class="k">${t("comment")}</span>
        <span class="v">${esc(order.comment)}</span></div>` : ""}
    </div>`;
  show("order");
}

// Состав заказа. Названия и цены берутся те, что были на момент оформления
// (BR-09): в заказе покупателя каталог менять задним числом нельзя.
function orderLine(item) {
  const count = item.quantity > 1 ? ` × ${item.quantity}` : "";
  // у товара может не быть фото: карточку заводят из REGOS без картинки,
  // и заказ должен читаться в любом случае — вместо снимка ставим заглушку
  const photo = item.photo
    ? `<img class="ord-pic" src="${esc(item.photo)}" alt="" loading="lazy">`
    : `<span class="ord-pic ord-pic-none"></span>`;
  return `
    <div class="ord-line">
      ${photo}
      <span class="ord-name">${esc(item.product_name)}
        <span class="mut">${esc(item.weight)}${count}</span></span>
      <span class="ord-sum">${money(item.price * item.quantity)}</span>
    </div>`;
}

// ---------- точка на карте ----------

/* Адрес строкой курьеру нужен всё равно — по нему подъезд, этаж и квартира.
 * Карта отвечает за другое: по ней он находит дом, не разбирая «за третьим
 * магазином направо». Поэтому точка необязательна и ничего не заменяет.
 *
 * Библиотека и стили лежат у нас же и подгружаются только при открытии карты:
 * тянуть 150 КБ ради каталога незачем, а сторонний CDN — лишняя точка отказа
 * там, где связь и так небыстрая.
 */

const TASHKENT = [41.311081, 69.240562];   // центр по умолчанию
const MAP_ZOOM = 16;

/* Два вида карты. Спутник стоит первым и по умолчанию: свой дом человек узнаёт
 * по крыше и двору быстрее, чем по названию улицы, а в махаллях названий часто
 * и нет. Схема — обычный OpenStreetMap: по Ташкенту он знает дома, дворы
 * и проезды, чего нельзя сказать о других бесплатных схемах.
 *
 * Здесь была схема CARTO с подписями поверх спутника. В сентябре 2026 CARTO
 * закрыли бесплатную отдачу: тайлы приходят с водяным знаком «API KEY
 * REQUIRED» поверх карты. Ссылки оставлять нельзя — красиво, но нечитаемо;
 * подписей к спутнику из-за этого больше нет, зато адрес под картой
 * определяется по точке и показывается словами.
 *
 * Ключи ни одному из слоёв не нужны, но указывать источник обязаны оба.
 * Esri на снимках просит ссылаться на себя и поставщиков, OpenStreetMap —
 * на участников проекта. */
const MAP_LAYERS = {
  satellite: {
    tiles: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    maxZoom: 19,
    attribution: "Esri, Maxar",
  },
  scheme: {
    tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  },
};

const MAP_LAYER_KEY = "rozmart.map.layer";

let mapView = null;        // объект Leaflet, создаётся один раз
let mapTarget = "f";       // какое поле правим: «f» — заказ, «p» — профиль
let mapMoved = false;      // карту уже двигали руками
let mapBase = null;        // слой подложки
let mapKind = "satellite"; // выбранный вид
let mapAddress = "";       // что нашлось по точке в центре карты
let mapHouse = "";         // номер дома оттуда же — у него своё поле
let mapMe = null;          // синяя точка «вы здесь»
let mapAccuracy = null;    // круг точности вокруг неё
let mapLocating = false;   // запрос координат уже идёт
let mapLocationReady = null;   // промис инициализации LocationManager
let mapLookup = null;      // таймер отложенного запроса адреса

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const tag = document.createElement("script");
    tag.src = src;
    tag.onload = resolve;
    tag.onerror = () => reject(new Error(`не загрузился ${src}`));
    document.head.appendChild(tag);
  });
}

async function loadLeaflet() {
  if (window.L) return;
  const css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/static/vendor/leaflet/leaflet.css";
  document.head.appendChild(css);
  await loadScript("/static/vendor/leaflet/leaflet.js");
}

/** Открывает карту для поля prefix: «f» — оформление заказа, «p» — профиль. */
async function openMap(prefix) {
  mapTarget = prefix;
  show("map");
  try {
    await loadLeaflet();
  } catch (error) {
    console.error(error);
    show(prefix === "f" ? "checkout" : "profile");
    return note(t("map_fail"));
  }

  const point = state.point[prefix];
  const center = point ? [point.lat, point.lon] : TASHKENT;

  mapMoved = false;
  if (!mapView) {
    mapView = L.map("map", { zoomControl: false, attributionControl: true })
      .setView(center, MAP_ZOOM);
    // как только карту тронули пальцем, автоматическое «моё местоположение»
    // больше не вмешивается: ответ на запрос геопозиции приходит с задержкой
    // в несколько секунд и иначе уводит карту из-под уже выбранной точки
    mapView.on("dragstart zoomstart", () => { mapMoved = true; });
    // адрес под булавкой обновляем, когда карта остановилась
    mapView.on("moveend", scheduleLookup);
    setMapKind(savedMapKind());
  } else {
    mapView.setView(center, MAP_ZOOM);
  }

  // экран показан только что: Leaflet посчитал размеры, пока блок был скрыт,
  // и без этого рисует карту в четверть экрана
  setTimeout(() => mapView.invalidateSize(), 60);
  scheduleLookup();
  if (!point) locateMe(true);
}

function savedMapKind() {
  // выбор запоминаем: человек, которому привычнее схема, не должен переключать
  // её при каждом заказе. localStorage может быть недоступен — не беда
  try {
    const saved = localStorage.getItem(MAP_LAYER_KEY);
    if (saved && MAP_LAYERS[saved]) return saved;
  } catch (error) {
    console.error(error);
  }
  return "satellite";
}

/** Переключает подложку карты. */
function setMapKind(kind) {
  if (!MAP_LAYERS[kind] || !mapView) return;
  mapKind = kind;
  const layer = MAP_LAYERS[kind];

  if (mapBase) mapView.removeLayer(mapBase);

  mapBase = L.tileLayer(layer.tiles, {
    maxZoom: layer.maxZoom,
    // указание источника обязательно по условиям обоих поставщиков карт
    attribution: layer.attribution,
  }).addTo(mapView);

  for (const button of document.querySelectorAll("[data-layer]")) {
    button.classList.toggle("on", button.dataset.layer === kind);
  }
  try {
    localStorage.setItem(MAP_LAYER_KEY, kind);
  } catch (error) {
    console.error(error);   // приватный режим — просто не запомним выбор
  }
}

/* «Где я» — самое хрупкое место карты, поэтому подробно.
 *
 * Внутри Telegram браузерный доступ к геопозиции часто закрыт, и работает
 * только LocationManager — его же средствами Telegram и спрашивает разрешение.
 * Спрашивать его при каждом открытии карты нельзя: покупатель видел окно
 * с вопросом на каждый заказ. Поэтому сами, без нажатия кнопки, мы берём
 * координаты, только если доступ уже выдан; в остальных случаях ждём кнопку —
 * то есть человек сам попросил, и окно с вопросом ему понятно.
 *
 * И результат надо показать. Раньше карта просто уезжала, и если точка была
 * рядом, казалось, что кнопка не работает. Теперь на карте появляется синяя
 * точка и круг точности: видно, где телефон считает нас находящимися
 * и насколько уверенно.
 */

function locationManager() {
  const tg = window.Telegram && window.Telegram.WebApp;
  const manager = tg && tg.LocationManager;
  return manager && typeof manager.getLocation === "function" ? manager : null;
}

/** Инициализация нужна один раз за сессию; второй init только тратит время. */
function initLocation(manager) {
  if (!mapLocationReady) {
    mapLocationReady = new Promise((resolve) => {
      try {
        if (manager.isInited) return resolve(true);
        manager.init(() => resolve(true));
        // бывает, что ответа нет вовсе — на старых клиентах и вне Telegram.
        // Две с половиной секунды: дольше человек считает кнопку сломанной
        setTimeout(() => resolve(Boolean(manager.isInited)), 2500);
      } catch (error) {
        console.error(error);
        resolve(false);
      }
    });
  }
  return mapLocationReady;
}

/** Показывает, где покупатель сейчас. quiet — сами, без его просьбы. */
async function locateMe(quiet) {
  if (mapLocating) return;
  // Значок начинает крутиться сразу по нажатию, ещё до первого запроса:
  // Telegram отвечает не мгновенно, и эти секунды молчания выглядят так,
  // будто кнопка не работает.
  if (!quiet) setLocating(true);
  const manager = locationManager();
  if (!manager) return browserLocate(quiet);

  const ready = await initLocation(manager);
  if (!ready || manager.isLocationAvailable === false) {
    // клиент старый или устройство не умеет — молча уходим в браузерный путь
    return browserLocate(quiet);
  }
  // без явной просьбы разрешение не спрашиваем: это и есть то самое окно,
  // которое выскакивало каждый раз
  if (quiet && manager.isAccessGranted !== true) return setLocating(false);

  setLocating(true);
  try {
    manager.getLocation((data) => {
      setLocating(false);
      if (data && data.latitude) {
        return showMe(data.latitude, data.longitude, data.horizontal_accuracy, quiet);
      }
      if (quiet) return;
      offerSettings(manager);
    });
  } catch (error) {
    console.error(error);
    setLocating(false);
    browserLocate(quiet);
  }
}

/** Доступ закрыт. Telegram умеет открыть свои настройки — предлагаем. */
function offerSettings(manager) {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (manager.isAccessGranted === false && manager.openSettings && tg && tg.showConfirm) {
    return tg.showConfirm(
      t("geo_settings"),
      (yes) => { if (yes) manager.openSettings(); },
    );
  }
  note(t("geo_fail"));
}

async function browserLocate(quiet) {
  if (!navigator.geolocation) {
    setLocating(false);
    if (!quiet) note(t("geo_phone"));
    return;
  }
  if (quiet) {
    // Сами спрашиваем, только если разрешение уже дано: иначе браузер покажет
    // своё окно с вопросом — при каждом открытии карты
    if (!navigator.permissions) return setLocating(false);
    try {
      const status = await navigator.permissions.query({ name: "geolocation" });
      if (status.state !== "granted") return setLocating(false);
    } catch (error) {
      return setLocating(false);
    }
  }

  setLocating(true);
  navigator.geolocation.getCurrentPosition(
    (position) => {
      setLocating(false);
      showMe(position.coords.latitude, position.coords.longitude,
             position.coords.accuracy, quiet);
    },
    (error) => {
      setLocating(false);
      if (quiet) return;
      note(error.code === 1 ? t("geo_denied") : t("geo_fail"));
    },
    // maximumAge: свежий сигнал ищем не дольше десяти секунд, а ответ минутной
    // давности берём сразу — на улице он тот же, а ждать не приходится
    { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
  );
}

/** Синяя точка и круг точности там, где телефон нас нашёл. */
function showMe(lat, lon, accuracy, quiet) {
  if (!mapView) return;
  const radius = Math.min(Math.max(Number(accuracy) || 0, 0), 2000);

  if (!mapMe) {
    mapMe = L.circleMarker([lat, lon], {
      radius: 7, weight: 3, color: "#fff", fillColor: "#1C64F2", fillOpacity: 1,
    }).addTo(mapView);
    mapAccuracy = L.circle([lat, lon], {
      radius, weight: 1, color: "#1C64F2", fillColor: "#1C64F2", fillOpacity: 0.12,
    }).addTo(mapView);
  } else {
    mapMe.setLatLng([lat, lon]);
    mapAccuracy.setLatLng([lat, lon]).setRadius(radius);
  }

  // карту, которую уже двигали руками, автоматический ответ не трогает
  if (quiet && mapMoved) return;
  // при слабом сигнале не приближаем вплотную: булавка встала бы не на тот дом,
  // а человек бы этого не заметил
  mapView.setView([lat, lon], radius > 120 ? 16 : 17);
}

function setLocating(busy) {
  mapLocating = busy;
  const button = el("map-here");
  if (button) button.classList.toggle("busy", busy);
}

/** Короткое сообщение покупателю. В Telegram обычный alert() показывается
 *  не везде, поэтому по возможности просим показать его сам Telegram. */
function note(text) {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.showAlert) return tg.showAlert(text);
  alert(text);
}

/* Адрес по точке. Спрашиваем не на каждый сдвиг карты, а когда её отпустили
 * и она постояла секунду: геокодер бесплатный и просит не частить, да и
 * покупателю незачем видеть мелькание адресов, пока он возит карту пальцем. */
function scheduleLookup() {
  clearTimeout(mapLookup);
  mapHouse = "";
  setMapAddress("", t("addr_lookup"));
  mapLookup = setTimeout(lookupAddress, 900);
}

async function lookupAddress() {
  if (!mapView) return;
  const center = mapView.getCenter();
  const at = [center.lat.toFixed(6), center.lng.toFixed(6)];
  try {
    const found = await api(`/api/geocode?lat=${at[0]}&lon=${at[1]}`);
    // пока ходил запрос, карту могли увезти дальше: тот ответ уже не про эту точку
    const now = mapView.getCenter();
    if (now.lat.toFixed(6) !== at[0] || now.lng.toFixed(6) !== at[1]) return;
    mapHouse = found.house || "";
    setMapAddress(found.address || "", found.address ? "" : t("addr_unknown"));
  } catch (error) {
    console.error(error);
    mapHouse = "";
    setMapAddress("", t("addr_unknown"));
  }
}

function setMapAddress(address, fallbackHint) {
  mapAddress = address;
  // номер дома показываем вместе с улицей: человек должен видеть, что булавка
  // стоит на его доме, а не на соседнем
  const shown = address && mapHouse ? t("addr_house", { a: address, h: mapHouse }) : address;
  el("map-hint").textContent = shown
    || fallbackHint
    || t("map_hint");
}

/** Запоминает точку в центре карты и возвращает покупателя к форме. */
function saveMapPoint() {
  if (mapView) {
    const center = mapView.getCenter();
    state.point[mapTarget] = {
      // шесть знаков — около 10 см; больше хранить нет смысла
      lat: Number(center.lat.toFixed(6)),
      lon: Number(center.lng.toFixed(6)),
    };
    fillAddress(mapTarget, mapAddress);
  }
  renderPoint(mapTarget);
  if (mapTarget === "f") checkZone();
  show(mapTarget === "f" ? "checkout" : "profile");
}

/** Доставляем ли в точку заказа. Спрашиваем сразу, как поставили булавку:
 * узнать про чужой район после заполнения всей формы — обидно. Окончательно
 * проверяет сервер при оформлении; молчание здесь заказу не мешает. */
async function checkZone() {
  state.zone = null;
  const point = state.point.f;
  if (!point) return renderPoint("f");
  try {
    const r = await fetch(`/api/delivery-zone?lat=${point.lat}&lon=${point.lon}`,
      { headers: tgHeaders() });
    if (r.ok && state.point.f === point) state.zone = await r.json();
  } catch (error) {
    console.error(error);
  }
  renderPoint("f");
}

/** Ставит найденный адрес в поле.
 *
 * Именно ставит, а не предлагает: точка на карте — это и есть ответ на вопрос
 * «куда везти», и человек, который её поставил, ждёт адрес в поле, а не ещё
 * одну кнопку. Написанное раньше при этом заменяется, поэтому подъезд, этаж
 * и квартиру дописывают после — об этом говорит подсказка под полем. */
function fillAddress(prefix, address) {
  if (!address) return;
  const field = el(`${prefix}-address`);
  field.value = address;
  state.filled[prefix] = address;
  field.classList.remove("bad");
  // номер дома с карты кладём в своё поле; подъезд и квартиру знает
  // только сам покупатель, их не трогаем
  if (mapHouse) el(`${prefix}-house`).value = mapHouse;
}

function clearPoint(prefix) {
  state.point[prefix] = null;
  if (prefix === "f") state.zone = null;
  renderPoint(prefix);
}

function renderPoint(prefix) {
  const box = el(`${prefix}-point`);
  const point = state.point[prefix];
  box.classList.toggle("hidden", !point);
  if (!point) return;
  const zone = prefix === "f" && state.zone && !state.zone.ok ? state.zone : null;
  box.innerHTML = `
    <span class="${zone ? "bad" : "ok"}">${t("point_ok")}</span>
    <button data-map="${prefix}">${t("point_change")}</button>
    <button data-drop-map="${prefix}">${t("point_drop")}</button>
    ${zone ? `<div class="zone"></div>` : ""}`;
  // текст сервера — textContent: в названиях районов ничего опасного нет,
  // но вставлять ответ сервера разметкой незачем
  if (zone) box.querySelector(".zone").textContent = zone.message;
}

// ---------- обновление свайпом сверху вниз ----------

/* Своего «потянуть, чтобы обновить» в Telegram нет, а браузерного не будет:
 * вертикальный свайп там принадлежит самому Telegram — им приложение
 * сворачивают. Мы этот жест отключаем (disableVerticalSwipes), иначе
 * приложение закрывалось бы при каждом промахе мимо списка. Освободившийся
 * жест и используем: тянем сверху вниз — перечитываем то, что на экране.
 */

const PULL_SCREENS = ["catalog", "cart", "orders", "profile"];
const PULL_THRESHOLD = 70;      // насколько надо оттянуть, чтобы сработало
const PULL_MAX = 96;            // дальше значок не едет, даже если тянут сильнее

let pullFrom = null;            // откуда начали тянуть
let pullShift = 0;              // на сколько оттянули (с сопротивлением)
let refreshing = false;

function pullBadge() {
  let badge = el("pull");
  if (!badge) {
    badge = document.createElement("div");
    badge.id = "pull";
    badge.className = "pull";
    badge.innerHTML = `<i class="ti ti-refresh"></i>`;
    document.body.appendChild(badge);
  }
  return badge;
}

function drawPull(shift, spinning) {
  const badge = pullBadge();
  badge.style.transform = `translateX(-50%) translateY(${shift}px) rotate(${shift * 3}deg)`;
  badge.style.opacity = String(Math.min(shift / PULL_THRESHOLD, 1));
  badge.classList.toggle("ready", shift >= PULL_THRESHOLD && !spinning);
  badge.classList.toggle("spin", Boolean(spinning));
}

function hidePull() {
  const badge = pullBadge();
  badge.style.transition = "transform .2s, opacity .2s";
  drawPull(0, false);
  setTimeout(() => { badge.style.transition = ""; }, 220);
}

/** Перечитывает то, что показано сейчас. */
async function refreshScreen() {
  if (state.screen === "orders") return openOrders();
  if (state.screen === "cart") {
    await refreshCart();
    return renderCart();
  }
  if (state.screen === "profile") return loadProfile();
  // каталог: заодно обновляем стоимость доставки — она тоже приходит с сервера
  await loadDeliveryPrice();
  return loadCatalog();
}

document.addEventListener("touchstart", (event) => {
  if (refreshing || event.touches.length !== 1) return;
  if (!PULL_SCREENS.includes(state.screen)) return;
  // тянуть можно только от самого верха: иначе жест спорит с прокруткой списка
  if (window.scrollY > 0) return;
  pullFrom = event.touches[0].clientY;
  pullShift = 0;
}, { passive: true });

document.addEventListener("touchmove", (event) => {
  if (pullFrom === null) return;
  const moved = event.touches[0].clientY - pullFrom;
  if (moved <= 0) {                     // потянули вверх — это обычная прокрутка
    pullFrom = null;
    return hidePull();
  }
  // сопротивление: палец проходит вдвое больше, чем едет значок, — так жест
  // ощущается упругим и не срабатывает от случайного касания
  pullShift = Math.min(moved / 2, PULL_MAX);
  drawPull(pullShift, false);
}, { passive: true });

document.addEventListener("touchend", async () => {
  if (pullFrom === null) return;
  const enough = pullShift >= PULL_THRESHOLD;
  pullFrom = null;
  if (!enough) return hidePull();

  refreshing = true;
  drawPull(PULL_THRESHOLD, true);
  const tg = window.Telegram && window.Telegram.WebApp;
  // короткий отклик: понятно, что жест принят, ещё до того, как придут данные
  if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
  try {
    await refreshScreen();
  } catch (error) {
    console.error(error);
  } finally {
    refreshing = false;
    hidePull();
  }
}, { passive: true });

// ---------- оплата картой ----------

/* Покупатель платит в окне Telegram (openInvoice), не выходя из приложения.
 *
 * Статус, который возвращает окно, — это только «как закрылось окно». Правда
 * о деньгах приходит серверу от Telegram отдельным уведомлением, поэтому после
 * «paid» мы не рисуем «оплачено» сразу, а спрашиваем сервер, пока он не
 * подтвердит. Иначе покупатель увидел бы «оплачено» у заказа, который кассир
 * не получит.
 */

async function loadPaymentOptions() {
  try {
    const options = await api("/api/payment-options");
    state.providers = options.providers || (options.card ? [options.provider] : []);
    state.unpaidMinutes = options.unpaid_minutes || 30;
  } catch (error) {
    console.error(error);
    state.providers = [];   // не узнали — не предлагаем
  }
  state.cardAvailable = state.providers.length > 0;
  // выбранный раньше способ мог стать недоступным — не оставляем его выбранным
  if (!["cash", "online"].includes(state.payment) && !state.providers.includes(state.payment)) {
    state.payment = "online";
  }
  if (state.screen === "checkout") renderCheckout();
}

function availablePayments() {
  return PAYMENTS.filter(([code]) => code === "cash" || (state.providers || []).includes(code));
}

/** Кнопки оплаты неоплаченного заказа: выбранный способ — главной кнопкой,
 *  остальные — запасными: не вышло через Payme — можно через Paynet. */
function payButtons(order) {
  const providers = (state.providers || []).length ? state.providers
    : [order.pay_provider].filter(Boolean);
  if (!providers.length) {
    return `<button class="btn" data-pay="${esc(order.number)}" style="margin-bottom:14px">
      ${t("pay")}${order.total ? " " + money(order.total) : ""}</button>`;
  }
  const main = providers.includes(order.pay_provider) ? order.pay_provider : providers[0];
  const ordered = [main, ...providers.filter((p) => p !== main)];
  return ordered.map((p, i) => `
    <button class="btn" data-pay="${esc(order.number)}" data-provider="${esc(p)}"
      style="margin-bottom:${i === ordered.length - 1 ? 14 : 10}px${i ? ";background:#F2F2F2;color:#1A1A1A" : ""}">
      ${t("pay_with", { name: PROVIDER_NAME[p] || p })}${i === 0 && order.total ? " · " + money(order.total) : ""}</button>`).join("");
}

/** Выставляет счёт и открывает окно оплаты: окно Telegram или страницу Payme. */
async function payOrder(number, provider) {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    return note(t("tg_only"));
  }
  if (state.paying) return;          // двойное нажатие открыло бы два окна
  state.paying = true;
  try {
    // provider — способ, отличный от выбранного при оформлении; без него
    // сервер откроет тот, что выбран у заказа
    const query = provider ? `?provider=${encodeURIComponent(provider)}` : "";
    const invoice = await api(`/api/orders/${encodeURIComponent(number)}/invoice${query}`,
      { method: "POST" });
    const { link, kind } = invoice;
    if (kind === "payme" || kind === "click") {
      state.paying = false;
      return openPayme(tg, number, link, kind);
    }
    if (kind === "paynet") {
      state.paying = false;
      return openPaynet(tg, number, invoice);
    }
    if (!tg.openInvoice) {
      state.paying = false;
      return note(t("tg_update"));
    }
    tg.openInvoice(link, (status) => afterInvoice(number, status));
    // окно Telegram модальное: второе поверх не откроется, и держать флаг
    // дольше незачем. А если колбэк не придёт вовсе, кнопка не должна умереть
    state.paying = false;
  } catch (error) {
    state.paying = false;
    console.error(error);
    note(error.detail || t("pay_open_fail"));
  }
}

/* Payme. Покупатель уходит на страницу Payme (оттуда — в приложение Payme,
 * если оно есть) и возвращается сам. Узнать, чем кончилось, можно только
 * у сервера: Payme сообщает об оплате ему. Поэтому, когда приложение снова
 * на экране, спрашиваем сервер несколько раз подряд — подтверждение приходит
 * через секунду-другую после оплаты. */
/* Paynet. Страницы оплаты нет: покупатель платит в самом Paynet по номеру
 * заказа. Показываем, что ввести, даём скопировать номер; если Paynet дал
 * ссылку на оплату сервиса — открываем её. Об оплате Paynet сообщает
 * серверу, и, когда приложение снова на экране, мы спрашиваем сервер —
 * так же, как после Payme. */
function openPaynet(tg, number, info) {
  state.payingExternal = number;
  state.paynet = { order: info.order, amount: info.amount, link: info.link };
  if (info.link) {
    if (tg.openLink) tg.openLink(info.link); else window.open(info.link, "_blank");
  }
  const order = (state.orders || []).find((o) => o.number === number);
  renderDone(order || orderStub(number), "paynet");
  show("done");
}

/** Номер заказа — в буфер обмена: вводить его в Paynet проще вставкой. */
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    // старый WebView без доступа к буферу: выделяем вручную
    const field = document.createElement("textarea");
    field.value = text;
    document.body.appendChild(field);
    field.select();
    try { document.execCommand("copy"); } catch (e) { console.error(e); }
    field.remove();
  }
  note(t("copied"));
}

/** Покупатель ушёл платить на страницу Payme или Click: какой — для текстов и
 *  кнопки «открыть ещё раз». */
function externalProvider() {
  return state.externalProvider || "payme";
}

function openPayme(tg, number, link, provider) {
  state.payingExternal = number;
  state.externalProvider = provider || "payme";
  if (tg.openLink) tg.openLink(link); else window.open(link, "_blank");
  const order = (state.orders || []).find((o) => o.number === number);
  renderDone(order || orderStub(number), "external");
  show("done");
}

/** Спрашивает сервер об оплате после возврата со страницы Payme. */
async function checkExternalPayment(number, byButton) {
  if (state.checkingPay) return;
  state.checkingPay = true;
  try {
    for (let attempt = 0; attempt < (byButton ? 3 : 5); attempt += 1) {
      const order = await fetchMyOrder(number);
      if (order && !awaitingPayment(order)) {
        state.payingExternal = null;
        renderDone(order, stateOf(order));
        show("done");
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    if (byButton) note(t("not_yet"));
  } finally {
    state.checkingPay = false;
  }
}

function onAppVisible() {
  if (state.payingExternal && document.visibilityState !== "hidden") {
    checkExternalPayment(state.payingExternal, false);
  }
}
document.addEventListener("visibilitychange", onAppVisible);
if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.onEvent) {
  window.Telegram.WebApp.onEvent("activated", onAppVisible);
}

/** Что показать после окна оплаты. */
async function afterInvoice(number, status) {
  if (status === "paid" || status === "pending") {
    renderDone(orderStub(number), "checking");
    show("done");
    const order = await waitForPayment(number);
    return renderDone(order || orderStub(number), stateAfterPayment(order));
  }
  // Закрыли окно или платёж не прошёл. Что показать — решают данные сервера:
  // за время в окне оплаты заказ мог отмениться по таймауту, и кнопка
  // «Оплатить» у отменённого заказа только обманет
  const order = await fetchMyOrder(number);
  renderDone(order || orderStub(number), order ? stateOf(order) : "awaiting");
  show("done");
  if (status === "failed" && order && awaitingPayment(order)) {
    note(t("pay_failed"));
  }
}

/** Режим экрана по тому, что знает о заказе сервер. */
function stateOf(order) {
  if (order.paid) return order.status === "CANCELED" ? "late" : "paid";
  if (awaitingPayment(order)) return "awaiting";
  return order.status === "CANCELED" ? "expired" : "slow";
}

/** После «paid» из окна: сервер подтвердил, не успел или деньги пришли поздно. */
function stateAfterPayment(order) {
  if (!order) return "slow";
  if (order.paid) return order.status === "CANCELED" ? "late" : "paid";
  // окно сказало «оплачено», а заказ уже отменён — деньги могли списаться
  // за отменённый заказ, и об этом надо сказать прямо
  if (order.status === "CANCELED") return "late";
  return "slow";
}

/** Ждёт, пока сервер получит подтверждение оплаты от Telegram. */
async function waitForPayment(number) {
  // обычно уведомление приходит за секунду-две; 30 секунд — с большим запасом
  for (let attempt = 0; attempt < 15; attempt += 1) {
    const order = await fetchMyOrder(number);
    if (order && (order.paid || order.status !== "NEW")) return order;
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  return fetchMyOrder(number);
}

async function fetchMyOrder(number) {
  try {
    const orders = await api("/api/my-orders");
    state.orders = orders;
    return orders.find((o) => o.number === number) || null;
  } catch (error) {
    console.error(error);
    return null;
  }
}

/** Заглушка на случай, когда заказ не удалось перечитать: номер есть всегда. */
function orderStub(number) {
  const known = (state.orders || []).find((o) => o.number === number);
  return known || { number, items: [], total: 0, delivery_slot: "", payment_method: "online" };
}

function awaitingPayment(order) {
  return order.payment_method === "online" && !order.paid && order.status === "NEW";
}

function payUntilText(order) {
  if (!order.pay_until) return "";
  const until = new Date(order.pay_until).toLocaleTimeString(locale(),
    { hour: "2-digit", minute: "2-digit" });
  return t("pay_until", { t: until });
}

// ---------- переключение экранов ----------

const SCREENS = ["catalog", "product", "cart", "checkout", "done", "orders", "profile",
                 "order", "map"];

// корневые разделы — те, между которыми переключает нижнее меню. Остальные
// экраны открываются «поверх» и меню не показывают: у них своя кнопка внизу
const ROOT_SCREENS = ["catalog", "cart", "orders", "profile"];
const SCREENS_WITH_BUTTON = ["product", "cart", "checkout", "done", "profile"];

// куда ведёт кнопка «назад» Telegram с каждого некорневого экрана
// с карты возвращаемся туда, откуда её открыли, — это решает mapReturn()
const BACK_TO = { product: "catalog", checkout: "cart", done: "catalog",
                  order: "orders" };

function show(name) {
  // незавершённый запрос не должен подменить экран: пока грузилась карточка
  // товара, покупатель мог уже уйти в корзину
  requestId += 1;
  state.screen = name;
  for (const s of SCREENS) el(`screen-${s}`).classList.toggle("hidden", s !== name);

  const root = ROOT_SCREENS.includes(name);
  const tg = window.Telegram && window.Telegram.WebApp;
  // без неё аппаратная «назад» на Android закрывает приложение целиком —
  // например, с наполовину заполненной формы оформления
  if (tg && tg.BackButton) (root ? tg.BackButton.hide : tg.BackButton.show).call(tg.BackButton);
  el("tabs").classList.toggle("hidden", !root);
  // от этих классов зависит нижний отступ содержимого: под меню, под кнопку
  // или под то и другое сразу — иначе последняя строка уезжает под них
  document.body.classList.toggle("tabs-on", root);
  document.body.classList.toggle("action-on", SCREENS_WITH_BUTTON.includes(name));
  for (const tab of document.querySelectorAll("[data-tab]")) {
    tab.classList.toggle("on", tab.dataset.tab === name);
  }

  renderCartBadge();
  window.scrollTo(0, name === "catalog" ? state.scrollY : 0);
}

/** Открывает корзину, предварительно сверив её с каталогом. */
async function openCart(notice = "") {
  state.cartNotice = notice;
  renderCart();
  show("cart");
  await refreshCart();
  if (!state.cartNotice) state.cartNotice = notice;
  renderCart();
}

// ---------- события ----------

el("q").addEventListener("input", (event) => search(event.target.value));
// Enter убирает клавиатуру: формы нет, отправлять нечего
el("q").addEventListener("keydown", (event) => {
  if (event.key === "Enter") el("q").blur();
});

document.addEventListener("click", (event) => {
  if (event.target.closest("#q-clear")) {
    clearSearch();
    return search("");
  }

  const category = event.target.closest("[data-category]");
  if (category) return selectCategory(Number(category.dataset.category));

  const product = event.target.closest("[data-product]");
  if (product) return openProduct(Number(product.dataset.product));

  const variant = event.target.closest("[data-variant]");
  if (variant) {
    // количество сохраняем: покупатель уже выбрал, сколько ему нужно
    state.variant = state.product.variants.find((v) => v.id === Number(variant.dataset.variant));
    return renderProduct();
  }

  if (event.target.closest("#plus")) {
    state.qty = Math.min(MAX_QTY, state.qty + 1);
    return updateTotal();
  }
  if (event.target.closest("#minus")) {
    state.qty = Math.max(1, state.qty - 1);
    return updateTotal();
  }
  if (event.target.closest("#back")) return show("catalog");
  if (event.target.closest("#add")) return addToCart();

  const langButton = event.target.closest("[data-lang]");
  if (langButton) return setLang(langButton.dataset.lang);

  const payButton = event.target.closest("[data-pay]");
  if (payButton) return payOrder(payButton.dataset.pay, payButton.dataset.provider);

  const copyButton = event.target.closest("[data-copy]");
  if (copyButton) return copyText(copyButton.dataset.copy);

  const checkPayButton = event.target.closest("[data-check-pay]");
  if (checkPayButton) return checkExternalPayment(checkPayButton.dataset.checkPay, true);

  const orderCardEl = event.target.closest("[data-order]");
  if (orderCardEl) return openOrder(orderCardEl.dataset.order);

  const mapButton = event.target.closest("[data-map]");
  if (mapButton) return openMap(mapButton.dataset.map);

  const dropPoint = event.target.closest("[data-drop-map]");
  if (dropPoint) return clearPoint(dropPoint.dataset.dropMap);

  const layerButton = event.target.closest("[data-layer]");
  if (layerButton) return setMapKind(layerButton.dataset.layer);

  if (event.target.closest("#map-done")) return saveMapPoint();
  if (event.target.closest("#map-here")) return locateMe(false);
  if (event.target.closest("#map-back")) return show(mapTarget === "f" ? "checkout" : "profile");

  if (event.target.closest("#save-profile")) return saveProfile();
  if (event.target.closest("#forget")) return forgetProfile();

  const tab = event.target.closest("[data-tab]");
  if (tab) {
    if (tab.dataset.tab === "cart") return openCart();
    if (tab.dataset.tab === "orders") return openOrders();
    if (tab.dataset.tab === "profile") {
      renderProfile();
      return show("profile");
    }
    return show("catalog");
  }

  // корзина
  const minus = event.target.closest("[data-cart-minus]");
  if (minus) return changeCartQty(Number(minus.dataset.cartMinus), -1);
  const plus = event.target.closest("[data-cart-plus]");
  if (plus) return changeCartQty(Number(plus.dataset.cartPlus), 1);
  const del = event.target.closest("[data-cart-del]");
  if (del) {
    state.cart.splice(Number(del.dataset.cartDel), 1);
    saveCart();
    return renderCart();
  }

  // оформление
  const slot = event.target.closest("[data-slot]");
  if (slot) {
    state.slot = slot.dataset.slot;
    return renderCheckout();
  }
  const payment = event.target.closest("[data-payment]");
  if (payment) {
    const code = payment.dataset.payment;
    // «Онлайн оплата»: если доступен ровно один способ — сразу он, иначе
    // покупатель выбирает плитку
    if (code === "online") {
      if (state.payment === "cash") {
        const ready = state.providers || [];
        state.payment = ready.length === 1 ? ready[0] : "online";
      }
    } else {
      state.payment = code;
    }
    el("checkout-error").classList.add("hidden");
    return renderCheckout();
  }
  const pick = event.target.closest("[data-pick]");
  if (pick) {
    const provider = pick.dataset.pick;
    if (!(state.providers || []).includes(provider)) {
      return note(t("soon_note", { name: PROVIDER_NAME[provider] || provider }));
    }
    state.payment = provider;
    el("checkout-error").classList.add("hidden");
    return renderCheckout();
  }
  if (event.target.closest("#to-checkout")) {
    // ключ живёт до конца попытки, в том числе если приложение свернули
    // и открыли заново: повторная отправка не заведёт второй заказ
    if (!state.clientKey) {
      state.clientKey = crypto.randomUUID();
      persistCart();
    }
    state.cartNotice = "";
    el("checkout-error").classList.add("hidden");   // ошибка прошлой попытки
    renderCheckout();
    return show("checkout");
  }
  if (event.target.closest("#submit-order")) return submitOrder();

  // переходы между экранами
  const go = event.target.closest("[data-go]");
  if (go) return go.dataset.go === "cart" ? openCart() : show(go.dataset.go);
});

/** Смена языка: тексты разметки и всё, что на экране сейчас, — заново.
 *  Набранное в полях не трогаем. */
function setLang(lang) {
  if (lang === LANG || !I18N[lang]) return;
  LANG = lang;
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch (error) {
    console.error(error);   // приватный режим — выбор продержится до закрытия
  }
  applyStatic();
  // показанная ошибка — на прежнем языке: прячем, при новой попытке появится заново
  el("checkout-error").classList.add("hidden");
  el("profile-error").classList.add("hidden");
  renderCategories();
  if (state.allProducts.length) showCurrentList();
  renderAddrBar();
  renderPoint("f");
  renderPoint("p");
  if (!state.savingProfile) el("save-profile").textContent = t("save");
  if (state.screen === "cart") renderCart();
  if (state.screen === "checkout") renderCheckout();
  if (state.screen === "orders") openOrders();
  if (state.screen === "product" && state.product) renderProduct();
}

function changeCartQty(index, delta) {
  const item = state.cart[index];
  if (!item) return;
  item.qty = Math.min(MAX_QTY, item.qty + delta);
  if (item.qty < 1) state.cart.splice(index, 1);
  saveCart();
  renderCart();
}

/** Внутри Telegram приложение по умолчанию открывается в половину высоты,
 *  а свайп вниз закрывает его прямо посреди прокрутки каталога. */
function setupTelegram() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) return;   // открыли в обычном браузере
  try {
    tg.ready();
    tg.expand();
    tg.setHeaderColor("#E30613");
    tg.setBackgroundColor("#F5F5F5");
    if (tg.disableVerticalSwipes) tg.disableVerticalSwipes();
    if (tg.setBottomBarColor) tg.setBottomBarColor("#ffffff");
    if (tg.BackButton) {
      tg.BackButton.onClick(() => {
        // с карты возвращаемся к той форме, из которой её открыли: иначе
        // «назад» уносит из наполовину заполненного заказа в каталог
        if (state.screen === "map") return show(mapTarget === "f" ? "checkout" : "profile");
        const back = BACK_TO[state.screen] || "catalog";
        return back === "cart" ? openCart() : show(back);
      });
    }
    // на весь экран — только с Bot API 8.0. На клиенте постарше просто
    // останется обычная шторка: отступы считаются по тем же переменным
    if (tg.isVersionAtLeast && tg.isVersionAtLeast("8.0") && tg.requestFullscreen) {
      tg.requestFullscreen();
    }
  } catch (error) {
    console.error(error);   // старый клиент не знает часть команд — не повод падать
  }

  if (!tg.initData) return;   // открыто в браузере: заказов и профиля на него нет
  el("tab-orders").classList.remove("hidden");
  el("tab-profile").classList.remove("hidden");
  // имя придёт от Telegram, телефон и адрес — из прошлого заказа, если он был
  loadProfile();
  // можно ли этому покупателю платить картой — решает сервер
  loadPaymentOptions();
}

async function start() {
  applyStatic();
  setupTelegram();
  loadSavedCart();
  show("catalog");
  // стоимость доставки грузим вместе с каталогом: без неё итог в корзине
  // разошёлся бы с расчётом сервера
  try {
    const { delivery_price: price } = await api("/api/delivery-price");
    state.deliveryPrice = price;
    await loadCatalog();
  } catch (error) {
    fail(t("catalog_fail"), error);
  }
}

start();

/* Вход в административную панель.
 *
 * Mini App открывается всегда по корневому адресу, адресной строки внутри
 * Telegram нет, а в обычном браузере подпись не придёт и панель ответит 401.
 * Поэтому единственный способ туда попасть — перейти из самой витрины:
 * тот же источник, тот же WebView, подпись остаётся доступной.
 *
 * Кнопку видит только тот, кого сервер признал администратором. Проверка идёт
 * на сервере: скрыть кнопку на клиенте — не защита, доступ всё равно решает он.
 */
(function () {
  'use strict';
  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.initData) return;

  fetch('/api/admin/whoami', { headers: { 'X-Telegram-Init-Data': tg.initData } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (me) {
      if (!me || !me.admin) return;
      var a = document.createElement('a');
      a.href = '/admin?tg=1';   // подпись проверит сама панель
      a.textContent = t('admin_panel');
      a.style.cssText = 'position:fixed;left:10px;bottom:76px;z-index:900;' +
        'background:#E30613;color:#fff;border-radius:18px;padding:7px 14px;' +
        'font-size:12px;text-decoration:none;box-shadow:0 3px 12px rgba(0,0,0,.25)';
      document.body.appendChild(a);
    })
    .catch(function () {});   // витрине эта кнопка не нужна: молча пропускаем
})();
