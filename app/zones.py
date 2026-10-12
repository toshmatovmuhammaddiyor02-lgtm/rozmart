"""Зона доставки: районы Ташкента, куда магазин возит заказы.

Район определяем по точке на карте, которую покупатель ставит на свой дом:
адрес словами пишут как угодно («Чиланзар 9», «Chilonzor-9», «рядом с Чорсу»),
а точка однозначна. Поэтому, пока зона ограничена, без точки заказ не принять.

Границы районов — из OpenStreetMap (app/data/tashkent_districts.json, собраны
скриптом из отношений boundary=administrative, admin_level=6). Проверка —
обычная «точка в многоугольнике»: у района бывает несколько колец (у Мирзо-
Улугбекского есть анклав), дыр в ташкентских районах нет, но учитываем и их.

Какие районы обслуживаются, выбирает администратор в панели. Пустой выбор —
ограничения нет, заказ принимается из любой точки.
"""
import json
import logging
from functools import lru_cache
from pathlib import Path

from app.models import Setting

log = logging.getLogger(__name__)

DATA = Path(__file__).resolve().parent / "data" / "tashkent_districts.json"
SETTING = "delivery_districts"
# районы, с которых магазин начал 12 октября 2026 (в панели их и отметили)
STARTED_WITH = ["chilonzor", "yakkasaroy", "mirobod", "yashnobod", "mirzo_ulugbek",
           "yunusobod", "olmazor"]


@lru_cache(maxsize=1)
def districts() -> list[dict]:
    raw = json.loads(DATA.read_text(encoding="utf-8"))["districts"]
    for d in raw:
        points = [p for ring in d["outer"] for p in ring]
        d["bbox"] = (min(p[0] for p in points), min(p[1] for p in points),
                     max(p[0] for p in points), max(p[1] for p in points))
    return raw


def _inside(lon: float, lat: float, ring: list) -> bool:
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def district_at(lat: float, lon: float) -> dict | None:
    for d in districts():
        x1, y1, x2, y2 = d["bbox"]
        if not (x1 <= lon <= x2 and y1 <= lat <= y2):
            continue
        if any(_inside(lon, lat, r) for r in d["outer"]) \
                and not any(_inside(lon, lat, r) for r in d["inner"]):
            return d
    return None


def allowed(db) -> list[str]:
    """Коды обслуживаемых районов. Пусто — ограничения нет."""
    row = db.get(Setting, SETTING)
    if row is None:
        return []                   # пока в панели не выбрали — без ограничений
    try:
        codes = json.loads(row.value)
    except (TypeError, ValueError):
        # испорченная настройка не должна молча закрыть магазин для всех
        log.warning("Настройка %s испорчена, ограничение районов не действует", SETTING)
        return []
    known = {d["code"] for d in districts()}
    return [c for c in codes if c in known]


def save(db, codes: list[str]) -> list[str]:
    known = [d["code"] for d in districts()]
    clean = [c for c in known if c in set(codes)]     # в порядке справочника
    row = db.get(Setting, SETTING)
    value = json.dumps(clean)
    if row is None:
        db.add(Setting(name=SETTING, value=value))
    else:
        row.value = value
    db.commit()
    return clean


def name(d: dict, lang: str) -> str:
    return d["uz"] if lang == "uz" else d["ru"]


def served_names(db, lang: str) -> str:
    codes = set(allowed(db))
    return ", ".join(name(d, lang) for d in districts() if d["code"] in codes)


# Для карты в приложении границы упрощаем: 600 точек на район телефону
# рисовать ни к чему, а 30 метров на глаз не видны. Заказ проверяется
# по точным границам (district_at), так что упрощение на него не влияет
MAP_TOLERANCE = 0.0003       # градуса, около 30 м


def _simplify(ring: list, tol: float = MAP_TOLERANCE) -> list:
    """Рамер — Дуглас — Пейкер, без рекурсии: кольцо бывает в сотни точек."""
    if len(ring) < 4:
        return ring
    keep = [False] * len(ring)
    keep[0] = keep[-1] = True
    stack = [(0, len(ring) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = ring[a], ring[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        far, far_i = 0.0, None
        for i in range(a + 1, b):
            x, y = ring[i]
            # у замкнутого кольца концы совпадают — тогда расстояние до точки
            d = (abs(dy * (x - x1) - dx * (y - y1)) / norm if norm
                 else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5)
            if d > far:
                far, far_i = d, i
        if far_i is not None and far > tol:
            keep[far_i] = True
            stack += [(a, far_i), (far_i, b)]
    return [p for p, k in zip(ring, keep) if k]


@lru_cache(maxsize=8)
def map_shapes(codes: tuple[str, ...]) -> list[dict]:
    """Обслуживаемые районы для карты: кольца точками [широта, долгота]."""
    wanted = set(codes)
    return [{"code": d["code"], "ru": d["ru"], "uz": d["uz"],
             "rings": [[[y, x] for x, y in _simplify(r)] for r in d["outer"]]}
            for d in districts() if d["code"] in wanted]


def check(db, lat: float | None, lon: float | None, lang: str = "ru") -> tuple[bool, str, dict | None]:
    """Можно ли везти в эту точку. (да/нет, текст для покупателя, район)."""
    codes = allowed(db)
    if not codes:
        return True, "", None
    if lat is None or lon is None:
        return False, ("Uyingizni xaritada belgilang — shu nuqta bo'yicha tumaningizga "
                       "yetkazib berishimizni tekshiramiz" if lang == "uz" else
                       "Укажите дом на карте — по этой точке мы проверяем, "
                       "доставляем ли в ваш район"), None
    d = district_at(lat, lon)
    if d is not None and d["code"] in codes:
        return True, "", d
    served = served_names(db, lang)
    if lang == "uz":
        where = f"bu tumandan ({d['uz']})" if d else "bu manzildan"
        text = (f"Hozircha {where} buyurtma berish imkoni yo'q. "
                f"Yetkazib beradigan tumanlarimiz: {served}.")
    else:
        where = f"из этого района ({d['ru']})" if d else "по этому адресу"
        text = (f"На данный момент нет возможности заказать {where}. "
                f"Доставляем в районы: {served}.")
    return False, text, d
