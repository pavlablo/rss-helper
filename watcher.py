#!/usr/bin/env python3
"""
Flat watcher: следит за новыми объявлениями об аренде в Братиславе
на topreality.sk, reality.bazos.sk и nehnutelnosti.sk и шлёт новые в Telegram.

Запуск:
    python watcher.py              # обычный прогон (для GitHub Actions)
    python watcher.py --print      # просто распарсить и показать, что сейчас на страницах
    python watcher.py --dry-run    # показать, что было бы отправлено, ничего не сохраняя
    python watcher.py --chat-id    # подсказать chat_id (нужен TELEGRAM_BOT_TOKEN)
    python watcher.py --test       # отправить тестовое сообщение в Telegram
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Страницы поиска с фильтрами. Везде включена сортировка "сначала новые".
# topreality: стр. 1 = vyhladavanie-nehnutelnosti.html, стр. 2 = ...-2.html.
#   На 1-й странице сверху 12 платных "TOP" объявлений, свежих там всего ~4,
#   поэтому смотрим две страницы.
# bazos: order=4 = сначала новые, без TOP-объявлений; cenado=901 добавлен,
#   чтобы фильтр цены совпадал с остальными сайтами.
SEARCH_URLS: dict[str, list[str]] = {
    "topreality": [
        "https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?type%5B0%5D=101&type%5B1%5D=108&type%5B2%5D=102&type%5B3%5D=103&type%5B4%5D=104&form=3&obec=2%2C5%2C8%2C21&cena_do=901&n_search=search&gpsPolygon=&searchType=string&sort=id_desc",
        "https://www.topreality.sk/vyhladavanie-nehnutelnosti-2.html?type%5B0%5D=101&type%5B1%5D=108&type%5B2%5D=102&type%5B3%5D=103&type%5B4%5D=104&form=3&obec=2%2C5%2C8%2C21&cena_do=901&n_search=search&gpsPolygon=&searchType=string&sort=id_desc",
    ],
    "bazos": [
        "https://reality.bazos.sk/prenajmu/byt/?hledat=&rubriky=reality&hlokalita=81107&humkreis=6&cenaod=&cenado=901&order=4&crp=&kitx=ano",
    ],
    "nehnutelnosti": [
        "https://www.nehnutelnosti.sk/vysledky/prenajom?locations=100012439&locations=100012513&locations=100012511&locations=100012524&categories=11&categories=300000&categories=12&categories=18&categories=17&priceTo=901&order=NEWEST",
    ],
}

SOURCE_LABELS = {
    "topreality": "TopReality",
    "bazos": "Bazoš",
    "nehnutelnosti": "Nehnuteľnosti",
}

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("seen.json")))
MAX_PRICE = float(os.environ["MAX_PRICE"]) if os.environ.get("MAX_PRICE") else None  # доп. фильтр, € в месяц
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "25"))
FORGET_AFTER_DAYS = 90       # ID старше этого удаляются из seen.json
ALERT_AFTER_FAILURES = 6     # столько неудачных прогонов подряд -> сообщение "источник сломался"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8",
}


@dataclass
class Listing:
    source: str
    id: str
    title: str
    price: str
    location: str
    url: str
    details: str = ""

    @property
    def price_value(self) -> float | None:
        m = re.search(r"\d[\d\s ]*(?:[.,]\d+)?", self.price)
        if not m:
            return None
        num = re.sub(r"[\s ]", "", m.group(0)).replace(",", ".")
        try:
            return float(num)
        except ValueError:
            return None


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Парсеры
# ---------------------------------------------------------------------------

def parse_topreality(page_html: str, base_url: str = "https://www.topreality.sk/") -> list[Listing]:
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for card in soup.select("div.estate[data-idinz]"):
        lid = card["data-idinz"].strip()
        a = card.select_one("h2 a") or card.select_one("a[href]")
        if not a:
            continue
        price_el = card.select_one(".price")
        price = clean(price_el.get_text(" ") if price_el else "")
        price = price.replace(",00", "").replace(" / mesiac", "").replace(" €", "").strip()
        # "580 + 240" -> "580 €/mes. + 240 € energie"; "460" -> "460 €/mes."
        m = re.fullmatch(r"([\d\s.,]+)\+\s*([\d\s.,]+)", price)
        if m:
            price = f"{m.group(1).strip()} €/mes. + {m.group(2).strip()} € energie"
        elif re.fullmatch(r"[\d\s.,]+", price):
            price = f"{price} €/mes."
        loc_el = card.select_one(".location")
        area_el = card.select_one(".areas .value")
        out.append(Listing(
            source="topreality",
            id=lid,
            title=clean(a.get("title") or a.get_text()),
            price=price or "?",
            location=clean(loc_el.get_text(" ")).replace(" ,", ",") if loc_el else "",
            url=urljoin(base_url, a["href"]),
            details=clean(area_el.get_text()) if area_el else "",
        ))
    return out


# Почтовый индекс Братиславы -> район (okres). 81x = I, 82x = II, ...
BA_OKRES = {"81": "Bratislava I", "82": "Bratislava II", "83": "Bratislava III",
            "84": "Bratislava IV", "85": "Bratislava V"}


def parse_bazos(page_html: str, base_url: str = "https://reality.bazos.sk/") -> list[Listing]:
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for card in soup.select("div.inzeraty"):
        a = card.select_one("h2.nadpis a")
        if not a or not a.get("href"):
            continue
        m = re.search(r"/inzerat/(\d+)/", a["href"])
        if not m:
            continue
        price_el = card.select_one(".inzeratycena")
        loc_el = card.select_one(".inzeratylok")
        location = ""
        if loc_el:
            parts = [clean(s) for s in loc_el.stripped_strings]
            location = ", ".join(parts)
            psc = next((p for p in parts if re.fullmatch(r"\d{3} ?\d{2}", p)), None)
            if psc and psc[:2] in BA_OKRES and any("Bratislava" in p for p in parts):
                location = f"{BA_OKRES[psc[:2]]} ({psc})"
        out.append(Listing(
            source="bazos",
            id=m.group(1),
            title=clean(a.get_text()),
            price=clean(price_el.get_text(" ")) if price_el else "?",
            location=location,
            url=urljoin(base_url, a["href"]),
        ))
    return out


def parse_nehnutelnosti(page_html: str, base_url: str = "https://www.nehnutelnosti.sk/") -> list[Listing]:
    soup = BeautifulSoup(page_html, "html.parser")

    # 1) Районы из карточек: id -> "Ulica, Bratislava-Ružinov, okres Bratislava II"
    locations: dict[str, str] = {}
    for h2 in soup.select('h2[data-test-id="text"]'):
        card = h2
        while card is not None and not card.select_one('a[href*="/detail/"]'):
            card = card.parent
        if card is None:
            continue
        m = re.search(r"/detail/([^/?#]+)", card.select_one('a[href*="/detail/"]')["href"])
        if not m:
            continue
        texts = [clean(p.get_text()) for p in card.select('p[data-test-id="text"]')]
        loc = next((t for t in texts if "okres " in t), None) \
            or next((t for t in texts if "Bratislava" in t and len(t) < 120), "")
        locations.setdefault(m.group(1), loc)

    # 2) Основные данные из JSON-LD (стабильнее, чем CSS-классы MUI)
    out: list[Listing] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes:
            if not isinstance(node, dict):
                continue
            items = (node.get("mainEntity") or {}).get("itemListElement") or node.get("itemListElement") or []
            for li in items:
                item = li.get("item", li) if isinstance(li, dict) else {}
                url = item.get("url", "")
                m = re.search(r"/detail/([^/?#]+)", url)
                if not m:
                    continue
                lid = m.group(1)
                price = (item.get("offers") or {}).get("price") or (item.get("priceSpecification") or {}).get("price")
                details = []
                if item.get("category"):
                    details.append(item["category"])
                size = (item.get("floorSize") or {}).get("value")
                if size:
                    details.append(f"{size:g} m²" if isinstance(size, (int, float)) else f"{size} m²")
                out.append(Listing(
                    source="nehnutelnosti",
                    id=lid,
                    title=clean(item.get("name")),
                    price=f"{price} €/mes." if price not in (None, "") else "?",
                    location=locations.get(lid, ""),
                    url=urljoin(base_url, url),
                    details=" · ".join(details),
                ))

    # Запасной вариант, если JSON-LD пропал: берём ссылки из карточек
    if not out:
        for lid, loc in locations.items():
            a = soup.select_one(f'a[href*="/detail/{lid}"]')
            card = a
            while card is not None and not card.select_one('h2[data-test-id="text"]'):
                card = card.parent
            title = clean(card.select_one('h2[data-test-id="text"]').get_text()) if card else lid
            price = ""
            if card:
                price = next((clean(p.get_text()) for p in card.select('p[data-test-id="text"]')
                              if "€/mes" in p.get_text() and "m²" not in p.get_text()), "")
            out.append(Listing("nehnutelnosti", lid, title, price or "?", loc, urljoin(base_url, a["href"])))
    return out


PARSERS: dict[str, Callable[[str], list[Listing]]] = {
    "topreality": parse_topreality,
    "bazos": parse_bazos,
    "nehnutelnosti": parse_nehnutelnosti,
}


# ---------------------------------------------------------------------------
# Загрузка страниц
# ---------------------------------------------------------------------------

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url: str, attempts: int = 3) -> str:
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            r.encoding = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else "utf-8"
            return r.text
        except requests.RequestException as e:
            last_err = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"не удалось загрузить {url}: {last_err}")


def collect(source: str) -> list[Listing]:
    seen_ids: set[str] = set()
    result: list[Listing] = []
    for url in SEARCH_URLS[source]:
        for item in PARSERS[source](fetch(url)):
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                result.append(item)
        time.sleep(1.5)
    if not result:
        raise RuntimeError("страница загрузилась, но объявлений не найдено — возможно, поменялась вёрстка сайта")
    return result


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_api(method: str, **params) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Нет переменной окружения TELEGRAM_BOT_TOKEN")
    for _ in range(5):
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=30)
        data = r.json()
        if r.status_code == 429:  # слишком часто — ждём, сколько просит Telegram
            time.sleep(int(data.get("parameters", {}).get("retry_after", 5)) + 1)
            continue
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description')}")
        return data
    raise RuntimeError(f"Telegram {method}: слишком много повторов")


def tg_send(text: str, preview: bool = True) -> None:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        raise SystemExit("Нет переменной окружения TELEGRAM_CHAT_ID")
    tg_api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
           disable_web_page_preview="false" if preview else "true")


def notify(text: str) -> None:
    """Служебное сообщение: ошибка отправки не должна мешать сохранить состояние."""
    try:
        tg_send(text, preview=False)
    except RuntimeError as e:
        print(f"❌ Telegram: {e}", file=sys.stderr)


def format_listing(item: Listing) -> str:
    e = html.escape
    lines = [f"🏠 <b>{e(item.title)}</b>", f"💶 {e(item.price)}"]
    if item.location:
        lines.append(f"📍 {e(item.location)}")
    if item.details:
        lines.append(f"📐 {e(item.details)}")
    lines.append(f'🔗 <a href="{e(item.url, quote=True)}">Открыть на {e(SOURCE_LABELS[item.source])}</a>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Состояние (seen.json)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("⚠️ seen.json повреждён, начинаю с нуля", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    for source, ids in state.get("seen", {}).items():
        state["seen"][source] = {k: v for k, v in ids.items() if v >= cutoff}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Основной прогон
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> int:
    state = load_state()
    seen: dict[str, dict[str, str]] = state.setdefault("seen", {})
    health: dict[str, int] = state.setdefault("failures", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    budget = MAX_MESSAGES_PER_RUN
    ok_sources = 0

    for source in SEARCH_URLS:
        label = SOURCE_LABELS[source]
        try:
            listings = collect(source)
        except Exception as e:  # noqa: BLE001 — один сломанный сайт не должен ронять остальные
            health[source] = health.get(source, 0) + 1
            print(f"❌ {label}: {e} (подряд: {health[source]})", file=sys.stderr)
            if health[source] == ALERT_AFTER_FAILURES and not dry_run:
                notify(f"⚠️ {label}: не получается проверить объявления уже {health[source]} раз подряд.\n"
                       f"<code>{html.escape(str(e)[:300])}</code>")
            continue

        ok_sources += 1
        if health.get(source, 0) >= ALERT_AFTER_FAILURES and not dry_run:
            notify(f"✅ {label} снова работает.")
        health[source] = 0

        if MAX_PRICE is not None:
            listings = [x for x in listings if x.price_value is None or x.price_value <= MAX_PRICE]

        known = seen.get(source)
        if known is None:
            # Первый запуск для этого сайта: запоминаем всё молча, чтобы не завалить чат.
            seen[source] = {x.id: now for x in listings}
            print(f"🆕 {label}: первый запуск, запомнил {len(listings)} объявлений")
            if not dry_run:
                notify(f"👀 Начал следить за {label}: запомнил {len(listings)} текущих объявлений. "
                       f"Дальше буду присылать только новые.")
            continue

        new = [x for x in listings if x.id not in known]
        print(f"✔ {label}: на страницах {len(listings)}, новых {len(new)}")
        for item in reversed(new):  # сайты отдают сначала новые -> шлём в хронологическом порядке
            if dry_run:
                print("\n" + format_listing(item))
                continue
            if budget <= 0:
                print("…лимит сообщений за прогон исчерпан, остальное в следующий раз")
                break
            try:
                tg_send(format_listing(item))
            except Exception as e:  # noqa: BLE001
                print(f"❌ Telegram: {e}", file=sys.stderr)
                break
            known[item.id] = now
            budget -= 1
            time.sleep(1.1)

    if not dry_run:
        save_state(state)
    return 0 if ok_sources else 1


def print_all() -> None:
    for source in SEARCH_URLS:
        print(f"\n===== {SOURCE_LABELS[source]} =====")
        try:
            items = collect(source)
        except Exception as e:  # noqa: BLE001
            print(f"ошибка: {e}")
            continue
        for x in items:
            print(f"[{x.id}] {x.price:>28} | {x.location[:45]:45} | {x.title[:70]}")
        print(f"всего: {len(items)}")


def show_chat_ids() -> None:
    updates = tg_api("getUpdates").get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("first_name")
    if not chats:
        print("Пусто. Напиши своему боту любое сообщение в Telegram и запусти команду ещё раз.")
    for cid, name in chats.items():
        print(f"chat_id = {cid}   ({name})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--print", action="store_true", help="показать, что сейчас на страницах поиска")
    g.add_argument("--dry-run", action="store_true", help="показать новые объявления, ничего не отправляя")
    g.add_argument("--chat-id", action="store_true", help="узнать chat_id через getUpdates")
    g.add_argument("--test", action="store_true", help="отправить тестовое сообщение")
    args = p.parse_args()

    if args.print:
        print_all()
    elif args.chat_id:
        show_chat_ids()
    elif args.test:
        tg_send("✅ Бот работает, сюда будут приходить новые квартиры.", preview=False)
        print("Отправлено.")
    else:
        sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
