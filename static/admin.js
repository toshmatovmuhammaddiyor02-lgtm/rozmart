/* Административная панель ROZMART: товары (SA-01) и заказы (SA-04).
 *
 * Открывается из Telegram: подпись оттуда уходит заголовком X-Telegram-Init-Data,
 * тем же, что и в витрине. Сервер сверяет её и проверяет, что Telegram-id есть
 * в списке администраторов. Посторонний получает 404, а не 403 — чтобы по ответу
 * нельзя было понять, что панель вообще существует.
 */
(function () {
  'use strict';

  var tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }

  var state = { view: 'products', status: 'new', q: '', category: '',
                orderStatus: '', statuses: {} };
  var timer = null;

  // ------------------------------------------------------------------ запросы

  function api(path, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    if (tg && tg.initData) opts.headers['X-Telegram-Init-Data'] = tg.initData;
    return fetch('/api/admin' + path, opts).then(function (r) {
      if (r.status === 404 && !opts.quiet) {
        throw new Error('Нет доступа. Ваш Telegram не в списке администраторов.');
      }
      if (r.status === 401) {
        // из браузера показываем форму входа, из Telegram — объясняем словами:
        // там формы быть не должно, подпись уже есть, дело в списке админов
        if (!(tg && tg.initData)) { location.href = '/admin/login'; }
        throw new Error('Нет доступа к панели.');
      }
      return r.json().then(function (body) {
        if (!r.ok) throw new Error(body.detail || ('Ошибка ' + r.status));
        return body;
      });
    });
  }

  function say(text, kind) {
    var box = document.getElementById('msg');
    if (!text) { box.innerHTML = ''; return; }
    box.innerHTML = '<div class="msg ' + (kind || 'err') + '"></div>';
    box.firstChild.textContent = text;
    if (kind === 'ok') setTimeout(function () { box.innerHTML = ''; }, 2500);
  }

  function money(v) {
    return v == null ? '—' : String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' сум';
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;   // textContent, не innerHTML: в названиях из REGOS
    return e;                                  // встречается что угодно, включая угловые скобки
  }

  // ------------------------------------------------------------------ товары

  function loadProducts() {
    // с поиском — по всем товарам: ищут обычно по названию или коду и не
    // помнят, опубликован товар или нет. Вкладка снова действует, когда поле пустое
    var url = '/products?status=' + (state.q ? 'all' : state.status) + '&limit=200';
    if (state.q) url += '&q=' + encodeURIComponent(state.q);
    if (state.category) url += '&category_id=' + state.category;
    api(url).then(renderProducts).catch(function (e) { say(e.message); });
    api('/products/count').then(function (c) {
      document.getElementById('cntProducts').textContent = c.hidden + ' / ' + c.total;
    }).catch(function () {});
  }

  function renderProducts(rows) {
    var box = document.getElementById('products');
    box.innerHTML = '';

    var at = document.getElementById('productsAt');
    if (at) {
      at.textContent = 'обновлено в ' + new Date().toLocaleTimeString('ru-RU',
        { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' · товаров ' + rows.length + (state.q ? ' · поиск по всем вкладкам' : '');
    }
    document.querySelectorAll('.tab[data-status]').forEach(function (t) {
      t.style.opacity = state.q ? '.45' : '';
    });

    if (!rows.length) {
      box.appendChild(el('div', 'empty', 'Ничего не найдено'));
      return;
    }
    rows.forEach(function (p) {
      var card = el('div', 'card');
      if (p.photo) {
        var img = document.createElement('img');
        img.src = p.photo; img.alt = '';
        card.appendChild(img);
      } else {
        card.appendChild(el('div', 'ph', '—'));
      }
      var info = el('div');
      info.appendChild(el('div', 'nm', p.name));

      var meta = el('div', 'meta');
      meta.appendChild(el('span', 'pill ' + (p.is_active ? 'ok' : 'no'),
        p.is_active ? 'на витрине' : 'скрыт'));
      if (!p.sellable) meta.appendChild(el('span', 'pill warn', 'нет цены'));
      info.appendChild(meta);

      info.appendChild(el('div', 'meta',
        p.category + ' · фасовок ' + p.variant_count +
        (p.min_price != null ? ' · от ' + money(p.min_price) : '')));
      card.appendChild(info);
      card.onclick = function () { openProduct(p.id); };
      box.appendChild(card);
    });
  }

  // Отбор по категории. Список тянем один раз: категорий единицы, и дёргать
  // сервер на каждое открытие вкладки незачем.
  function loadCategoryFilter() {
    api('/categories').then(function (cats) {
      var sel = document.getElementById('catFilter');
      if (!sel) return;
      var current = state.category;
      sel.innerHTML = '';
      var all = document.createElement('option');
      all.value = ''; all.textContent = 'Все категории';
      sel.appendChild(all);
      cats.forEach(function (c) {
        var o = document.createElement('option');
        o.value = c.id;
        o.textContent = c.name + ' (' + c.product_count + ')';
        if (String(c.id) === String(current)) o.selected = true;
        sel.appendChild(o);
      });
    }).catch(function () {});   // без фильтра список товаров всё равно работает
  }

  function openProduct(id) {
    Promise.all([api('/products/' + id), api('/categories')])
      .then(function (r) { renderProduct(r[0], r[1]); })
      .catch(function (e) { say(e.message); });
  }

  // Фасовка с ценой. Своя цена действует вместо цены REGOS, и синхронизация
  // её не трогает — поэтому рядом всегда видно, сколько стоит в REGOS:
  // иначе забытая своя цена разойдётся с кассой, и никто не заметит
  function variantRow(productId, v) {
    var row = el('div');
    row.style.cssText = 'padding:9px 0;border-bottom:1px solid #F0F0F0';

    var top = el('div');
    top.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap';
    top.appendChild(el('span', null, v.weight));
    var input = document.createElement('input');
    input.type = 'number'; input.min = '1'; input.step = '1';
    input.style.cssText = 'max-width:130px';
    input.value = v.price != null ? v.price : '';
    input.placeholder = v.regos_price != null ? String(v.regos_price) : 'цена';
    top.appendChild(input);
    top.appendChild(el('span', 'meta', 'сум'));

    var save = el('button', 'act ghost', 'Сохранить цену');
    save.onclick = function () {
      var value = input.value.trim();
      if (!value) return say('Укажите цену или верните цену REGOS');
      setPrice(v.id, Number(value));
    };
    top.appendChild(save);
    if (v.manual_price != null) {
      var reset = el('button', 'act ghost', 'Вернуть цену REGOS');
      reset.onclick = function () { setPrice(v.id, null); };
      top.appendChild(reset);
    }
    row.appendChild(top);

    var source = v.manual_price != null
      ? 'своя цена · в REGOS ' + (v.regos_price != null ? money(v.regos_price) : 'цены нет')
      : (v.regos_price != null ? 'цена REGOS' : 'в REGOS цены нет — фасовка не продаётся');
    row.appendChild(el('div', 'meta', source + ' · код ' + v.external_code +
      (v.barcode ? ' · ш/к ' + v.barcode : '') + (v.is_active ? '' : ' · скрыта')));
    row.appendChild(fiscalRow(v));
    return row;
  }

  // ИКПУ и код упаковки — для фискального чека Payme и Click. Обычно приходят
  // из REGOS; пусто там — можно вписать здесь, REGOS потом заменит своим
  function fiscalRow(v) {
    var box = el('div');
    box.style.cssText = 'display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-top:8px';
    function input(label, value, placeholder, width) {
      var w = el('div');
      w.appendChild(el('div', 'meta', label));
      var i = document.createElement('input');
      i.value = value || '';
      i.placeholder = placeholder;
      i.inputMode = 'numeric';
      i.spellcheck = false;
      i.style.cssText = 'max-width:' + width + 'px';
      w.appendChild(i);
      box.appendChild(w);
      return i;
    }
    var mxik = input('ИКПУ', v.mxik, '17 цифр', 200);
    var pack = input('Код упаковки', v.package_code, 'цифры', 140);
    var save = el('button', 'act ghost', 'Сохранить коды');
    save.onclick = function () {
      save.disabled = true;
      api('/variants/' + v.id + '/fiscal', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mxik: mxik.value.trim(), package_code: pack.value.trim() })
      }).then(function (p) {
        say('ИКПУ и код упаковки сохранены', 'ok');
        return api('/categories').then(function (cats) { renderProduct(p, cats); });
      }).catch(function (e) { say(e.message); save.disabled = false; });
    };
    box.appendChild(save);
    var wrap = el('div');
    wrap.appendChild(box);
    var missing = [!v.mxik && 'ИКПУ', !v.package_code && 'кода упаковки'].filter(Boolean);
    wrap.appendChild(el('div', 'meta', (missing.length
      ? 'Нет ' + missing.join(' и ') + ' — чек Payme/Click может не уйти в налоговую. '
      : '') + 'НДС ' + (v.vat_percent != null ? v.vat_percent + '%' : 'не указан (в чеке 12%)')
      + '. Заполненное в REGOS заменит вписанное здесь.'));
    return wrap;
  }

  function setPrice(variantId, price) {
    api('/variants/' + variantId + '/price', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price: price })
    }).then(function (p) {
      say(price == null ? 'Вернули цену REGOS' : 'Цена сохранена', 'ok');
      loadProducts();
      return api('/categories').then(function (cats) { renderProduct(p, cats); });
    }).catch(function (e) { say(e.message); });
  }

  function renderProduct(p, categories) {
    document.getElementById('paneTtl').textContent = 'Карточка товара';
    var body = document.getElementById('paneBody');
    body.innerHTML = '';

    function field(label, node) {
      var f = el('div', 'fld');
      f.appendChild(el('label', null, label));
      f.appendChild(node);
      body.appendChild(f);
      return node;
    }

    var name = document.createElement('input');
    name.value = p.name;
    field('Название для покупателя', name);

    var cat = document.createElement('select');
    categories.forEach(function (c) {
      var o = document.createElement('option');
      o.value = c.id; o.textContent = c.name;
      if (c.id === p.category_id) o.selected = true;
      cat.appendChild(o);
    });
    field('Категория', cat);

    var desc = document.createElement('textarea'); desc.value = p.description || '';
    field('Описание', desc);
    var comp = document.createElement('textarea'); comp.value = p.composition || '';
    field('Состав', comp);
    var life = document.createElement('input'); life.value = p.shelf_life || '';
    field('Срок годности', life);

    // Фото: видно текущее, иначе администратор не знает, что заменяет.
    // Адрес приходит с отметкой версии, поэтому после замены показывается
    // новая картинка, а не та, что осела в кеше браузера.
    var photoBox = el('div');
    photoBox.style.cssText = 'display:flex;gap:12px;align-items:flex-start';

    var preview = document.createElement('img');
    preview.style.cssText = 'width:96px;height:96px;object-fit:cover;border-radius:10px;' +
      'background:#F2F2F2;flex:none;border:1px solid #ECECEC';
    if (p.photo) {
      preview.src = p.photo;
    } else {
      preview.replaceWith();
      preview = el('div');
      preview.style.cssText = 'width:96px;height:96px;border-radius:10px;background:#F2F2F2;' +
        'flex:none;display:flex;align-items:center;justify-content:center;color:#BBB;font-size:12px';
      preview.textContent = 'нет фото';
    }
    photoBox.appendChild(preview);

    var side = el('div');
    side.style.cssText = 'flex:1;min-width:0';
    var photo = document.createElement('input');
    photo.type = 'file'; photo.accept = 'image/jpeg,image/png,image/webp';
    side.appendChild(photo);
    side.appendChild(el('div', 'meta', 'JPEG, PNG или WebP, до 4 МБ'));

    if (p.photo) {
      var drop = el('button', 'act ghost', 'Удалить фото');
      drop.style.marginTop = '8px';
      drop.onclick = function () {
        if (!window.confirm('Убрать фотографию с карточки?')) return;
        api('/products/' + p.id + '/photo', { method: 'DELETE' })
          .then(function () { say('Фото удалено', 'ok'); openProduct(p.id); loadProducts(); })
          .catch(function (e) { say(e.message); });
      };
      side.appendChild(drop);
    }
    photoBox.appendChild(side);
    field('Фотография', photoBox);

    photo.onchange = function () {
      if (!photo.files.length) return;
      var fd = new FormData();
      fd.append('file', photo.files[0]);
      say('Загружаем…', 'ok');
      api('/products/' + p.id + '/photo', { method: 'POST', body: fd })
        .then(function () { say('Фото обновлено', 'ok'); openProduct(p.id); loadProducts(); })
        .catch(function (e) { say(e.message); });
    };

    // Фасовки. Показываем код REGOS — администратору он нужен, чтобы сверяться
    // с учётной системой. Покупателю этот код не показывается нигде (BR-36).
    var vbox = el('div');
    p.variants.forEach(function (v) { vbox.appendChild(variantRow(p.id, v)); });
    field('Фасовки из REGOS (' + p.variants.length + ')', vbox);

    var moveTo = document.createElement('input');
    moveTo.placeholder = 'id карточки-получателя';
    if (p.variants.length) {
      field('Перенести первую фасовку в другую карточку', moveTo);
      var moveBtn = el('button', 'act ghost', 'Перенести ' + p.variants[0].weight);
      moveBtn.onclick = function () {
        if (!moveTo.value) return say('Укажите id карточки');
        api('/variants/' + p.variants[0].id + '/move?target_product_id=' + moveTo.value,
            { method: 'POST' })
          .then(function () { say('Перенесено', 'ok'); closePane(); loadProducts(); })
          .catch(function (e) { say(e.message); });
      };
      body.appendChild(moveBtn);
    }

    body.appendChild(el('div', 'meta', 'id карточки: ' + p.id));

    var foot = document.getElementById('paneFoot');
    foot.innerHTML = '';
    var save = el('button', 'act', 'Сохранить');
    save.onclick = function () {
      api('/products/' + p.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.value.trim(), category_id: Number(cat.value),
          description: desc.value.trim() || null, composition: comp.value.trim() || null,
          shelf_life: life.value.trim() || null
        })
      }).then(function () { say('Сохранено', 'ok'); closePane(); loadProducts(); })
        .catch(function (e) { say(e.message); });
    };
    foot.appendChild(save);

    var pub = el('button', 'act ' + (p.is_active ? 'ghost' : ''),
      p.is_active ? 'Снять с витрины' : 'Опубликовать');
    pub.disabled = !p.is_active && !p.sellable;
    pub.title = pub.disabled ? 'Нет цены ни у одной фасовки' : '';
    pub.onclick = function () {
      api('/products/' + p.id, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ is_active: !p.is_active })
      }).then(function () {
        say(p.is_active ? 'Снято с витрины' : 'Опубликовано', 'ok');
        closePane(); loadProducts();
      }).catch(function (e) { say(e.message); });
    };
    foot.appendChild(pub);

    openPane();
  }

  // ------------------------------------------------------------------ заказы

  function loadOrders() {
    var url = '/orders?limit=100';
    if (state.orderStatus) url += '&status=' + state.orderStatus;
    if (state.q) url += '&q=' + encodeURIComponent(state.q);
    api(url).then(renderOrders).catch(function (e) { say(e.message); });
  }

  function renderOrders(rows) {
    var body = document.getElementById('orders');
    body.innerHTML = '';

    // Отметка времени: по ней видно, насколько список свежий. Без неё после
    // нажатия «Обновить» непонятно, отработало оно или нет — особенно когда
    // ничего не изменилось.
    var at = document.getElementById('ordersAt');
    if (at) {
      at.textContent = 'обновлено в ' + new Date().toLocaleTimeString('ru-RU',
        { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' · заказов ' + rows.length;
    }

    if (!rows.length) {
      var tr = el('tr'); var td = el('td', 'empty', 'Заказов нет');
      td.colSpan = 7; tr.appendChild(td); body.appendChild(tr);
      return;
    }
    rows.forEach(function (o) {
      var tr = el('tr', 'row');
      [o.number,
       new Date(o.created_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }),
       o.customer_name + ' · ' + o.phone,
       String(o.item_count),
       money(o.total),
       o.status_text,
       o.regos_document_id ? '№ ' + o.regos_document_id : '—'
      ].forEach(function (v) { tr.appendChild(el('td', null, v)); });
      tr.onclick = function () { openOrder(o.id); };
      body.appendChild(tr);
    });
  }

  function renderOrderTabs() {
    var box = document.getElementById('orderTabs');
    box.innerHTML = '';
    var all = el('button', 'tab' + (state.orderStatus ? '' : ' on'), 'Все');
    all.onclick = function () { state.orderStatus = ''; renderOrderTabs(); loadOrders(); };
    box.appendChild(all);
    Object.keys(state.statuses).forEach(function (code) {
      var b = el('button', 'tab' + (state.orderStatus === code ? ' on' : ''),
                 state.statuses[code]);
      b.onclick = function () { state.orderStatus = code; renderOrderTabs(); loadOrders(); };
      box.appendChild(b);
    });
  }

  // Про деньги — без полутонов: «онлайн» без слова «оплачено» кассир
  // прочитал бы как «оплатит онлайн когда-нибудь»
  function paymentText(o) {
    if (o.payment_method === 'cash') return 'наличные курьеру';
    if (!o.paid_at) {
      return o.status === 'CANCELED' ? 'картой — не оплачен, заказ отменён'
                                     : 'картой — ждёт оплаты';
    }
    var when = new Date(o.paid_at).toLocaleString('ru-RU');
    return 'картой — ОПЛАЧЕНО ' + when + ', ' + money(o.paid_amount)
      + ' · платёж ' + (o.provider_charge_id || o.payment_charge_id);
  }

  function openOrder(id) {
    api('/orders/' + id).then(function (o) {
      document.getElementById('paneTtl').textContent = 'Заказ ' + o.number;
      var body = document.getElementById('paneBody');
      body.innerHTML = '';

      [['Состояние', o.status_text],
       ['В REGOS', o.regos_document_id
          ? 'документ № ' + o.regos_document_id
          : (o.regos_error ? 'не выгружен: ' + o.regos_error
                           : 'ещё не выгружен (попыток ' + o.regos_attempts + ')')],
       ['Покупатель', o.customer_name],
       ['Телефон', o.phone],
       // @имя ссылкой: оператору по нему писать, а не читать его
       ['Telegram', o.telegram_username ? '@' + o.telegram_username : '—',
        o.telegram_username ? 'https://t.me/' + o.telegram_username : null],
       ['Адрес', o.address],
       // точка с карты: по ней курьер находит дом, по адресу — подъезд
       ['На карте', o.lat != null ? o.lat.toFixed(6) + ', ' + o.lon.toFixed(6) : 'не указана',
        o.lat != null ? 'https://maps.google.com/?q=' + o.lat + ',' + o.lon : null],
       ['Доставка', o.delivery_slot],
       ['Оплата', paymentText(o)],
       ['Доставщик', o.courier_name
          ? o.courier_name + ' · взял ' + new Date(o.courier_taken_at).toLocaleString('ru-RU')
            + (o.delivered_at ? ' · доставил ' + new Date(o.delivered_at).toLocaleString('ru-RU') : '')
          : '—'],
       ['Комментарий', o.comment || '—'],
       ['Отказ доставщика', o.courier_drop_note
          ? (o.courier_drop_by || 'доставщик') + ': ' + o.courier_drop_note : '—'],
       ['Оформлен', new Date(o.created_at).toLocaleString('ru-RU')]
      ].forEach(function (pair) {
        var f = el('div', 'fld');
        f.appendChild(el('label', null, pair[0]));
        if (pair[2]) {
          var a = el('a', null, pair[1]);
          a.href = pair[2];
          a.target = '_blank';
          a.rel = 'noopener';
          f.appendChild(a);
        } else {
          f.appendChild(el('div', null, pair[1]));
        }
        body.appendChild(f);
      });

      var t = document.createElement('table');
      t.innerHTML = '<thead><tr><th>Товар</th><th>Фасовка</th><th>Кол-во</th><th>Сумма</th></tr></thead>';
      var tb = document.createElement('tbody');
      o.items.forEach(function (i) {
        var tr = el('tr');
        [i.product_name, i.weight, String(i.quantity), money(i.sum)]
          .forEach(function (v) { tr.appendChild(el('td', null, v)); });
        tb.appendChild(tr);
      });
      var tr = el('tr');
      tr.appendChild(el('td', null, 'Доставка')); tr.appendChild(el('td'));
      tr.appendChild(el('td')); tr.appendChild(el('td', null, money(o.delivery_price)));
      tb.appendChild(tr);
      var total = el('tr');
      total.appendChild(el('td', null, 'Итого')); total.appendChild(el('td'));
      total.appendChild(el('td')); total.appendChild(el('td', null, money(o.total)));
      tb.appendChild(total);
      t.appendChild(tb);
      body.appendChild(t);

      // Кнопки только для разрешённых переходов: сервер всё равно проверит,
      // но показывать заведомо невозможное действие — врать оператору
      var foot = document.getElementById('paneFoot');
      foot.innerHTML = '';

      // Ручная отправка — на случай, когда автоматическая исчерпала попытки
      // или оператор устранил причину отказа
      // неоплаченный заказ картой уйдёт в REGOS сам, когда придёт оплата;
      // кнопка здесь была лазейкой отдать его кассиру без денег
      if (!o.regos_document_id && !o.awaiting_payment && o.status !== 'CANCELED') {
        var push = el('button', 'act ghost', 'Отправить в REGOS');
        push.onclick = function () {
          push.disabled = true;
          push.textContent = 'Отправляем…';
          api('/orders/' + o.id + '/push', { method: 'POST' })
            .then(function (r) {
              say('Выгружен, документ № ' + r.document_id, 'ok');
              closePane(); loadOrders();
            })
            .catch(function (e) {
              say(e.message);
              push.disabled = false;
              push.textContent = 'Отправить в REGOS';
            });
        };
        foot.appendChild(push);
      }

      (state.transitions[o.status] || []).forEach(function (code) {
        // сервер всё равно откажет, но показывать заведомо невозможное — врать
        if (code === 'CONFIRMED' && o.awaiting_payment) return;
        var b = el('button', 'act ' + (code === 'CANCELED' ? 'ghost' : ''),
                   state.statuses[code]);
        b.onclick = function () {
          if (code === 'CANCELED' && o.checkout_open && !confirm(
              'Покупатель прямо сейчас в окне оплаты. Если деньги успеют списаться, '
              + 'заказ останется отменённым и понадобится возврат. Всё равно отменить?')) {
            return;
          }
          api('/orders/' + o.id + '/status', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: code })
          }).then(function () { say('Состояние изменено', 'ok'); closePane(); loadOrders(); })
            .catch(function (e) { say(e.message); });
        };
        foot.appendChild(b);
      });

      // Удаление — необратимое действие, поэтому стоит последним, выглядит
      // неприметно и спрашивает подтверждение с номером заказа: нажать его
      // вместо соседней кнопки не должно быть легко
      var del = el('button', 'act ghost', 'Удалить');
      del.style.marginLeft = 'auto';
      del.style.color = '#B3050F';
      del.onclick = function () {
        if (!confirm('Удалить заказ ' + o.number + '?\n\n'
                     + 'Заказ исчезнет из витрины вместе с составом и суммой, '
                     + 'а в REGOS документ будет отменён. Отменить удаление нельзя.')) {
          return;
        }
        del.disabled = true;
        del.textContent = 'Удаляем…';
        api('/orders/' + o.id, { method: 'DELETE' })
          .then(function (r) {
            say('Заказ ' + r.deleted + ' удалён — ' + r.regos, 'ok');
            closePane(); loadOrders();
          })
          .catch(function (e) {
            say(e.message);
            del.disabled = false;
            del.textContent = 'Удалить';
          });
      };
      foot.appendChild(del);
      openPane();
    }).catch(function (e) { say(e.message); });
  }


  // ------------------------------------------------------------- состав каталога

  function loadSettings() {
    api('/categories').then(renderCats).catch(function (e) { say(e.message); });
    api('/regos/groups').then(renderGroups).catch(function (e) { say(e.message); });
    api('/regos/auto').then(renderAuto).catch(function (e) { say(e.message); });
    loadChats();
    loadPriceTypes();
    loadDelivery();
  }

  // --------------------------------------------------- онлайн-оплата: вкл/выкл

  function loadPaySwitches() {
    api('/payments').then(renderPayments).catch(function (e) {
      document.getElementById('payCards').textContent = 'Не загрузилось: ' + e.message;
    });
  }

  // форма оплаты, с которой заказ приходит на кассу REGOS: кассиру остаётся
  // «Закрыть чек». Не выбрана — «Пласт. карта»
  var REGOS_HINT = 'С ней оплаченный заказ приходит на кассу. Не выбрано — «Пласт. карта»';
  var regosTypes = [];

  // поля каждого способа: [имя настройки, подпись, вид, подсказка]
  var PAY_FIELDS = {
    payme: [
      ['PAYME_MERCHANT_ID', 'ID кассы', 'masked', '24 символа, из кабинета Payme Business'],
      ['PAYME_KEY', 'Ключ кассы', 'secret', 'Боевой или тестовый — смотря по режиму ниже'],
      ['REGOS_PAY_PAYME', 'Форма оплаты в REGOS', 'regos', REGOS_HINT],
      ['FEE_PAYME', 'Комиссия, %', 'text', 'По договору. Для отчёта «Онлайн-платежи»'],
      ['PAYME_TEST', 'Тестовый режим (песочница Payme)', 'bool',
       'Выключено — настоящие деньги']
    ],
    paynet: [
      ['PAYNET_SERVICE_ID', 'Номер сервиса (serviceId)', 'text', 'Выдаёт Paynet'],
      ['PAYNET_LOGIN', 'Логин', 'text', 'Задаём сами и передаём Paynet'],
      ['PAYNET_PASSWORD', 'Пароль', 'secret', 'Задаём сами и передаём Paynet лично, не в группу'],
      ['PAYNET_PAY_URL', 'Ссылка на оплату в Paynet', 'text',
       'Если Paynet даст: {service}, {order}, {amount}. Пусто — покупатель вводит номер сам'],
      ['REGOS_PAY_PAYNET', 'Форма оплаты в REGOS', 'regos', REGOS_HINT],
      ['FEE_PAYNET', 'Комиссия, %', 'text', 'По договору. Для отчёта «Онлайн-платежи»'],
      ['PAYNET_TEST', 'Тестовый режим', 'bool', 'Выключено — настоящие деньги']
    ],
    click: [
      ['CLICK_SERVICE_ID', 'Service ID', 'text', 'Из кабинета merchant.click.uz'],
      ['CLICK_MERCHANT_ID', 'Merchant ID', 'text', 'Из кабинета merchant.click.uz'],
      ['CLICK_MERCHANT_USER_ID', 'Merchant user ID', 'text', 'Нужен для чека в налоговую и возвратов'],
      ['CLICK_SECRET_KEY', 'Секретный ключ', 'secret', 'Из кабинета, в чаты не пересылать'],
      ['CLICK_TIN', 'ИНН продавца', 'text', 'Для чека: 9 цифр'],
      ['CLICK_DELIVERY_SPIC', 'ИКПУ доставки', 'text', 'Для чека, если доставка платная: 17 цифр'],
      ['CLICK_DELIVERY_PACKAGE', 'Код упаковки доставки', 'text', 'Для чека, если доставка платная'],
      ['REGOS_PAY_CLICK', 'Форма оплаты в REGOS', 'regos', REGOS_HINT],
      ['FEE_CLICK', 'Комиссия, %', 'text', 'По договору. Для отчёта «Онлайн-платежи»'],
      ['CLICK_PILOT', 'Пилот: видят только тестировщики', 'bool',
       'Тестовой среды у Click нет — платежи всегда настоящие. Выключите, чтобы открыть всем']
    ]
  };

  var SOURCE = { panel: 'задано в панели', server: 'из настроек сервера', none: 'не задано' };

  function payField(name, title, kind, hint, cfg) {
    var f = el('div', 'fld');
    var c = cfg[name] || {};
    var label = el('label', null, title);
    label.appendChild(el('span', 'src', '· ' + (SOURCE[c.source] || '')));
    f.appendChild(label);
    var input = document.createElement('input');
    input.dataset.name = name;
    if (kind === 'regos') {
      var sel = document.createElement('select');
      sel.dataset.name = name;
      var current = c.value || '';
      var opts = [['', 'Пласт. карта (по умолчанию)']].concat(regosTypes.filter(function (t) {
        return !t.cash;
      }).map(function (t) { return [String(t.id), t.name + ' (№' + t.id + ')']; }));
      if (current && !opts.some(function (o) { return o[0] === current; })) {
        opts.push([current, '№' + current + ' — нет в списке REGOS']);
      }
      opts.forEach(function (o) {
        var op = document.createElement('option');
        op.value = o[0]; op.textContent = o[1]; op.selected = o[0] === current;
        sel.appendChild(op);
      });
      f.appendChild(sel);
      if (hint) f.appendChild(el('div', 'hint', hint));
      return f;
    }
    if (kind === 'bool') {
      var wrap = el('label', 'sw');
      input.type = 'checkbox';
      input.checked = c.value === '1';
      input.dataset.kind = 'bool';
      wrap.appendChild(input);
      wrap.appendChild(document.createElement('span'));
      f.appendChild(wrap);
    } else {
      input.type = kind === 'secret' ? 'password' : 'text';
      // Браузер принимает пару «текст + пароль» за форму входа и подставляет
      // логин и пароль от панели — так в «Ключ кассы» однажды попало не то.
      // new-password и «только чтение до касания» отбивают автозаполнение
      input.autocomplete = kind === 'secret' ? 'new-password' : 'off';
      input.name = 'pay-' + name.toLowerCase() + '-' + Date.now();
      input.setAttribute('data-lpignore', 'true');
      input.setAttribute('data-1p-ignore', 'true');
      input.readOnly = true;
      input.addEventListener('focus', function () { input.readOnly = false; });
      input.spellcheck = false;
      // секрет и ID кассы — только хвостом; поле пустое, вписанное заменяет
      if (kind === 'secret' || kind === 'masked') {
        input.placeholder = c.set ? 'задан ' + c.masked + ' — впишите новый, чтобы заменить'
                                  : 'не задан';
        input.dataset.kind = 'secret';
      } else {
        input.value = c.value || '';
      }
      f.appendChild(input);
    }
    if (hint) f.appendChild(el('div', 'hint', hint));
    return f;
  }

  function collect(card) {
    var values = {};
    card.querySelectorAll('select[data-name]').forEach(function (s) {
      values[s.dataset.name] = s.value;
    });
    card.querySelectorAll('input[data-name]').forEach(function (i) {
      if (i.dataset.kind === 'bool') values[i.dataset.name] = i.checked ? '1' : '0';
      else if (i.dataset.kind === 'secret') { if (i.value.trim()) values[i.dataset.name] = i.value.trim(); }
      else values[i.dataset.name] = i.value.trim();
    });
    return values;
  }

  function saveConfig(values, done) {
    // Ключ и режим Payme — это живые деньги: переспрашиваем
    if ((values.PAYME_KEY || values.PAYME_MERCHANT_ID) && !confirm(
        'Заменить ' + (values.PAYME_KEY ? 'ключ' : 'ID') + ' кассы Payme?\n\n'
        + 'Если он не совпадает с кабинетом Payme Business, оплаты у покупателей '
        + 'перестанут проходить.')) return;
    if (values.CLICK_SECRET_KEY && !confirm('Заменить секретный ключ Click? Если он не совпадает '
        + 'с кабинетом merchant.click.uz, оплаты через Click перестанут проходить.')) return;
    if (values.PAYNET_PASSWORD && !confirm('Заменить пароль Paynet? Старый перестанет работать — '
        + 'новый нужно сразу передать Paynet.')) return;
    api('/payments/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: values })
    }).then(function (r) { say('Сохранено', 'ok'); renderPayments(r); if (done) done(); })
      .catch(function (e) { say(e.message); });
  }

  function switchFor(p) {
    var label = document.createElement('label');
    label.className = 'sw';
    label.title = p.on ? 'Выключить' : 'Включить';
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = p.on;
    box.onchange = function () {
      var on = box.checked;
      if (!on && !confirm('Выключить ' + p.title + '? Покупатели перестанут видеть этот способ '
          + 'оплаты. Начатые оплаты пройдут.')) { box.checked = true; return; }
      box.disabled = true;
      api('/payments/' + p.provider, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ on: on })
      }).then(function () {
        say(p.title + (on ? ' включён' : ' выключен'), 'ok');
        loadPaySwitches();
      }).catch(function (e) { say(e.message); box.checked = !on; box.disabled = false; });
    };
    label.appendChild(box);
    label.appendChild(document.createElement('span'));
    return label;
  }

  function renderPayments(data) {
    var box = document.getElementById('payCards');
    box.innerHTML = '';
    var cfg = data.config || {};
    regosTypes = data.regos_payment_types || [];
    data.providers.forEach(function (p) {
      var card = el('div', 'pcard');
      var hd = el('div', 'pcard-hd');
      hd.appendChild(el('div', 'ttl', p.title));
      hd.appendChild(switchFor(p));
      card.appendChild(hd);
      var state = !p.configured ? 'не подключён — покупатели видят «Скоро»'
        : !p.on ? 'выключен — покупатели видят «Скоро»'
        : p.testers_only ? 'включён, видят только тестировщики' + (p.test ? ' (тестовый режим)' : '')
        : 'включён — видят все покупатели';
      var pill = el('div', 'meta', state);
      pill.style.marginBottom = '14px';
      card.appendChild(pill);
      if (data.endpoints && data.endpoints[p.provider]) {
        var ep = el('div', 'hint');
        ep.style.marginBottom = '14px';
        var urls = [].concat(data.endpoints[p.provider]);
        ep.appendChild(document.createTextNode((urls.length > 1 ? 'Адреса' : 'Адрес') + ' для '
                                               + p.title + ': '));
        urls.forEach(function (u, i) {
          if (i) ep.appendChild(document.createTextNode(', '));
          ep.appendChild(el('code', null, u));
        });
        card.appendChild(ep);
      }
      var fields = PAY_FIELDS[p.provider] || [];
      var grid = el('div', 'row2');
      fields.forEach(function (f) {
        (f[2] === 'bool' ? card : grid).appendChild(payField(f[0], f[1], f[2], f[3], cfg));
      });
      card.insertBefore(grid, card.querySelector('.fld') || null);
      var actions = el('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:4px';
      var save = el('button', 'act', 'Сохранить');
      save.onclick = function () { saveConfig(collect(card)); };
      actions.appendChild(save);
      if (p.provider === 'paynet') {
        var gen = el('button', 'act ghost', 'Сгенерировать пароль');
        gen.onclick = function () {
          if (!confirm('Сгенерировать новый пароль для Paynet? Старый перестанет работать — '
              + 'новый нужно сразу передать Paynet.')) return;
          api('/payments/paynet-password', { method: 'POST' }).then(function (r) {
            renderPayments(r);
            prompt('Новый пароль Paynet — скопируйте и передайте Paynet лично. '
                   + 'Больше он показан не будет:', r.password);
          }).catch(function (e) { say(e.message); });
        };
        actions.appendChild(gen);
      }
      card.appendChild(actions);
      box.appendChild(card);
    });

    // общие: тестировщики и пилот
    var common = el('div', 'pcard');
    var hd2 = el('div', 'pcard-hd');
    hd2.appendChild(el('div', 'ttl', 'Тестировщики'));
    common.appendChild(hd2);
    common.appendChild(payField('TEST_USERS', 'Telegram-id тестировщиков', 'text',
      'Через запятую. Они видят способ в тестовом режиме и в пилоте', cfg));
    common.appendChild(payField('TESTERS_ONLY', 'Пилот: боевой способ видят только тестировщики',
      'bool', 'Включено — настоящая оплата открыта только им', cfg));
    var save2 = el('button', 'act', 'Сохранить');
    save2.onclick = function () { saveConfig(collect(common)); };
    common.appendChild(save2);
    box.appendChild(common);
  }

  // --------------------------------------------------- пользователи и роли

  var usersState = { roles: [], min: 10 };

  // пароль из 14 символов без похожих букв (l/1, O/0) — его диктуют и переписывают
  function makePassword() {
    var abc = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    var out = '';
    var buf = new Uint32Array(14);
    window.crypto.getRandomValues(buf);
    for (var i = 0; i < buf.length; i++) out += abc[buf[i] % abc.length];
    return out;
  }

  function roleSelect(current) {
    var sel = document.createElement('select');
    usersState.roles.forEach(function (r) {
      var o = document.createElement('option');
      o.value = r.role; o.textContent = r.title; o.selected = r.role === current;
      sel.appendChild(o);
    });
    if (!current) {
      var none = document.createElement('option');
      none.value = ''; none.textContent = 'нет роли'; none.selected = true;
      sel.insertBefore(none, sel.firstChild);
    }
    return sel;
  }

  function loadUsers() {
    api('/users').then(renderUsers).catch(function (e) { say(e.message); });
  }

  function patchUser(username, body, okText) {
    return api('/users/' + encodeURIComponent(username), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) { say(okText, 'ok'); renderUsers(r); })
      .catch(function (e) { say(e.message); loadUsers(); });
  }

  function renderUsers(r) {
    usersState.roles = r.roles;
    usersState.min = r.min_password;
    var me = state.me || {};
    var t = document.getElementById('usersTable');
    t.innerHTML = '';
    headRow(t, [['Логин'], ['Имя'], ['Роль'], ['Состояние'], ['Сейчас вошёл'], ['']]);
    r.users.forEach(function (u) {
      var tr = document.createElement('tr');
      cell(tr, u.username + (u.username === me.username ? ' (вы)' : ''));
      cell(tr, u.full_name || '—');
      var sel = roleSelect(u.role);
      sel.onchange = function () {
        if (!confirm('Сменить роль «' + u.username + '» на «' + sel.options[sel.selectedIndex].text
            + '»? Пользователю нужно будет войти заново.')) { sel.value = u.role; return; }
        patchUser(u.username, { role: sel.value }, 'Роль изменена');
      };
      cell(tr, '').appendChild(sel);
      cell(tr, '').appendChild(el('span', 'pill ' + (u.is_active ? 'ok' : 'no'),
                                  u.is_active ? 'действует' : 'отключён'));
      cell(tr, u.sessions ? 'да' : 'нет');
      var acts = cell(tr, '');
      var pass = el('button', 'act ghost', 'Новый пароль');
      pass.onclick = function () { openPasswordPane(u); };
      acts.appendChild(pass);
      var tog = el('button', 'act ' + (u.is_active ? 'ghost' : 'blue'),
                   u.is_active ? 'Отключить' : 'Включить');
      tog.onclick = function () {
        if (u.is_active && !confirm('Отключить «' + u.username + '»? Войти он больше не сможет, '
            + 'открытая панель закроется.')) return;
        patchUser(u.username, { is_active: !u.is_active },
                  u.is_active ? 'Пользователь отключён' : 'Пользователь включён');
      };
      acts.appendChild(tog);
      t.appendChild(tr);
    });

    var nr = document.getElementById('nuRole');
    if (!nr.options.length) {
      r.roles.forEach(function (x) {
        var o = document.createElement('option');
        o.value = x.role; o.textContent = x.title; o.selected = x.role === 'manager';
        nr.appendChild(o);
      });
    }
    document.getElementById('nuHint').textContent = 'Не короче ' + r.min_password
      + ' символов. Передайте пароль человеку лично, не в общий чат.';

    var info = document.getElementById('rolesInfo');
    info.innerHTML = '';
    r.roles.forEach(function (x) {
      var d = el('div', 'rolerow');
      d.appendChild(el('b', null, x.title + ': '));
      d.appendChild(document.createTextNode(x.sections.join(', ')));
      info.appendChild(d);
    });
  }

  document.getElementById('nuGen').onclick = function () {
    var f = document.getElementById('nuPass');
    f.value = makePassword();
    f.type = 'text';
  };
  document.getElementById('nuCreate').onclick = function (e) {
    var body = {
      username: document.getElementById('nuLogin').value.trim().toLowerCase(),
      full_name: document.getElementById('nuName').value.trim(),
      role: document.getElementById('nuRole').value,
      password: document.getElementById('nuPass').value
    };
    e.target.disabled = true;
    api('/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (r) {
      say('Пользователь «' + body.username + '» создан. Передайте ему логин и пароль лично.', 'ok');
      ['nuLogin', 'nuName', 'nuPass'].forEach(function (id) { document.getElementById(id).value = ''; });
      renderUsers(r);
    }).catch(function (err) { say(err.message); })
      .then(function () { e.target.disabled = false; });
  };

  function passField(label, id, hint) {
    var f = el('div', 'fld');
    f.appendChild(el('label', null, label));
    var i = document.createElement('input');
    i.type = 'password'; i.id = id; i.autocomplete = 'new-password'; i.spellcheck = false;
    f.appendChild(i);
    if (hint) f.appendChild(el('div', 'hint', hint));
    return f;
  }

  // новый пароль другому пользователю (только администратор)
  function openPasswordPane(u) {
    document.getElementById('paneTtl').textContent = 'Новый пароль: ' + u.username;
    var body = document.getElementById('paneBody');
    body.innerHTML = '';
    body.appendChild(el('p', 'meta', 'Старый пароль перестанет работать, открытые сессии '
      + 'пользователя закроются. Передайте новый пароль лично.'));
    var f = passField('Новый пароль', 'rpPass', 'Не короче ' + usersState.min + ' символов');
    var gen = el('button', 'act ghost', 'Придумать');
    gen.style.marginTop = '6px';
    gen.onclick = function () {
      var i = document.getElementById('rpPass');
      i.value = makePassword(); i.type = 'text';
    };
    f.appendChild(gen);
    body.appendChild(f);
    var foot = document.getElementById('paneFoot');
    foot.innerHTML = '';
    var save = el('button', 'act', 'Сохранить пароль');
    save.onclick = function () {
      save.disabled = true;
      api('/users/' + encodeURIComponent(u.username) + '/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: document.getElementById('rpPass').value })
      }).then(function () {
        closePane(); say('Пароль «' + u.username + '» изменён', 'ok'); loadUsers();
      }).catch(function (e) { say(e.message); save.disabled = false; });
    };
    foot.appendChild(save);
    openPane();
  }

  // свой пароль — любой вошедший по логину
  document.getElementById('myPass').onclick = function () {
    document.getElementById('paneTtl').textContent = 'Сменить пароль';
    var body = document.getElementById('paneBody');
    body.innerHTML = '';
    body.appendChild(passField('Текущий пароль', 'mpCur'));
    body.appendChild(passField('Новый пароль', 'mpNew',
      'Не короче ' + ((state.me && state.me.min_password) || 5) + ' символов'));
    body.appendChild(passField('Новый пароль ещё раз', 'mpNew2'));
    body.appendChild(el('p', 'meta', 'Панель на других устройствах закроется, здесь вы останетесь.'));
    var foot = document.getElementById('paneFoot');
    foot.innerHTML = '';
    var save = el('button', 'act', 'Сменить пароль');
    save.onclick = function () {
      var cur = document.getElementById('mpCur').value;
      var a = document.getElementById('mpNew').value;
      if (a !== document.getElementById('mpNew2').value) { say('Новые пароли не совпадают'); return; }
      save.disabled = true;
      api('/me/password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current: cur, password: a })
      }).then(function () { closePane(); say('Пароль изменён', 'ok'); })
        .catch(function (e) { say(e.message); save.disabled = false; });
    };
    foot.appendChild(save);
    openPane();
  };

  // --------------------------------------------------- онлайн-платежи: отчёт

  function isoDay(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-'
      + String(d.getDate()).padStart(2, '0');
  }

  function quickPeriod(days) {
    var to = new Date();
    var from = new Date();
    if (days === 1) { to.setDate(to.getDate() - 1); from.setDate(from.getDate() - 1); }
    else if (days > 1) from.setDate(from.getDate() - (days - 1));
    document.getElementById('repFrom').value = isoDay(from);
    document.getElementById('repTo').value = isoDay(to);
  }

  function reportQuery() {
    return '?date_from=' + encodeURIComponent(document.getElementById('repFrom').value)
      + '&date_to=' + encodeURIComponent(document.getElementById('repTo').value);
  }

  function loadReport() {
    if (!document.getElementById('repFrom').value) quickPeriod(0);
    api('/online-payments' + reportQuery()).then(renderReport).catch(function (e) { say(e.message); });
  }

  function cell(tr, text, cls) {
    var td = el('td', cls, text);
    tr.appendChild(td);
    return td;
  }

  function headRow(table, titles) {
    var tr = document.createElement('tr');
    titles.forEach(function (t) {
      tr.appendChild(el('th', t[1] || null, t[0]));
    });
    table.appendChild(tr);
  }

  var FISCAL = { ok: 'есть', wait: 'ждём', missing: 'нет', error: 'ошибка', none: '—' };

  function renderReport(r) {
    document.getElementById('cntAlerts').textContent = r.alerts.length ? r.alerts.length : '';

    var box = document.getElementById('repAlerts');
    box.innerHTML = '';
    r.alerts.forEach(function (a) {
      var d = el('div', 'ralert');
      d.appendChild(el('b', null, a.number + ' · ' + a.provider + ': '));
      d.appendChild(document.createTextNode(a.text));
      box.appendChild(d);
    });

    var tot = document.getElementById('repTotals');
    tot.innerHTML = '';
    r.totals.providers.concat(r.totals.providers.length > 1 ? [r.totals.overall] : [])
      .forEach(function (t) {
        var c = el('div', 'rtot');
        c.appendChild(el('div', 't', t.title));
        c.appendChild(el('div', 'meta', 'Должно прийти на счёт'));
        c.appendChild(el('div', 'n', money(t.net)));
        c.appendChild(el('div', 'meta', t.count + ' плат. на ' + money(t.paid)
          + (t.refund_count ? ' · возвратов ' + t.refund_count + ' на ' + money(t.refunds) : '')
          + ' · комиссия ' + money(t.fee)));
        tot.appendChild(c);
      });
    if (!r.payments.length) tot.appendChild(el('div', 'meta', 'За этот период онлайн-платежей нет.'));

    var days = document.getElementById('repDays');
    days.innerHTML = '';
    headRow(days, [['Дата'], ['Способ'], ['Платежей', 'num'], ['Оплачено', 'num'],
                   ['Возвраты', 'num'], ['Комиссия', 'num'], ['На счёт', 'num']]);
    r.totals.days.forEach(function (d) {
      var tr = document.createElement('tr');
      cell(tr, d.day.split('-').reverse().join('.'));
      cell(tr, d.title);
      cell(tr, d.count, 'num');
      cell(tr, money(d.paid), 'num');
      cell(tr, d.refunds ? money(d.refunds) : '—', 'num');
      cell(tr, money(d.fee), 'num');
      cell(tr, money(d.net), 'num');
      days.appendChild(tr);
    });

    var rows = document.getElementById('repRows');
    rows.innerHTML = '';
    headRow(rows, [['Время'], ['Заказ'], ['Покупатель'], ['Способ'], ['Сумма', 'num'],
                   ['Комиссия', 'num'], ['На счёт', 'num'], ['Состояние'], ['Чек'],
                   ['Касса REGOS'], ['Заказ']]);
    r.payments.forEach(function (p) {
      var tr = document.createElement('tr');
      cell(tr, p.time);
      cell(tr, p.number);
      cell(tr, p.customer);
      cell(tr, p.provider);
      cell(tr, money(p.amount), 'num');
      cell(tr, money(p.fee) + ' (' + p.fee_pct + '%)', 'num');
      cell(tr, money(p.net), 'num');
      cell(tr, '').appendChild(el('span', 'pill ' + (p.state === 'paid' ? 'ok' : 'warn'),
                                  p.state_text));
      var fc = cell(tr, '');
      if (p.fiscal_url) {
        var a = el('a', null, 'открыть');
        a.href = p.fiscal_url;
        a.target = '_blank';
        a.rel = 'noopener';
        fc.appendChild(a);
      } else {
        fc.appendChild(el('span', 'pill ' + (p.fiscal === 'none' ? 'no' : 'warn'),
                          FISCAL[p.fiscal] || ''));
        if (p.fiscal_note) fc.title = p.fiscal_note;
      }
      cell(tr, p.regos_status + (p.regos_doc ? ' · №' + p.regos_doc : ''));
      cell(tr, p.order_status);
      rows.appendChild(tr);
    });

    document.getElementById('repNote').textContent = 'Комиссия: Payme ' + r.fees.payme
      + '%, Click ' + r.fees.click + '%, Paynet ' + r.fees.paynet + '% — меняется в разделе '
      + '«Онлайн оплата». «Не закрыт на кассе» — если заказ оплачен больше ' + r.open_hours
      + ' ч назад, а в REGOS не «Завершен». Время — ташкентское.';
  }

  document.querySelectorAll('#repQuick .tab').forEach(function (t) {
    t.onclick = function () {
      document.querySelectorAll('#repQuick .tab').forEach(function (x) {
        x.classList.toggle('on', x === t);
      });
      quickPeriod(Number(t.dataset.days));
      loadReport();
    };
  });
  document.getElementById('repShow').onclick = function () {
    document.querySelectorAll('#repQuick .tab').forEach(function (x) { x.classList.remove('on'); });
    loadReport();
  };
  document.getElementById('repXlsx').onclick = function (e) {
    var button = e.target;
    button.disabled = true;
    var headers = {};
    if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;
    fetch('/api/admin/online-payments.xlsx' + reportQuery(), { headers: headers })
      .then(function (r) {
        if (!r.ok) throw new Error('Файл не получен: ' + r.status);
        var found = (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/);
        return r.blob().then(function (b) { return [b, found ? found[1] : 'onlayn-platezhi.xlsx']; });
      })
      .then(function (res) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(res[0]);
        a.download = res[1];
        document.body.appendChild(a);
        a.click();
        setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
      })
      .catch(function (err) { say(err.message); })
      .then(function () { button.disabled = false; });
  };

  // --------------------------------------------------- стоимость доставки

  function showDelivery(r) {
    document.getElementById('deliveryPrice').value = r.price;
    document.getElementById('deliveryNote').textContent =
      'Сейчас: ' + (r.price ? money(r.price) : 'бесплатно') + '.';
  }

  function loadDelivery() {
    api('/delivery-price').then(showDelivery).catch(function (e) {
      document.getElementById('deliveryNote').textContent = 'Не загрузилось: ' + e.message;
    });
  }

  function saveDelivery() {
    var raw = document.getElementById('deliveryPrice').value.trim();
    var price = Number(raw);
    if (raw === '' || !Number.isInteger(price) || price < 0) {
      return say('Укажите стоимость доставки целым числом, 0 — бесплатно');
    }
    if (price > 1000000) return say('Слишком дорого для доставки — проверьте, нет ли лишнего нуля');
    var text = price ? money(price) : 'бесплатная доставка';
    if (!confirm('Новые заказы — ' + text + '. Сохранить?')) return;
    api('/delivery-price', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price: price })
    }).then(function (r) {
      showDelivery(r);
      say('Стоимость доставки сохранена', 'ok');
    }).catch(function (e) { say(e.message); });
  }

  function renderAuto(data) {
    var box = document.getElementById('autoPush');
    var note = document.getElementById('autoNote');
    box.checked = data.enabled;
    // Включать нечего, пока выгрузка не настроена: молча включённая
    // автоотправка, которая ничего не делает, уже один раз стоила заказов
    box.disabled = !data.ready && !data.enabled;
    note.textContent = data.ready
      ? (data.enabled
          ? 'Заказ уходит в REGOS сам и сразу встаёт на кассу.'
          : 'Сейчас заказы передаёт оператор кнопкой в карточке заказа.')
      : 'Включить нельзя: ' + data.reason + '.';
  }

  document.getElementById('autoPush').onchange = function (e) {
    var on = e.target.checked;
    api('/regos/auto', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: on })
    }).then(function () {
      say(on ? 'Автоотправка включена' : 'Автоотправка выключена', 'ok');
      loadSettings();
    }).catch(function (err) {
      e.target.checked = !on;        // возвращаем флажок: сервер отказал
      say(err.message);
      loadSettings();
    });
  };

  function when(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit',
                                       minute: '2-digit' });
  }

  // --------------------------------------------------- кому приходят заказы

  // Сюда уходит каждый заказ целиком — с телефоном и адресом покупателя.
  // Поэтому доступ включается только здесь, руками: сам себе его в боте
  // никто выдать не может.
  function renderChats(data) {
    var box = document.getElementById('chats');
    var name = document.getElementById('botName');
    if (name && data.bot) name.textContent = '@' + data.bot;
    box.innerHTML = '';

    if (!data.enabled) {
      box.appendChild(el('div', 'meta', 'Бот не настроен: не задан токен.'));
      return;
    }
    if (!data.chats.length) {
      box.appendChild(el('div', 'meta',
        'Пока боту никто не писал. Напишите ему «Старт» — и чат появится здесь.'));
      return;
    }

    data.chats.forEach(function (c) {
      var row = el('div');
      row.style.cssText = 'display:flex;gap:10px;align-items:center;margin-bottom:8px;'
        + 'max-width:640px;padding:9px 11px;border:1px solid #ECECEC;border-radius:10px';

      var who = el('div');
      who.style.cssText = 'flex:1;min-width:0';
      var title = el('div', null, (c.kind === 'group' ? '👥 ' : '') + c.title);
      title.style.fontWeight = '500';
      who.appendChild(title);
      var sub = [];
      if (c.username) sub.push('@' + c.username);
      sub.push(c.kind === 'group' ? 'группа' : 'личный чат');
      if (!c.reachable) sub.push('бот заблокирован');
      who.appendChild(el('div', 'meta', sub.join(' · ')));
      row.appendChild(who);

      var mark = el('span', 'pill ' + (c.access ? 'ok' : 'no'),
                    c.access ? 'получает заказы' : 'нет доступа');
      row.appendChild(mark);

      // язык сообщений в этом чате: RU / UZ
      var lang = el('button', 'act ghost', c.language === 'uz' ? 'UZ' : 'RU');
      lang.title = 'Язык сообщений в этом чате — нажмите, чтобы сменить';
      lang.style.minWidth = '46px';
      lang.onclick = function () {
        var next = c.language === 'uz' ? 'ru' : 'uz';
        lang.disabled = true;
        api('/notify/chats/' + c.chat_id + '/language', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ language: next })
        }).then(function () {
          say(next === 'uz' ? 'Сообщения — на узбекском' : 'Сообщения — на русском', 'ok');
          loadChats();
        }).catch(function (e) { say(e.message); lang.disabled = false; });
      };
      row.appendChild(lang);

      var btn = el('button', 'act' + (c.access ? ' ghost' : ''),
                   c.access ? 'Отключить' : 'Дать доступ');
      btn.onclick = function () {
        btn.disabled = true;
        api('/notify/chats/' + c.chat_id, { method: c.access ? 'DELETE' : 'POST' })
          .then(function () {
            say(c.access ? 'Отключён' : 'Доступ выдан', 'ok');
            loadChats();
          })
          .catch(function (e) { say(e.message); btn.disabled = false; });
      };
      row.appendChild(btn);
      box.appendChild(row);
    });
  }

  // --------------------------------------------- фотографии из REGOS

  function pullPhotos(apply) {
    var note = document.getElementById('photoNote');
    var buttons = [document.getElementById('photoCheck'), document.getElementById('photoPull')];
    buttons.forEach(function (b) { b.disabled = true; });
    note.textContent = apply ? 'Загружаем… это может занять минуту' : 'Считаем…';

    api('/regos/photos?apply=' + (apply ? 'true' : 'false'), { method: 'POST' })
      .then(function (r) {
        if (!r.applied) {
          note.textContent = r.candidates
            ? 'Карточек без фото, для которых есть картинка: ' + r.candidates
              + ' (всего картинок в REGOS ' + r.in_regos + ')'
            : 'Все карточки уже с фотографиями';
          return;
        }
        note.textContent = 'Загружено: ' + r.loaded
          + (r.failed ? ', не получилось: ' + r.failed : '');
        if (r.loaded) { say('Фотографии загружены', 'ok'); loadProducts(); }
      })
      .catch(function (e) { note.textContent = ''; say(e.message); })
      .finally(function () { buttons.forEach(function (b) { b.disabled = false; }); });
  }

  // --------------------------------------------------- вид цены REGOS

  var priceState = { current: null, checked: null };

  function loadPriceTypes() {
    api('/regos/price-types').then(function (r) {
      priceState.current = r.current;
      var sel = document.getElementById('priceType');
      sel.innerHTML = '';
      r.types.forEach(function (t) {
        var o = document.createElement('option');
        o.value = t.id;
        o.textContent = t.name + (t.id === r.current ? ' — сейчас на витрине' : '');
        if (t.id === r.current) o.selected = true;
        sel.appendChild(o);
      });
      document.getElementById('priceNote').textContent = '';
      document.getElementById('priceApply').disabled = true;
    }).catch(function (e) {
      document.getElementById('priceNote').textContent = 'Виды цен не загрузились: ' + e.message;
    });
  }

  // Применять можно только то, что сначала посмотрели: в другом виде цены
  // части товаров цены может не быть, и они молча пропали бы с витрины
  function priceChange(apply) {
    var id = Number(document.getElementById('priceType').value);
    var note = document.getElementById('priceNote');
    var applyBtn = document.getElementById('priceApply');
    if (apply && priceState.checked !== id) return say('Сначала посмотрите, что изменится');
    note.textContent = apply ? 'Применяем…' : 'Считаем…';
    applyBtn.disabled = true;
    api('/regos/price-type?apply=' + (apply ? 'true' : 'false'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ price_type_id: id })
    }).then(function (r) {
      var st = r.stats;
      var text = 'Товаров на витрине: ' + st.total + '. Дороже: ' + st.up +
        ', дешевле: ' + st.down + ', без изменений: ' + st.same +
        (st.manual ? ', со своей ценой (не меняются): ' + st.manual : '') +
        (st.lost ? '. ПРОПАДУТ с витрины — нет цены в этом виде: ' + st.lost : '') + '.';
      var lost = r.rows.filter(function (x) { return x.change === 'пропадёт с витрины'; })
        .slice(0, 5).map(function (x) { return x.product + ' ' + x.weight; });
      if (lost.length) text += ' Например: ' + lost.join(', ') + '.';
      if (r.applied) {
        note.textContent = 'Применено. ' + text;
        say('Вид цены переключён', 'ok');
        priceState.checked = null;
        loadPriceTypes(); loadProducts();
      } else {
        note.textContent = text;
        priceState.checked = id;
        applyBtn.disabled = id === priceState.current;
      }
    }).catch(function (e) { note.textContent = ''; say(e.message); });
  }

  function loadChats() {
    api('/notify/chats').then(renderChats).catch(function (e) { say(e.message); });
    loadCouriers();
  }

  // --------------------------------------------------- доставщики

  function loadCouriers() {
    api('/couriers').then(renderCouriers).catch(function (e) { say(e.message); });
  }

  function renderCouriers(data) {
    var box = document.getElementById('couriers');
    var name = document.getElementById('courierBot');
    if (name && data.bot) name.textContent = '@' + data.bot;
    box.innerHTML = '';
    if (!data.enabled) {
      box.appendChild(el('div', 'meta', 'Бот доставщиков не настроен: не задан токен.'));
      return;
    }
    if (!data.couriers.length) {
      box.appendChild(el('div', 'meta',
        'Пока боту никто не писал. Попросите доставщика нажать в нём «Старт».'));
      return;
    }
    data.couriers.forEach(function (c) {
      var row = el('div');
      row.style.cssText = 'display:flex;gap:10px;align-items:center;margin-bottom:8px;'
        + 'max-width:640px;padding:9px 11px;border:1px solid #ECECEC;border-radius:10px';
      var who = el('div');
      who.style.cssText = 'flex:1;min-width:0';
      var title = el('div', null, '🚚 ' + c.name);
      title.style.fontWeight = '500';
      who.appendChild(title);
      var sub = [];
      if (c.username) sub.push('@' + c.username);
      if (c.in_delivery) sub.push('в доставке: ' + c.in_delivery);
      if (sub.length) who.appendChild(el('div', 'meta', sub.join(' · ')));
      row.appendChild(who);
      row.appendChild(el('span', 'pill ' + (c.access ? 'ok' : 'no'),
                         c.access ? 'видит заказы' : 'нет доступа'));
      var btn = el('button', 'act' + (c.access ? ' ghost' : ''),
                   c.access ? 'Отключить' : 'Дать доступ');
      btn.onclick = function () {
        if (c.access && c.in_delivery && !confirm('У доставщика ' + c.in_delivery
            + ' заказ(ов) в пути. Они станут свободными. Отключить?')) return;
        btn.disabled = true;
        api('/couriers/' + c.telegram_id, { method: c.access ? 'DELETE' : 'POST' })
          .then(function (r) {
            say(c.access ? 'Отключён' + (r.released && r.released.length
              ? ', освобождены: ' + r.released.join(', ') : '') : 'Доступ выдан', 'ok');
            loadCouriers();
          })
          .catch(function (e) { say(e.message); btn.disabled = false; });
      };
      row.appendChild(btn);
      box.appendChild(row);
    });
  }

  function renderCats(cats) {
    var box = document.getElementById('cats');
    box.innerHTML = '';
    cats.forEach(function (c) {
      var row = el('div');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;margin-bottom:8px;max-width:640px';

      var name = document.createElement('input');
      name.value = c.name;
      var ord = document.createElement('input');
      ord.type = 'number'; ord.value = c.sort_order; ord.style.maxWidth = '90px';

      var save = el('button', 'act ghost', 'Сохранить');
      save.onclick = function () {
        api('/categories/' + c.id, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name.value.trim(), sort_order: Number(ord.value) })
        }).then(function () { say('Сохранено', 'ok'); loadSettings(); })
          .catch(function (e) { say(e.message); });
      };

      var del = el('button', 'act ghost', 'Удалить');
      del.disabled = c.product_count > 0;
      del.title = c.product_count ? 'В категории есть товары' : '';
      del.onclick = function () {
        api('/categories/' + c.id, { method: 'DELETE' })
          .then(function () { say('Удалена', 'ok'); loadSettings(); })
          .catch(function (e) { say(e.message); });
      };

      row.appendChild(name);
      row.appendChild(ord);
      row.appendChild(el('span', 'meta', c.product_count + ' тов.'));
      row.appendChild(save);
      row.appendChild(del);
      box.appendChild(row);
    });
  }

  function renderGroups(data) {
    var box = document.getElementById('groups');
    box.innerHTML = '';
    if (data.error) {
      box.appendChild(el('div', 'meta', 'REGOS недоступен: ' + data.error));
      return;
    }
    if (!data.groups.length) {
      box.appendChild(el('div', 'meta', 'Групп нет'));
      return;
    }
    data.groups.forEach(function (g) {
      var row = el('label');
      row.style.cssText = 'display:flex;gap:8px;align-items:center;padding:5px 2px;cursor:pointer';
      var cb = document.createElement('input');
      cb.type = 'checkbox'; cb.value = g.id; cb.checked = g.selected;
      cb.style.width = 'auto';
      cb.className = 'grp';
      row.appendChild(cb);
      row.appendChild(el('span', null, g.name));
      var cnt = el('span', 'meta', g.in_catalog ? g.in_catalog + ' в витрине' : '');
      cnt.style.marginLeft = 'auto';
      row.appendChild(cnt);
      box.appendChild(row);
    });
  }

  function chosenGroups() {
    return [].slice.call(document.querySelectorAll('.grp'))
      .filter(function (c) { return c.checked; })
      .map(function (c) { return Number(c.value); });
  }

  document.getElementById('addCat').onclick = function () {
    var input = document.getElementById('newCat');
    if (!input.value.trim()) return say('Введите название');
    api('/categories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: input.value.trim(), sort_order: 100 })
    }).then(function () { input.value = ''; say('Добавлена', 'ok'); loadSettings(); })
      .catch(function (e) { say(e.message); });
  };

  document.getElementById('saveGroups').onclick = function () {
    api('/regos/groups', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_ids: chosenGroups() })
    }).then(function (r) {
      say('Состав сохранён: групп ' + r.selected.length +
          '. Применится при следующей синхронизации.', 'ok');
    }).catch(function (e) { say(e.message); });
  };

  // Удаление необратимо, поэтому сначала показываем, сколько именно уедет,
  // и только вторым нажатием выполняем.
  document.getElementById('deliverySave').onclick = saveDelivery;
  document.getElementById('priceCheck').onclick = function () { priceChange(false); };
  document.getElementById('priceApply').onclick = function () { priceChange(true); };
  document.getElementById('priceType').onchange = function () {
    // выбрали другой вид — прежний предпросмотр к нему не относится
    priceState.checked = null;
    document.getElementById('priceApply').disabled = true;
    document.getElementById('priceNote').textContent = '';
  };
  document.getElementById('photoCheck').onclick = function () { pullPhotos(false); };
  document.getElementById('photoPull').onclick = function () { pullPhotos(true); };

  document.getElementById('cleanup').onclick = function () {
    api('/cleanup', { method: 'POST' }).then(function (r) {
      if (!r.count) return say('Лишнего нет', 'ok');
      var text = 'Удалить ' + r.count + ' неопубликованных карточек вне выбранных групп?'
        + '\n\nНапример: ' + r.sample.slice(0, 3).join(', ')
        + '\n\nОтменить это будет нельзя.';
      if (!window.confirm(text)) return;
      api('/cleanup?apply=true', { method: 'POST' }).then(function (d) {
        say('Удалено ' + d.removed + (d.blocked ? ', пропущено (есть заказы) ' + d.blocked : ''), 'ok');
        loadSettings();
      }).catch(function (e) { say(e.message); });
    }).catch(function (e) { say(e.message); });
  };

  // ------------------------------------------------------------------ каркас

  function openPane() {
    document.getElementById('pane').classList.add('on');
    document.getElementById('ovl').classList.add('on');
  }
  function closePane() {
    document.getElementById('pane').classList.remove('on');
    document.getElementById('ovl').classList.remove('on');
  }

  // разделы, доступные роли вошедшего (из /whoami); порядок — как в меню
  var ORDER_OF_VIEWS = ['products', 'orders', 'report', 'settings', 'payments', 'users'];
  var allowedViews = ORDER_OF_VIEWS.slice();

  function show(view) {
    if (allowedViews.indexOf(view) < 0) view = allowedViews[0];
    if (!view) return;
    state.view = view;
    document.getElementById('viewProducts').hidden = view !== 'products';
    document.getElementById('viewOrders').hidden = view !== 'orders';
    document.getElementById('viewSettings').hidden = view !== 'settings';
    document.getElementById('viewPayments').hidden = view !== 'payments';
    document.getElementById('viewReport').hidden = view !== 'report';
    document.getElementById('viewUsers').hidden = view !== 'users';
    document.getElementById('q').hidden = view === 'settings' || view === 'payments'
      || view === 'report' || view === 'users';
    document.getElementById('ttl').textContent =
      { products: 'Категории и товары', orders: 'Заказы',
        settings: 'Состав каталога', payments: 'Онлайн оплата',
        report: 'Онлайн-платежи', users: 'Пользователи' }[view];
    document.querySelectorAll('.nav').forEach(function (n) {
      n.classList.toggle('on', n.dataset.go === view);
    });
    say('');
    if (view === 'products') loadProducts();
    else if (view === 'orders') loadOrders();
    else if (view === 'payments') loadPaySwitches();
    else if (view === 'report') loadReport();
    else if (view === 'users') loadUsers();
    else loadSettings();
  }

  document.querySelectorAll('.nav').forEach(function (n) {
    n.onclick = function () { show(n.dataset.go); };
  });
  document.querySelectorAll('.tab[data-status]').forEach(function (t) {
    t.onclick = function () {
      state.status = t.dataset.status;
      document.querySelectorAll('.tab[data-status]').forEach(function (x) {
        x.classList.toggle('on', x === t);
      });
      loadProducts();
    };
  });
  document.getElementById('catFilter').onchange = function (e) {
    state.category = e.target.value;
    loadProducts();
  };

  function refreshButton(button, action) {
    button.disabled = true;
    var was = button.textContent;
    button.textContent = 'Обновляем…';
    action();
    // кнопку возвращаем сразу: список отрисуется сам, а держать её
    // заблокированной до ответа незачем — запрос короткий
    setTimeout(function () {
      button.disabled = false;
      button.textContent = was;
    }, 400);
  }

  document.getElementById('reloadProducts').onclick = function (e) {
    refreshButton(e.target, function () {
      loadCategoryFilter();       // счётчики в категориях тоже освежаем
      loadProducts();
    });
  };

  document.getElementById('reloadOrders').onclick = function (e) {
    refreshButton(e.target, loadOrders);
  };

  document.getElementById('close').onclick = closePane;
  document.getElementById('ovl').onclick = closePane;
  document.getElementById('q').oninput = function (e) {
    state.q = e.target.value.trim();
    clearTimeout(timer);            // не дёргаем сервер на каждой букве
    timer = setTimeout(function () {
      if (state.view === 'products') loadProducts();
      else if (state.view === 'orders') loadOrders();
    }, 350);
  };

  // Если не пустили — показываем собственный id, иначе человеку неоткуда его
  // взять, а без id его не добавить в список администраторов.
  function denied(message) {
    document.querySelector('.body').innerHTML = '';
    fetch('/api/admin/whoami', {
      headers: tg && tg.initData ? { 'X-Telegram-Init-Data': tg.initData } : {}
    }).then(function (r) { return r.json(); }).then(function (me) {
      say(message + (me.id ? ' Ваш Telegram id: ' + me.id : ' ' + (me.hint || '')));
    }).catch(function () { say(message); });
  }

  // Кто вошёл и чем выйти. В Telegram выхода нет: там сессии не заводится,
  // вход подтверждается подписью при каждом запросе.
  api('/whoami', { quiet: true }).then(function (me) {
    state.me = me;
    document.getElementById('who').textContent = (me.name || '')
      + (me.role_title ? ' · ' + me.role_title : '');
    if (me.sections) {
      allowedViews = ORDER_OF_VIEWS.filter(function (v) { return me.sections.indexOf(v) >= 0; });
    }
    document.querySelectorAll('.nav').forEach(function (n) {
      n.hidden = allowedViews.indexOf(n.dataset.go) < 0;
    });
    var out = document.getElementById('logout');
    if (me.via !== 'password') {
      out.hidden = true;
    } else {
      document.getElementById('myPass').hidden = false;
      out.onclick = function () {
        api('/logout', { method: 'POST' })
          .then(function () { location.href = '/admin/login'; })
          .catch(function () { location.href = '/admin/login'; });
      };
    }
    return api('/statuses');
  }).then(function (s) {
    state.statuses = s.statuses;
    state.transitions = s.transitions;
    renderOrderTabs();
    if (allowedViews.indexOf('products') >= 0) loadCategoryFilter();
    if (!allowedViews.length) { denied('Вашей роли не доступен ни один раздел.'); return; }
    show(allowedViews[0]);
  }).catch(function (e) { denied(e.message); });
})();
