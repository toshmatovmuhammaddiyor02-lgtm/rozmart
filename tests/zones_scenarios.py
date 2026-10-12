"""Районы доставки: заказ — только из отмеченных районов Ташкента.

    .venv\\Scripts\\python.exe tests/zones_scenarios.py

Район — по точке на карте (границы OpenStreetMap). База — временный SQLite.
Выход с кодом 1, если хоть одна проверка не прошла.
"""
import hashlib
import hmac
import json
import os
import pathlib
import sys
import tempfile
import time
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

tmp = pathlib.Path(tempfile.mkdtemp()) / "t.db"
os.environ.update({
    "DATABASE_URL": f"sqlite:///{tmp.as_posix()}",
    "BOT_TOKEN": "111:shop-token",
    "ADMIN_TELEGRAM_IDS": "1",
    "REGOS_KEY": "",
})

from fastapi.testclient import TestClient  # noqa: E402

from app import admin_auth, zones  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AdminUser, Category, Product, Variant  # noqa: E402

init_db()
db = SessionLocal()
cat = Category(name="Колбасы"); db.add(cat); db.flush()
prod = Product(category_id=cat.id, name="Докторская", is_active=True); db.add(prod); db.flush()
variant = Variant(product_id=prod.id, external_code="003100", weight="400 г", price=39000,
                  regos_price=39000, is_active=True)
db.add(variant)
db.add(AdminUser(username="buh", role="accountant", password_hash=admin_auth.hash_password("pass-12345")))
db.commit()
db.close()

client = TestClient(app)
results = []

# контрольные точки: район известен заранее
CHILONZOR = (41.2735, 69.2040)       # метро Чиланзар
YUNUSOBOD = (41.3650, 69.2870)
SERGELI = (41.2280, 69.2200)
CHORSU = (41.3265, 69.2350)          # Шайхантахурский
CHIRCHIQ = (41.4700, 69.5800)        # не Ташкент


def check(n, name, ok, detail=""):
    results.append((n, name, ok))
    print(f"{'ПРОШЛО ' if ok else 'НЕ ПРОШЛО'} {n:>2}. {name}{(' — ' + str(detail)) if detail else ''}")


def init_data(user_id):
    user = json.dumps({"id": user_id, "first_name": "Test"})
    fields = {"auth_date": str(int(time.time())), "user": user}
    c = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", b"111:shop-token", hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, c.encode(), hashlib.sha256).hexdigest()
    return {"X-Telegram-Init-Data": urlencode(fields)}


ADMIN, BUYER = init_data(1), init_data(900)
n_key = [0]


def order(point=None, lang="ru"):
    n_key[0] += 1
    body = {"customer_name": "Javohir Rahmonov", "phone": "+998901234567", "address": "Toshkent",
            "delivery_slot": "Как можно скорее", "payment_method": "cash", "consent": True,
            "client_key": f"zone-key-{n_key[0]:04d}", "items": [{"variant_id": variant.id, "quantity": 1}]}
    if point:
        body["lat"], body["lon"] = point
    return client.post("/api/orders", json=body, headers={**BUYER, "X-Lang": lang})


# ------------------------------------------------------------ точки -> районы
got = {name: (zones.district_at(*p) or {}).get("code") for name, p in
       {"chilonzor": CHILONZOR, "yunusobod": YUNUSOBOD, "sergeli": SERGELI,
        "shayxontohur": CHORSU, None: CHIRCHIQ}.items()}
check(1, "Контрольные точки — в своих районах, Чирчик — ни в одном",
      all(k == v for k, v in got.items()), got)
check(1, "В справочнике 12 районов Ташкента", len(zones.districts()) == 12)
check(1, "Анклав Мирзо-Улугбекского у посёлка Улугбек (за городом) — не зона",
      zones.district_at(41.4115, 69.4544) is None and zones.district_at(41.3281, 69.3194)["code"] == "mirzo_ulugbek")

# ------------------------------------------------------------ пока не выбрано — без ограничений
check(2, "Районы не выбраны — заказ без точки принимается", order().status_code == 201)
r = client.get("/api/delivery-zone", params={"lat": SERGELI[0], "lon": SERGELI[1]}).json()
check(2, "…и проверка точки отвечает «можно»", r["enabled"] is False and r["ok"] is True, r)

