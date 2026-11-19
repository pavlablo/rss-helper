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
    python watcher.py --sample     # прислать по одному свежему объявлению с каждого сайта (seen.json не трогает)
"""
from __future__ import annotations

import argparse
import html
import itertools
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urljoin

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
# Страница объявления: улица, комнаты, клима, риелтор, депозит, провизия
# ---------------------------------------------------------------------------

@dataclass
class Detail:
    text: str = ""              # заголовок + описание, по нему ищем всё остальное
    street: str = ""
    rooms: str = ""
    agency: bool | None = None  # True = риелтор, False = без риелтора, None = не понятно
    agency_name: str = ""
    energies: int | None = None             # из отдельного поля сайта, если есть
    energies_included: bool | None = None
    deposit: int | None = None


def page_text(el) -> str:
    """Текст с переносами строк между блоками: регулярки не перескакивают через абзацы."""
    if el is None:
        return ""
    lines = (clean(line) for line in el.get_text("\n").splitlines())
    return "\n".join(line for line in lines if line)


def parse_topreality_detail(page_html: str) -> Detail:
    soup = BeautifulSoup(page_html, "html.parser")
    d = Detail(text="\n".join([page_text(soup.select_one("h1")), page_text(soup.select_one(".description"))]))
    for li in soup.select(".properties li"):
        t = clean(li.get_text(" "))
        if t.startswith("Ulica "):
            d.street = t[len("Ulica "):]
        elif t.startswith("Kategória "):
            d.rooms = rooms_from(t)
    agency = soup.select_one(".a-agency")
    if agency:
        d.agency, d.agency_name = True, clean(agency.get_text())
    return d


def parse_bazos_detail(page_html: str) -> Detail:
    soup = BeautifulSoup(page_html, "html.parser")
    d = Detail(text="\n".join([page_text(soup.select_one("h1")), page_text(soup.select_one(".popisdetail"))]))
    for row in soup.select("td.listadvlevo tr"):
        cells = [clean(c.get_text(" ")) for c in row.select("td")]
        if len(cells) >= 2 and cells[0].startswith("Meno") and AGENCY_NAME_RE.search(cells[1]):
            d.agency, d.agency_name = True, cells[1].replace(" ,", ",")
    return d


def parse_nehnutelnosti_detail(page_html: str) -> Detail:
    soup = BeautifulSoup(page_html, "html.parser")
    d = Detail(text="\n".join([page_text(soup.select_one("h1")), page_text(soup.select_one("#detail-description"))]))
    # Данные объявления лежат в Next.js-скриптах как экранированный JSON.
    raw = page_html.replace('\\"', '"')
    m = re.search(r'"isWithinGracePeriod":\w+,"location":\{([^{}]*)', raw)
    if m:
        street = re.search(r'"street":"([^"]+)"', m.group(1))
        number = re.search(r'"streetNumber":"([^"]+)"', m.group(1))
        if street:
            d.street = street.group(1) + (f" {number.group(1)}" if number else "")
    m = re.search(r'"advertiser":\{"id":"[^"]*","name":"([^"]*)".*?"type":"(\w+)"(?:,"agency":\{"id":"[^"]*","name":"([^"]*)")?', raw)
    if m:
        d.agency = m.group(2) == "AGENT"
        d.agency_name = (m.group(3) or m.group(1)).replace("\\u0026", "&")
    # Первый блок цены на странице — у самого объявления, дальше идут похожие.
    m = re.search(r'"powerCosts":\{"value":"([^"]*)","areIncluded":(true|false)', raw)
    if m:
        d.energies_included = m.group(2) == "true" or None
        if m.group(1):
            d.energies = money(re.match(r"[\d  .]+", m.group(1)).group(0).strip())
    m = re.search(r'"label":"Depozit[^"]*","value":"([\d  .]+)', raw)
    if m:
        d.deposit = money(m.group(1).strip())
    return d


DETAIL_PARSERS: dict[str, Callable[[str], Detail]] = {
    "topreality": parse_topreality_detail,
    "bazos": parse_bazos_detail,
    "nehnutelnosti": parse_nehnutelnosti_detail,
}

# Сумма: "1 075 €", "900,- EUR", "780€", "1.050 eur", "800.-€", "1.100,- eur"
AMOUNT = r"(\d{1,3}(?:[  .]\d{3})+|\d+)(?:,\d{1,2}|[,.]-)?\s*(?:€|eur\b)"
# Кратность месячной платы: "1x", "1 mesačný", "jedného nájmu", "dvojnásobok", "2 nájmy", "1/2"
MULT_WORD = r"(\d/\d|\d|jedn(?!or[aá]z)\w*|dvoj\w*|dvoch|dva|dve|troj\w*|troch|tri|polovic\w*)"  # "jednorázový" = разовый
# Чего месяц. По умолчанию — аренда вместе с энергиями (так в Братиславе считают депозит и почти всегда провизию),
# чистая аренда — только если прямо написано "čistého nájmu" / "nájmu bez energií"
MONTH_UNIT = (r"(?:z\s+)?(?:celkov\w+\s+|komplet\w*\s+)?(?:celého\s+)?(?P<net>čist\w+\s+)?(?:mesačn\w+\s+)?"
              r"(?:nájm\w*|nájomn\w*|platb\w*|prenájm\w*|mesiac\w*)(?P<net2>\s+bez\s+energi\w*)?")
DEPOSIT_KW = r"(?:kauci|depozit|zábezpek|zabezpek)\w*"
COMMISSION_KW = (r"(?:provízi\w*|provizi\w*|odmen\w*\s+(?:pre\s+)?(?:RK|realit\w*|sprostredkovate\w*)|"
                 r"úhrad\w*\s+pre\s+(?:RK|realit\w*)|"
                 r"poplat\w*\s+(?:pre\s+)?(?:RK|realit\w*))")
COMMISSION_FOR_RK = rf"{AMOUNT}\s+(?:pre|za)\s+(?:RK|realitn\w+\s+kancel\w*|sprostredk\w*)"  # "a 600 Eur pre RK"
NO_COMMISSION_RE = re.compile(
    r"bez\s+provízi|neplatí\w*\s+(?:\w+\s+){0,2}?provízi|provízi\w*\s+(?:sa\s+)?neplatí|"
    r"(?:odmen|provízi)\w*\s+(?:\w+\s+){0,2}?hradí\s+(?:majite|prenajímate)|\bno\s+(?:agency\s+)?(?:commission|fee)",
    re.I)
AGENCY_NAME_RE = re.compile(r"\bRK\b|realit|kancel|s\.\s?r\.\s?o|maklér|broker|\breal\b|reality", re.I)
# Сдаёт сам владелец — сильнее, чем "агент" в профиле сайта
OWNER_RE = re.compile(
    r"bez\s+(?:RK|realitn\w+|realitky|sprostredkovate\w*)|nie\s+(?:som|sme)\s+(?:RK|realit\w*)|RK\s+nevola\w*|"
    r"ako\s+majite\w+|as\s+the\s+(?:property\s+)?owner|priamo\s+od\s+majite\w+",
    re.I)
NO_AGENCY_RE = re.compile(
    OWNER_RE.pattern + r"|súkromn\w+\s+(?:osob\w*|inzer\w*|majite\w*)|od\s+majite\w+|majite\w*\s+(?:bytu\s+)?(?:ponúka|prenajíma)",
    re.I)
AGENCY_TEXT_RE = re.compile(r"\bRK\b|realitn\w+\s+kancel|maklér|realít\b|\b[A-ZÁ-Ž]{3,}\s+REAL(?:ITY)?\b")
# Объявление "ищу квартиру" — не предложение, не шлём
WANTED_RE = re.compile(r"^\W*(?:hľadám|hladam|hľadáme|hladame|kúpim|vymením)\b", re.I)
ROOM_ONLY_RE = re.compile(
    r"\b(?:prenaj\w+|prenájom|ponúk\w+)\s+(?:\w+\s+){0,2}?izb[uay]\b(?!\s*(?:byt|\d))|^\W*izba\b|"
    r"\bizb[au]\s+v\s+(?:\w+\s+){0,3}?\d?\s*-?\s*izbov\w+\s+byt", re.I | re.M)
OTHER_CITY_RE = re.compile(
    r"\bv\s+(Žiline|Trnave|Nitre|Košiciach|Trenčíne|Banskej\s+Bystrici|Prešove|Martine|Senci|Pezinku|Malackách|Šamoríne)\b")
# Районы Братиславы: "Ružinov" нельзя превращать в улицу "Ružinovská"
DISTRICTS = ["ruzinov", "petrzalk", "karlov", "dubravk", "lamac", "devin", "rac", "vajnor", "vrakun", "biskupic",
             "jarovc", "rusovc", "cunov", "nove", "stare", "novom", "starom", "slnecnic", "nivy", "mesto", "meste"]


def money(s: str) -> int:
    return int(re.sub(r"[  .]", "", s))


def mult(word: str) -> float:
    w = word.lower()
    if "/" in w:
        a, b = w.split("/")
        return int(a) / int(b)
    if w.isdigit():
        return int(w)
    if w.startswith("polovic"):
        return 0.5
    return 1 if w.startswith("jedn") else 2 if w.startswith("dv") else 3


def rooms_from(text: str) -> str:
    if re.search(r"\b(?:2|dvoj)\s*-?\s*gar[sz]ón|dvojgar[sz]ón", text, re.I):
        return "Dvojgarsónka"
    if re.search(r"gar[sz]ón|garsonk", text, re.I):
        return "Garsónka"
    m = re.search(r"(\d(?:[,.]5)?)\s*[-‐–]?\s*(?:izb|iz\.|iz\s+byt|i\s+byt\b)", text, re.I)
    return f"{m.group(1).replace('.', ',')}-izb" if m else ""


STREETS_FILE = Path(__file__).with_name("streets.txt")  # все улицы Братиславы из OpenStreetMap
_streets: dict[str, str] | None = None


def fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()


def canonical_street(raw: str) -> str | None:
    """'Bajkalskej' -> 'Bajkalská', 'Drotárskej' -> 'Drotárska cesta'. None, если такой улицы нет.
    Google Maps косвенный падеж не находит, поэтому ищем по началу слов в справочнике."""
    global _streets
    if _streets is None:
        names = STREETS_FILE.read_text(encoding="utf-8").splitlines() if STREETS_FILE.exists() else []
        _streets = {fold(n): n for n in names if n.strip()}
    if not _streets:
        return raw
    m = re.fullmatch(r"(.*?)(?:\s+(\d+[a-zA-Z]?(?:/\d+)?))?", raw.strip())
    name, number = m.group(1), m.group(2)
    words = fold(name).split()
    if not words:
        return None
    if " ".join(words) in _streets:
        found = _streets[" ".join(words)]
    else:
        if any(re.fullmatch(stem + r"\w{0,3}", words[0]) for stem in DISTRICTS):
            return None  # "Ružinove", "Petržalke" — район, а не улица

        def word_ok(a: str, b: str) -> bool:  # разные окончания одного слова: bajkalskej / bajkalska
            p = len(os.path.commonprefix([a, b]))
            return p >= min(4, len(b) - 1) and p >= len(a) - 4 and p >= len(b) - 3 or a == b
        best = None
        for key, n in _streets.items():
            kw = key.split()
            extra = kw[len(words):]
            if len(kw) < len(words) or not all(word_ok(a, b) for a, b in zip(words, kw)):
                continue
            if extra and extra not in (["cesta"], ["ulica"], ["namestie"], ["nabrezie"]):
                continue
            score = (len(extra), sum(len(os.path.commonprefix([a, b])) for a, b in zip(words, kw)) * -1)
            if best is None or score < best[0]:
                best = (score, n)
        if not best:
            return None
        found = best[1]
    return f"{found} {number}" if number else found


STREET_WORD = r"[A-ZÁ-Ža-zá-ž][\w\-]+"
STREET_PATTERNS = [
    r"Ulica:\s*([^\n,]{3,40})",
    # "na ul. Znievska", "ulica FIALOVÁ", "ul. Tomášikova 9/10 posch." (9/10 — этаж, не номер дома)
    # Название с заглавной буквы: не "ulica tichšie bývanie", не "ulica na Hradnom kopci"
    rf"\bul(?:ica|\.)\s*:?\s+([A-ZÁ-Ž][\w\-]+(?:\s+{STREET_WORD}){{0,2}}(?:\s+\d+[a-z]?(?:/\d+)?(?![\d/]|\s*(?:posch|p\.|NP)))?)",
    rf"\b({STREET_WORD})\s+ul\.",                                                                  # "Donnerova ul."
    r"\b([A-ZÁ-Ž][\w\-]+\s+(?i:cesta|námestie))\b",                                               # "TRNAVSKÁ CESTA"
    r"\b([A-ZÁ-Ž][\w\-]+)\s+(?i:ulica)\b",                                                        # "Labutia ulica"
    r"na\s+ulici\s+([A-ZÁ-Ž][\w.\-]*(?:\s+[A-ZÁ-Ža-z][\w.\-]*){0,2}?)(?=\s*[,.\n]|\s+v\s|\s+\d)",
    r"\b(?:na|pri|v)\s+((?:[A-ZÁ-Ž][\w\-]+\s+){0,2}?[A-ZÁ-Ž][\w\-]+)\s+(?:ul\.|ulici|ulica|ceste|cesty|námestí)",
    r"\bna\s+([A-ZÁ-Ž][\w\-]+(?:skej|ckej|ovej|nej|ej))\b",                                       # "garsónka na Bajkalskej"
]
# Только фразы, где улица — адрес самой квартиры ("bytový dom na Vrakúnskej ulici"), а не "pri Trnavskej ceste"
FLAT_STREET_PATTERNS = STREET_PATTERNS[:3] + [
    STREET_PATTERNS[5],
    r"\bna\s+((?:[A-ZÁ-Ž][\w\-]+\s+){0,2}?[A-ZÁ-Ž][\w\-]+)\s+(?:ul\.|ulici)",
]


def streets_in(text: str, patterns: list[str] = STREET_PATTERNS) -> list[str]:
    """Все улицы из текста по порядку правил (только те, что реально есть в Братиславе)."""
    found = []
    for p in patterns:
        for m in re.finditer(p, text):
            words = clean(m.group(1)).split()
            number = words.pop() if len(words) > 1 and re.fullmatch(r"\d+[a-z]?(?:/\d+)?", words[-1]) else None
            for n in range(len(words), 0, -1):  # "Fialová v Petržalke" -> "Fialová v" -> "Fialová"
                street = canonical_street(" ".join(words[:n]))
                if street:
                    found.append(f"{street} {number}" if number else street)
                    break
    return found


def street_from(text: str, patterns: list[str] = STREET_PATTERNS) -> str:
    found = streets_in(text, patterns)
    return found[0] if found else ""


def sentences(text: str) -> list[str]:
    return re.split(r"[!?\n]|\.\s+(?=[A-ZÁ-Ž])", text)  # "t.j. 3 x" и "ul. Znievska" не режем


def find_part(text: str, keyword: str, rent: int | None, total: int | None,
              commission: bool = False) -> tuple[bool, int | None, float | None]:
    """(упомянут ли, сумма, во сколько месяцев) для депозита или провизии.
    Месяцы != None — сумма посчитана из "1 mesačný nájom", а не написана в объявлении."""
    if not re.search(keyword, text, re.I) and not (commission and re.search(COMMISSION_FOR_RK, text, re.I)):
        return False, None, None
    if commission:
        m = re.search(COMMISSION_FOR_RK, text, re.I)  # "Nájom, depozit a 600 Eur pre RK"
        if m:
            return True, money(m.group(1)), None
    # "Depozit: 780 €", "vratná kaucia 1 050 €" (но не "depozit a 600 Eur pre RK")
    m = re.search(rf"{keyword}[^.!?\n\d]{{0,60}}?{AMOUNT}(?!\s+(?:pre|za)\s+(?:RK|realit|sprostred))", text, re.I)
    if m:
        return True, money(m.group(1)), None
    for s in sentences(text):
        if not re.search(keyword, s, re.I):
            continue
        # "depozit a provízia RK vo výške celkového mesačného nájmu", "kaucia vo výške dvoch nájmov",
        # "Depozit vo výške 1x mesiac", "provízia v hodnote 1/2 z celého prenájmu"
        m = re.search(rf"{keyword}[^!?\n\d]{{0,220}}?(?:(?:výšk\w*|hodnot\w*|:|=)\s*(?:{MULT_WORD}\s*[x×]?\s*-?\s*)?"
                      rf"|{MULT_WORD}\s*[x×]?\s*-?\s*)(?:násob\w*\s+)?{MONTH_UNIT}", s, re.I)
        if not m:  # "1x depozit", "1 mesačný depozit", "dvojnásobná kaucia"
            m = re.search(rf"{MULT_WORD}\s*[x×]?\s*-?\s*(?:mesačn\w*\s+)?(?:vratn\w+\s+)?{keyword}", s, re.I)
        if m:
            k = next((mult(g) for g in m.groups()[:2] if g), 1)
            gd = m.groupdict()
            base = (rent or total) if gd.get("net") or gd.get("net2") else (total or rent)
            if base:
                return True, round(k * base), k
        # "nájomné, kaucia a provízia, t.j. 3 x 900,- EUR": каждая часть по 900
        m = re.search(rf"\d\s*[x×]\s*{AMOUNT}", s, re.I)
        if m:
            return True, money(m.group(1)), None
    return True, None, None


@dataclass
class Info:
    rooms: str = ""
    street: str = ""
    street_alt: str = ""            # в тексте другая улица, чем в поле сайта — покажем обе с "?"
    persons_note: str = ""          # "вдвоём 800 €", если цена зависит от числа жильцов
    warning: str = ""
    aircon: bool | None = None
    agency: bool | None = None
    agency_name: str = ""
    rent: int | None = None
    energies: int | None = None
    energies_state: str = "none"    # amount / included / unknown ("+ energie" без суммы) / none (ни слова)
    extras: list[tuple[str, int]] = field(default_factory=list)  # обязательные доплаты: парковка, интернет
    total_stated: int | None = None  # "Spolu: 1 075 €/mesiac" из описания
    monthly: int | None = None       # всё вместе в месяц
    first_months: int = 1            # "nájomné na dva mesiace vopred"
    deposit: int | None = None
    deposit_mentioned: bool = False
    deposit_months: float | None = None   # сумма посчитана как N месяцев, а не написана
    commission: int | None = None
    commission_mentioned: bool = False
    commission_months: float | None = None


ENERGIES_KW = r"(?:energi\w*|prevádzkov\w+\s+náklad\w*|náklad\w*\s+na\s+bývanie|poplatk\w*(?:\s+za\s+služb\w*)?)"
ENERGIES_INCLUDED_RE = re.compile(
    r"vrátane\s+(?:[\w,]+\s+){0,4}?energi|energi\w*\s+(?:sú\s+)?(?:v\s+cene|zahrnut|započítan)|"
    r"v\s+cene\s+(?:nájmu\s+|nájomného\s+)?(?:sú\s+)?(?:aj\s+|už\s+)?(?:všetky\s+)?energi|\bvč\.?\s+energi|\bs\s+energiami|"
    r"náklady\s*(?:\([^)]*\))?\s*sú\s+v\s+cene|(?:elektrina|energie|služby)[^.\n]{0,40}?sú\s+v\s+cene|všetko\s+v\s+cene|"
    r"utilit\w*[^.\n]{0,60}?included|including\s+utilities",
    re.I)
ENERGIES_UNKNOWN_RE = re.compile(
    r"(?:\+|plus)\s+(?:[\w.]+\s+){0,4}?energi|\+\s*(?:záloh\w*\s+(?:za\s+)?)?energi|\+\s*E\b|"
    r"energie\s+(?:sa\s+)?(?:platia|hradia)\s+(?:zvlášť|samostatne)", re.I)
EXTRAS = [("парковка", r"parkova\w*|garáž\w*|garážov\w*\s+stoj\w*"), ("интернет/ТВ", r"internet\w*|káblov\w*\s+TV")]
OPTIONAL_RE = re.compile(r"možn|osobitne|volite|príplat|záuj|nie\s*je\s+podmienk|nieje\s+podmienk|individuáln|doprenaj", re.I)
RENT_RE = re.compile(
    rf"(?:mesačn\w+\s+nájom\w*|nájomné|cena\s+(?:mesačného\s+)?(?:prenájmu|nájmu|nájomného)|\bnájom)(?:\s+na\s+mesiac)?"
    rf"(?P<gap>(?:[^\n\d€.]|\d\s*osob\w*){{0,70}}?)(?:je\s+)?[:–-]?\s*{AMOUNT}"
    rf"|\bcena\s*:?\s*{AMOUNT}\s*(?:/\s*)?(?:mesiac|mesačne|mes\b)", re.I)


def monthly_costs(item: Listing, d: Detail, text: str, info: Info) -> None:
    """Аренда, энергии, обязательные доплаты и итог в месяц."""
    info.rent = int(item.price_value) if item.price_value else None

    m = re.search(rf"(?:spolu|cena\s+komplet|celkov\w+\s+(?:mesačn\w+\s+)?(?:platb|cen|sum|nájomn)\w*)[^.!?\n\d]{{0,30}}?{AMOUNT}",
                  text, re.I)
    if m:
        info.total_stated = money(m.group(1))

    # Аренда из текста: на bazos в поле цены пишут что угодно, а в тексте "Mesačný nájom ... je 1.100,- eur"
    rent_m = RENT_RE.search(text)
    rent_text = money(rent_m.group(2) or rent_m.group(3)) if rent_m else None
    # "Cena prenájmu: 780€ / mesiac (vrátane energií...)" — только в той же строке, что и аренда
    rent_line = text[rent_m.start():].split("\n")[0][:160] if rent_m else ""
    rent_included = bool(ENERGIES_INCLUDED_RE.search(rent_line))
    if rent_text and (info.rent is None or item.source == "bazos"
                      or (info.total_stated == info.rent and rent_text < info.rent)):
        info.rent = rent_text

    in_price = re.search(r"\+\s*([\d\s]+)\s*€\s*energ", item.price)  # topreality: "580 €/mes. + 240 € energie"
    if d.energies:
        info.energies = d.energies
    elif in_price:
        info.energies = money(in_price.group(1).strip())
    elif not d.energies_included and not rent_included:
        for p in (rf"(?:\+|plus|–)\s*{AMOUNT}\s*(?:[\w/]+\s+){{0,3}}?(?:za\s+|na\s+)?energi",
                  rf"{ENERGIES_KW}[^.!?\n\d]{{0,90}}?{AMOUNT}"):
            for m in re.finditer(p, text, re.I):
                # не "750 € vrátane energií pre jednu osobu, 800 € pre dve osoby"
                # но "Prevádzkové náklady a služby vrátane internetu – 210 €" — это как раз энергии
                if not (re.search(r"vrátane|v\s+cene", text[max(0, m.start() - 30):m.start()], re.I)
                        or re.search(r"osob", m.group(0), re.I)):
                    info.energies = money(m.group(1))
                    break
            if info.energies:
                break
    if info.energies:
        info.energies_state = "amount"
    elif d.energies_included or rent_included or ENERGIES_INCLUDED_RE.search(text):
        info.energies_state = "included"
    elif ENERGIES_UNKNOWN_RE.search(text):
        info.energies_state = "unknown"

    # Доплаты с суммой в месяц. Обязательные идут в итог, "možnosť / v prípade záujmu" — нет
    parts = sentences(text) + re.split(r"\n|(?<=[.!?])\s+", text)
    optional = {label for label, kw in EXTRAS for s in parts if re.search(kw, s, re.I) and OPTIONAL_RE.search(s)}
    found: list[tuple[str, int, bool]] = []  # (что, сколько, необязательная ли)
    for line in re.split(r"\n|(?<=[.!?])\s+", text):
        if re.search(r"energi|prevádzkov|náklad|poplat", line, re.I):
            continue
        for label, kw in EXTRAS:
            # "... za 50 € mesačne" или строка прайса "• Parkovacie miesto: 50 EUR"
            m = re.search(rf"(?:{kw})[^\n\d€]{{0,50}}?{AMOUNT}(?:\s*/?\s*(?:mes|mesačne|mesiac)|\s*$)", line, re.I)
            if m and (label, money(m.group(1))) not in [(f[0], f[1]) for f in found]:
                found.append((label, money(m.group(1)), bool(OPTIONAL_RE.search(line)) or label in optional))
    found.sort(key=lambda f: f[2])  # обязательные первыми
    for label, n, opt in found:
        if not opt and label not in dict(info.extras):
            info.extras.append((label, n))

    base = (info.rent or 0) + (info.energies or 0)
    if info.total_stated and info.rent:
        # Раскладка должна сойтись со "Spolu": лишние подпункты ("Internet UPC 12 €" внутри 250 €) выкидываем,
        # а "Vyhradené parkovacie miesto: 50 €" берём, даже если где-то рядом "možnosť ďalšieho miesta"
        candidates = [(label, n) for label, n, _ in found]
        fits = [list(c) for k in range(len(candidates), -1, -1) for c in itertools.combinations(candidates, k)
                if base + sum(x for _, x in c) == info.total_stated and len({lbl for lbl, _ in c}) == len(c)]
        if fits:
            info.extras = fits[0]
        elif base + sum(x for _, x in info.extras) < info.total_stated:
            info.extras.append(("прочее", info.total_stated - base - sum(x for _, x in info.extras)))
    computed = base + sum(n for _, n in info.extras)
    info.monthly = info.total_stated or computed or None

    m = re.search(r"(?:nájom\w*|nájm\w*)\s+(?:na|za)\s+(dva|2|tri|3)\s+mesiac\w*\s+vopred", text, re.I)
    if m:
        info.first_months = 2 if m.group(1) in ("dva", "2") else 3


def persons_note(text: str, info: Info) -> str:
    """Цена зависит от числа жильцов: бот считает по первой цене, второй вариант показываем рядом."""
    m = re.search(rf"druh\w+\s+osob\w*\s*(?:plus|\+)\s*{AMOUNT}", text, re.I)  # "druhá osoba plus 50,-€"
    if m:
        return f"вдвоём +{eur(money(m.group(1)))} €"
    lines = text.splitlines()
    for who, label in ((r"(?:2|dve|dvoch)", "вдвоём"), (r"(?:1|jedn\w*)", "одному")):
        for i, line in enumerate(lines):
            # "800,- eur pre dve osoby", "250 € / 2 osoby", "680 €/mes. ... - 2 osoby", "Pri 2 osobách je cena 950"
            m = (re.search(rf"{AMOUNT}[^\n\d€]{{0,50}}?(?:pre|na|/|-|–)\s*{who}\s+osob", line, re.I)
                 or re.search(rf"(?:pri|pre)\s+{who}\s+osob\w*[^\n\d€]{{0,30}}?{AMOUNT}", line, re.I))
            if not m:
                continue
            n = money(m.group(1))
            # энергии: "Energie: 220 € / 1 osoba; 250 € / 2 osoby" или строка под заголовком "Energie a ... náklady:"
            j = i - 1
            while j >= 0 and re.match(r"\s*[-•–*]", lines[j]):  # поднимаемся по пунктам списка до заголовка
                j -= 1
            before = line[:m.start()] + (" " + lines[j] if j >= 0 and lines[j].rstrip().endswith(":") else "")
            energy = bool(re.search(r"energi|náklad|poplat|služb", before, re.I)) and not re.search(
                r"vrátane\s+(?:[\w,]+\s+){0,3}?energi", before, re.I)  # "750 € vrátane energií pre jednu osobu" — это цена
            if n in ((info.energies,) if energy else (info.rent, info.monthly)):
                continue  # эту сумму уже показали
            return f"{label}{' энергии' if energy else ''} {eur(n)} €"
    return ""


def analyze(item: Listing, d: Detail) -> Info:
    text = "\n".join([item.title, d.text])
    head = "\n".join([item.title, d.text[:300]])
    if ROOM_ONLY_RE.search(head):
        rooms = "Комната в квартире"
    else:
        rooms = rooms_from(item.title) or d.rooms or rooms_from(item.details) or rooms_from(d.text)
        in_text = rooms_from(d.text)
        if not rooms_from(item.title) and ("," in in_text and rooms[:1] == in_text[:1] or "gars" in in_text.lower()):
            rooms = in_text  # "Kategória: 2 izbový", а в тексте "2,5 izbový byt" или "2-garsónku"
    city = OTHER_CITY_RE.search(head)
    info = Info(rooms=rooms,
                street=(canonical_street(d.street) or d.street if d.street else "")
                or street_from(item.title) or street_from(d.text),
                warning=f"похоже, это не Братислава ({city.group(0)})" if city else "",
                agency=d.agency,
                # "Realitné kancelárie SR" — заглушка порталов вместо названия агентства
                agency_name="" if d.agency_name == "Realitné kancelárie SR" else d.agency_name)
    if d.street:  # поле сайта: Poludníková, а в тексте только "bytového domu na Vrakúnskej ulici"
        in_text = streets_in(d.text, FLAT_STREET_PATTERNS)
        if in_text and fold(info.street.split()[0]) not in {fold(s.split()[0]) for s in in_text}:
            info.street_alt = in_text[0]

    if re.search(r"bez\s+klim|klimatiz\w*\s*:?\s*(?:nie\b|nie\s+je|nemá)|nemá\s+klim", text, re.I):
        info.aircon = False
    elif re.search(r"klimatiz|klím|\bklima\b|air[- ]?condition", text, re.I):
        info.aircon = True

    if OWNER_RE.search(text):  # "bez RK", "nie som RK", "priamo od majiteľa" — хозяин сам
        info.agency = False
    elif info.agency is None:
        # Риелтор, подписанный просто именем, тоже пишет "majiteľ prenajíma byt", но рядом "provízia RK" —
        # признаки агентства важнее слабых слов хозяина
        positive = NO_COMMISSION_RE.sub(" ", text)  # "bez provízie" — не признак агентства
        if AGENCY_TEXT_RE.search(positive) or re.search(COMMISSION_KW, positive, re.I):
            info.agency = True
        elif NO_AGENCY_RE.search(text):
            info.agency = False

    monthly_costs(item, d, text, info)
    info.persons_note = persons_note(text, info)
    if d.deposit:
        info.deposit_mentioned, info.deposit = True, d.deposit
    else:
        info.deposit_mentioned, info.deposit, info.deposit_months = find_part(
            text, DEPOSIT_KW, info.rent, info.monthly)
    if info.agency is False or NO_COMMISSION_RE.search(text):
        info.commission_mentioned, info.commission = True, 0
    else:
        info.commission_mentioned, info.commission, info.commission_months = find_part(
            text, COMMISSION_KW, info.rent, info.monthly, commission=True)
    return info


def load_info(item: Listing) -> Info | None:
    try:
        return analyze(item, DETAIL_PARSERS[item.source](fetch(item.url, attempts=2)))
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ не удалось разобрать {item.url}: {e}", file=sys.stderr)
        return None


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


def months_str(m: float) -> str:
    return f"{m:g}".replace(".", ",")  # 0.5 -> "0,5"


def eur(n: int | None) -> str:
    return "?" if n is None else f"{n:,}".replace(",", " ")


def price_line(item: Listing, info: Info | None) -> str:
    """"815 € аренда + 210 € энергии + 50 € парковка = 1 075 €/мес" — сразу видно, что входит в цену."""
    if not info or info.rent is None:
        return item.price
    parts = [f"{eur(info.rent)} € аренда"]
    if info.energies_state == "amount":
        parts.append(f"{eur(info.energies)} € энергии")
    parts += [f"{eur(n)} € {label}" for label, n in info.extras]
    line = " + ".join(parts)
    if len(parts) > 1 or info.total_stated not in (None, info.rent):
        line += f" = {eur(info.monthly)} €/мес"
    else:
        line = f"{eur(info.rent)} €/мес"
    line += {
        "included": ", энергии включены",
        "unknown": " + энергии (сумма не указана)",
        "none": ", про энергии ничего не сказано",
    }.get(info.energies_state, "")
    return line + (f" · {info.persons_note}" if info.persons_note else "")


def format_listing(item: Listing, info: Info | None = None) -> str:
    e = html.escape
    rooms = f"{e(info.rooms)} · " if info and info.rooms else ""
    lines = [f"🏠 {rooms}<b>{e(item.title)}</b>", f"💶 {e(price_line(item, info))}"]

    location = item.location
    if info and info.street:
        def link(street: str) -> str:
            maps = "https://www.google.com/maps/search/?api=1&query=" + quote_plus(f"{street}, Bratislava")
            return f'<a href="{e(maps, quote=True)}">{e(street)}</a>'
        first, _, rest = location.partition(",")
        if first.strip().lower()[:5] == info.street.lower()[:5]:  # улица уже есть в районе, не повторяем
            location = rest.strip()
        # Сайт и текст называют разные улицы — показываем обе, пусть человек проверит
        street = f"{link(info.street)} или {link(info.street_alt)}?" if info.street_alt else link(info.street)
        lines.append(f"📍 {street}" + (f" · {e(location)}" if location else ""))
    elif location:
        lines.append(f"📍 {e(location)}")
    if item.details:
        lines.append(f"📐 {e(item.details)}")

    if info:
        if info.warning:
            lines.append(f"⚠️ {e(info.warning)}")
        lines.append("❄️ Клима: " + {True: "есть", False: "нет", None: "не указано"}[info.aircon])
        if info.agency is False:
            lines.append("🙋 Без риелтора")
        elif info.agency is None and not info.commission_mentioned:
            lines.append("❔ Риелтор: не указано")
        else:
            name = f" ({e(info.agency_name)})" if info.agency_name else ""
            if info.commission is None:
                fee = "провизия не указана"
            elif info.commission == 0:
                fee = "провизию не платишь"
            elif info.commission_months:
                fee = f"провизия {months_str(info.commission_months)} мес. (≈ {eur(info.commission)} €)"
            else:
                fee = f"провизия {eur(info.commission)} €"
            lines.append(f"🧑‍💼 С риелтором{name}, {fee}")

        # "≈ N мес." — в объявлении написано "1 mesačný nájom" без суммы, считаем от аренды
        def label(name: str, months: float | None) -> str:
            return f"{name} ≈ {months_str(months)} мес." if months else name
        month = ("1-й мес." if info.first_months == 1 else f"{info.first_months} мес. вперёд") + (
            " без энергий" if info.energies_state in ("unknown", "none") else "")
        parts = [(info.monthly * info.first_months if info.monthly else None, month),
                 (info.deposit, label("депозит", info.deposit_months) if info.deposit_mentioned else "депозит не указан")]
        if info.commission:
            parts.append((info.commission, label("провизия", info.commission_months)))
        elif info.commission is None and info.agency is not False:
            parts.append((None, "провизия"))
        total = sum(n for n, _ in parts if n)
        prefix = "от " if any(n is None for n, _ in parts) else "≈ " if info.deposit_months or info.commission_months else ""
        breakdown = " + ".join(f"{eur(n)} ({lbl})" for n, lbl in parts)
        lines.append(f"💰 Первый платёж: {prefix}{eur(total)} € = {breakdown}")

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
            if WANTED_RE.search(item.title):  # "Hľadám prenájom..." — человек ищет жильё, это не предложение
                known[item.id] = now
                continue
            if dry_run:
                print("\n" + format_listing(item, load_info(item)))
                continue
            if budget <= 0:
                print("…лимит сообщений за прогон исчерпан, остальное в следующий раз")
                break
            text = format_listing(item, load_info(item))  # без деталей, если страница объявления не открылась
            try:
                tg_send(text)
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


def send_samples() -> None:
    for source in SEARCH_URLS:
        try:
            items = collect(source)
        except Exception as e:  # noqa: BLE001
            print(f"❌ {SOURCE_LABELS[source]}: {e}")
            continue
        tg_send(format_listing(items[0], load_info(items[0])))
        print(f"✔ {SOURCE_LABELS[source]}: отправил {items[0].id}")
        time.sleep(1.1)


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
    g.add_argument("--sample", action="store_true", help="прислать по одному свежему объявлению с каждого сайта")
    args = p.parse_args()

    if args.print:
        print_all()
    elif args.chat_id:
        show_chat_ids()
    elif args.test:
        tg_send("✅ Бот работает, сюда будут приходить новые квартиры.", preview=False)
        print("Отправлено.")
    elif args.sample:
        send_samples()
    else:
        sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