# ------------------------------------------------------------ панель
r = client.put("/api/admin/delivery-zones", json={"codes": zones.STARTED_WITH}, headers=ADMIN)
on = [d["code"] for d in r.json()["districts"] if d["on"]] if r.status_code == 200 else []
check(3, "Администратор отметил 7 районов", r.status_code == 200 and sorted(on) == sorted(zones.STARTED_WITH), on)
check(3, "Неизвестный район в списке молча отброшен",
      "atlantis" not in [d["code"] for d in client.put("/api/admin/delivery-zones", json={
          "codes": zones.STARTED_WITH + ["atlantis"]}, headers=ADMIN).json()["districts"] if d["on"]])
buh = TestClient(app, base_url="https://testserver")
buh.post("/api/admin/login", json={"username": "buh", "password": "pass-12345"})
check(3, "Бухгалтеру районы менять нельзя",
      buh.put("/api/admin/delivery-zones", json={"codes": []}).status_code == 403)

# ------------------------------------------------------------ заказ
r = order()
check(4, "Без точки на карте — просим поставить", r.status_code == 400
      and "Укажите дом на карте" in r.json()["detail"], r.text)
r = order(CHILONZOR)
check(5, "Чиланзар — заказ принят", r.status_code == 201, r.text[:150])
check(5, "Юнусабад — заказ принят", order(YUNUSOBOD).status_code == 201)
r = order(SERGELI)
detail = r.json().get("detail", "")
check(6, "Сергели — отказ с названием района", r.status_code == 400
      and "нет возможности заказать из этого района (Сергелийский район)" in detail, detail)
check(6, "…и списком районов, куда возим", "Чиланзарский район" in detail and "Алмазарский район" in detail)
r = order(SERGELI, lang="uz")
check(7, "По-узбекски — свой текст", "Hozircha bu tumandan (Sergeli tumani)" in r.json().get("detail", "")
      and "Chilonzor tumani" in r.json()["detail"], r.json().get("detail"))
check(7, "Без точки по-узбекски", "xaritada belgilang" in order(lang="uz").json().get("detail", ""))
r = order(CHIRCHIQ)
check(8, "Вне Ташкента — «по этому адресу»", r.status_code == 400
      and "по этому адресу" in r.json().get("detail", ""), r.text[:200])

# ------------------------------------------------------------ проверка точки из приложения
r = client.get("/api/delivery-zone", params={"lat": SERGELI[0], "lon": SERGELI[1]},
               headers={"X-Lang": "ru"}).json()
check(9, "Приложение спрашивает про точку — «нельзя» и текст",
      r["enabled"] and r["ok"] is False and r["district"] == "Сергелийский район" and "Доставляем" in r["message"], r)
r = client.get("/api/delivery-zone", params={"lat": CHILONZOR[0], "lon": CHILONZOR[1]},
               headers={"X-Lang": "uz"}).json()
check(9, "…для Чиланзара — «можно», район по-узбекски", r["ok"] and r["district"] == "Chilonzor tumani", r)

# ------------------------------------------------------------ карта районов в приложении
m = client.get("/api/delivery-zone/map").json()
codes = sorted(d["code"] for d in m["districts"])
points = sum(len(r) for d in m["districts"] for r in d["rings"])
check(11, "Карта: 7 районов доставки, упрощённые границы", m["enabled"] and codes == sorted(zones.STARTED_WITH)
      and 200 < points < 1500, (codes, points))
first = m["districts"][0]["rings"][0][0]
check(11, "Точки — [широта, долгота] (как ждёт Leaflet)", 41 < first[0] < 42 and 69 < first[1] < 70, first)

# ------------------------------------------------------------ снять ограничение
client.put("/api/admin/delivery-zones", json={"codes": []}, headers=ADMIN)
check(10, "Ничего не отмечено — снова без ограничений", order(SERGELI).status_code == 201)
check(11, "…и на карте зон нет", client.get("/api/delivery-zone/map").json() == {"enabled": False, "districts": []})

failed = [r for r in results if not r[2]]
print(f"\nИтого проверок: {len(results)}, не прошло: {len(failed)}")
sys.exit(1 if failed else 0)
