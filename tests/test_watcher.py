"""Тесты парсеров на реальной разметке сайтов (снята 19.11.2026) и логики "новое/старое".
Запуск:  python -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import watcher  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


class ParserTests(unittest.TestCase):
    def test_topreality(self):
        items = watcher.parse_topreality(read("topreality.html"))
        self.assertEqual([x.id for x in items], ["9471042", "9468302", "9471070"])
        a = items[0]
        self.assertIn("Drotárskej", a.title)
        self.assertEqual(a.price, "580 €/mes. + 240 € energie")
        self.assertEqual(a.price_value, 580)
        self.assertEqual(a.location, "Drotárska cesta, Bratislava-Staré Mesto (Bratislava I)")
        self.assertEqual(a.details, "47 m2")
        self.assertTrue(a.url.endswith("-r9471042.html"))
        self.assertEqual(items[1].price, "460 €/mes.")
        self.assertEqual(items[2].location, "Bratislava-Nové Mesto (Bratislava III)")

    def test_bazos(self):
        items = watcher.parse_bazos(read("bazos.html"))
        self.assertEqual([x.id for x in items], ["197662804", "197652800", "197650190"])
        self.assertEqual(items[0].price, "1 200 €")
        self.assertEqual(items[0].price_value, 1200)
        self.assertEqual(items[0].location, "Bratislava II (821 05)")
        self.assertEqual(items[0].url, "https://reality.bazos.sk/inzerat/197662804/moderny-3i-byt-s-balkonom-a-garazou-v-nuppu-ruzinov.php")
        self.assertEqual(items[1].price, "Dohodou")
        self.assertIsNone(items[1].price_value)
        self.assertEqual(items[1].location, "Bratislava V (851 07)")
        self.assertEqual(items[2].location, "Senec, 903 01")

    def test_nehnutelnosti(self):
        items = watcher.parse_nehnutelnosti(read("nehnutelnosti.html"))
        self.assertEqual([x.id for x in items], ["JuKaUYynQij", "JuAr5jP1nkp", "Ju_8Eup_Fp9"])
        a = items[0]
        self.assertEqual(a.price, "815 €/mes.")
        self.assertEqual(a.location, "Na piesku, Bratislava-Ružinov, okres Bratislava II")
        self.assertEqual(a.details, "2 izbový byt · 52.5 m²")
        self.assertTrue(a.url.startswith("https://www.nehnutelnosti.sk/detail/JuKaUYynQij/"))
        self.assertEqual(items[2].location, "Žltá 13, Bratislava-Petržalka, okres Bratislava V")

    def test_nehnutelnosti_fallback_without_jsonld(self):
        items = watcher.parse_nehnutelnosti(read("nehnutelnosti_no_ld.html"))
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].price, "815 €/mes.")
        self.assertIn("BOSEN", items[0].title)

    def test_format_escapes_html(self):
        x = watcher.Listing("bazos", "1", "A <b> & B", "500 €", "Bratislava I", "https://x.sk/?a=1&b=2")
        msg = watcher.format_listing(x)
        self.assertIn("A &lt;b&gt; &amp; B", msg)
        self.assertIn('href="https://x.sk/?a=1&amp;b=2"', msg)


class DetailTests(unittest.TestCase):
    def info(self, source, fixture, title, price):
        item = watcher.Listing(source, "1", title, price, "", "https://x.sk/")
        return watcher.analyze(item, watcher.DETAIL_PARSERS[source](read(fixture)))

    def test_topreality_three_times_rent(self):
        # "pri podpise ... sa platí nájomné, kaucia a provízia, t.j. 3 x 900,- EUR"
        i = self.info("topreality", "topreality_detail.html", "PRENÁJOM 2 izb. byt, Krátka ul.", "900 €/mes.")
        self.assertEqual((i.rooms, i.street), ("2-izb", "Krátka"))
        self.assertEqual((i.agency, i.agency_name), (True, ""))  # "Realitné kancelárie SR" — заглушка портала
        self.assertEqual((i.monthly, i.deposit, i.commission), (900, 900, 900))

    def test_bazos_amounts_from_text(self):
        # "Depozit: 780 € Prvé nájomné: 780 € Provízia realitnej kancelárie: 600 €"
        i = self.info("bazos", "bazos_detail.html", "Nový 1 izbový byt SLNEČNICE, Labutia ulica", "780 €")
        self.assertEqual((i.rooms, i.street, i.aircon, i.agency), ("1-izb", "Labutia", True, True))
        self.assertEqual((i.monthly, i.deposit, i.commission), (780, 780, 600))

    def test_nehnutelnosti_cena_komplet(self):
        i = self.info("nehnutelnosti", "nehnutelnosti_detail.html", "Prenájom: 2-izbový byt 65 m²", "800 €/mes.")
        self.assertEqual((i.street, i.aircon), ("Žltá 13", True))
        self.assertEqual((i.agency, i.agency_name), (True, "NL REAL, s.r.o."))
        self.assertEqual((i.monthly, i.deposit, i.commission), (1050, 1050, 1050))

    def test_text_rules(self):
        def info(text, price="600 €"):
            return watcher.analyze(watcher.Listing("bazos", "1", "Byt", price, "", "u"), watcher.Detail(text=text))
        i = info("Prenajímam byt, bez RK. Kaucia vo výške dvoch nájmov. Cena 600 € + 150 € energie. Bez klimatizácie.")
        self.assertEqual((i.agency, i.commission, i.aircon), (False, 0, False))
        self.assertEqual((i.monthly, i.deposit), (750, 1500))  # "dvoch nájmov" = 2 x аренда с энергиями
        i = info("Cena 600 € mesačne + energie. 1 mesačný depozit.")
        self.assertEqual(i.energies_state, "unknown")
        self.assertEqual((i.monthly, i.deposit, i.deposit_months, i.agency, i.aircon), (600, 600, 1, None, None))
        # "jednorázový" = разовый, а не "jeden mesiac"
        i = info("Celková mesačná platba je 1 075 € + jednorázovy depozit + jednorazová provízia pre RK.", "815 €")
        self.assertEqual((i.deposit_mentioned, i.deposit, i.commission), (True, None, None))

    def test_phrasings_from_real_listings(self):
        """Формулировки из 118 реальных объявлений, которые бот раньше читал неправильно."""
        def info(text, price="900 €", source="bazos", detail=None):
            d = detail or watcher.Detail()
            d.text = text
            return watcher.analyze(watcher.Listing(source, "1", "Byt", price, "", "u"), d)

        i = info("Pri podpise zmluvy sa platí depozit a provízia RK vo výške celkového mesačného nájmu.")
        self.assertEqual((i.deposit, i.commission), (900, 900))
        i = info("1x nájom. 1x depozit a 1x provízia pre RK", "580 €")
        self.assertEqual((i.deposit, i.commission), (580, 580))
        i = info("Nájom: 860 €/mes. Energie: 180 €. Depozit: vo výške 2 mesačných nájmov", "860 €")
        self.assertEqual((i.monthly, i.deposit), (1040, 2080))  # депозит считается от аренды с энергиями
        i = info("Nájom: 860 €. Energie: 180 €. Provízia vo výške čistého nájmu", "860 €")
        self.assertEqual(i.commission, 860)  # "čistého nájmu" — только тогда без энергий
        i = info("Vratný depozit predstavuje dvojnásobok celkovej mesačnej platby. Spolu: 1 000 €", "780 €")
        self.assertEqual(i.deposit, 2000)
        i = info("Pri podpise sa platí Nájom, depozit a 600 Eur pre RK.", "780 €")
        self.assertEqual((i.deposit_mentioned, i.deposit, i.commission), (True, None, 600))
        self.assertEqual(info("a odmena sprostredkovateľa vo výške 550,-€").commission, 550)
        self.assertEqual(info("Úhrada pre RK Hulka REAL, ktorá Vám zabezpečí obhliadku, v hodnote 1/2 z celého prenájmu.",
                              "680 €").commission, 340)
        for text in ("Províziu neplatíte.", "Nájomca neplatí províziu realitnej kancelárii", "Odmenu RK hradí majiteľ.",
                     "No agency commission."):
            self.assertEqual(info(text, detail=watcher.Detail(agency=True)).commission, 0, text)
        self.assertEqual(info("sa hradí nájomné na dva mesiace vopred a vratný depozit").first_months, 2)

        # цена и энергии
        i = info("Cena: 750,- eur vrátane energií pre jednu osobu, 800,- eur pre dve osoby.", "750 €")
        self.assertEqual((i.rent, i.energies_state, i.monthly), (750, "included", 750))
        self.assertEqual(info("Mesačný nájom 800.-€ plus správc. popl.a energie", "790 €").energies_state, "unknown")
        self.assertEqual(info("Mesačný nájom 800.-€ plus správc. popl.a energie", "790 €").rent, 800)
        i = info("Mesačný nájom vrátane energií a pripojenia na internet pre 2 osoby je 1.100,- eur", "900 €")
        self.assertEqual((i.rent, i.energies_state), (1100, "included"))
        i = info("Nájomné na mesiac: 850 € / byt – 250 € energie a internet", "850 €")
        self.assertEqual((i.energies, i.monthly), (250, 1100))
        self.assertEqual(info("Náklady (elektrina, voda, kúrenie) sú v cene.", "800 €").energies_state, "included")
        i = info("v prípade záujmu vlastné parkovacie miesto v areáli\nParkovacie miesto: 50 €/mes.\nEnergie: 120 €", "530 €")
        self.assertEqual((i.extras, i.monthly), ([], 650))
        i = info("Mesačné náklady na 2 osoby: 250 €\nElektrina 38 €\nInternet UPC 12 €\nSpolu: 1 100 €", "850 €",
                 source="nehnutelnosti", detail=watcher.Detail(energies=250))
        self.assertEqual((i.extras, i.monthly), ([], 1100))  # интернет уже внутри 250 €

        # клима, комнаты, риелтор, прочее
        self.assertFalse(info("Klimatizácia: nie (pamiatková ochrana domu)").aircon)
        self.assertTrue(info("Amenities include air conditioning.").aircon)
        self.assertEqual(watcher.rooms_from("Prenájom 2-garzónky"), "Dvojgarsónka")
        self.assertEqual(watcher.rooms_from("3 iz byt po rekonštrukcii"), "3-izb")
        self.assertEqual(info("Prenajmem zariadenú izbu v 4 izbovom byte").rooms, "Комната в квартире")
        self.assertTrue(info("SHARK REAL – VÁŠ PARTNER V OBLASTI REALÍT").agency)
        # риелтор, подписанный именем: "majiteľ prenajíma", но рядом провизия — это агентство
        self.assertTrue(info("Majiteľ prenajíma 2-izbový byt. Provízia RK 500 €.").agency)
        self.assertFalse(info("Majiteľ prenajíma 2-izbový byt, bez provízie.").agency)
        self.assertIsNone(info("Prenajmem byt. Volajte Peter.").agency)  # просто имя — не угадываем
        self.assertFalse(info("Nie som RK, provízia RK sa neplatí.").agency)
        self.assertFalse(info("Ako majiteľ nehnuteľnosti Vám ponúkame byt", detail=watcher.Detail(agency=True)).agency)
        self.assertIn("Žiline", info("Prenájom 2-izbového bytu v Žiline").warning)
        self.assertTrue(watcher.WANTED_RE.search("Hľadám prenájom nepriechodnej izby"))

        # цена зависит от числа жильцов — второй вариант показываем
        self.assertEqual(info("Cena: 750,- eur vrátane energií pre jednu osobu, 800,- eur pre dve osoby.", "750 €").persons_note, "вдвоём 800 €")
        self.assertEqual(info("nájomné je 900,- EUR/mesiac. Pri 2 osobách je cena 950,- EUR/mesiac.").persons_note, "вдвоём 950 €")
        self.assertEqual(info("Nájom 700 €, druhá osoba plus 50,-€", "700 €").persons_note, "вдвоём +50 €")
        self.assertEqual(info("680,-€/mes. vrátane energií - 2 osoby\n650,-€/mes. vrátane energií - 1 osoba", "680 €")
                         .persons_note, "одному 650 €")
        self.assertEqual(info("Energie: 220 € / 1 osoba; 250 € / 2 osoby", "780 €").persons_note, "вдвоём энергии 250 €")

        # улица на сайте и в тексте разная — показываем обе
        names = ["Poludníková", "Vrakúnska", "Trnavská cesta"]
        with mock.patch.object(watcher, "_streets", {watcher.fold(n): n for n in names}):
            i = info("Byt sa nachádza v bytovom dome na Vrakúnskej ulici BA II", detail=watcher.Detail(street="Poludníková"))
            self.assertEqual((i.street, i.street_alt), ("Poludníková", "Vrakúnska"))
            i = info("Byt pri Trnavskej ceste", detail=watcher.Detail(street="Poludníková"))
            self.assertEqual(i.street_alt, "")  # "pri ..." — рядом, а не адрес

    def test_price_breakdown(self):
        def line(text, price):
            item = watcher.Listing("bazos", "1", "Byt", price, "", "u")
            return watcher.price_line(item, watcher.analyze(item, watcher.Detail(text=text)))
        # Bosen: аренда, энергии, обязательная парковка и "Spolu"
        self.assertEqual(line("Nájomné: 815 €/mesiac\nPrevádzkové náklady a služby (energie, internet): 210 €/mesiac\n"
                              "Parkovacie miesto: 50 €/mesiac\nSpolu: 1 075 €/mesiac", "815 €"),
                         "815 € аренда + 210 € энергии + 50 € парковка = 1 075 €/мес")
        # в цене объявления итог, аренда только в тексте; парковка без "/mesiac"
        self.assertEqual(line("• Nájomné: 815 EUR\n• Energie, služby: 210 EUR\n• Parkovacie miesto: 50 EUR\n"
                              "Celková mesačná platba: 1 075 EUR", "1 075 €"),
                         "815 € аренда + 210 € энергии + 50 € парковка = 1 075 €/мес")
        self.assertEqual(line("Cena 630 EUR vrátane wifi, KBTV, energií. Možnosť parkovania za 55 € / mesiac.", "630 €"),
                         "630 €/мес, энергии включены")
        self.assertEqual(line("Cena 600 € mesačne + energie.", "600 €"), "600 €/мес + энергии (сумма не указана)")
        self.assertEqual(line("Pekný byt.", "600 €"), "600 €/мес, про энергии ничего не сказано")

    def test_streets_to_nominative(self):
        names = ["Bajkalská", "Labutia", "Drotárska cesta", "Koceľova", "Na Piesku", "Vyšehradská", "Krátka"]
        with mock.patch.object(watcher, "_streets", {watcher.fold(n): n for n in names}):
            c = watcher.canonical_street
            self.assertEqual(c("Bajkalskej"), "Bajkalská")
            self.assertEqual(c("Labutej"), "Labutia")
            self.assertEqual(c("Drotárskej"), "Drotárska cesta")
            self.assertEqual(c("Koceľovej"), "Koceľova")
            self.assertEqual(c("Na piesku"), "Na Piesku")
            self.assertEqual(c("Vyšehradská 6"), "Vyšehradská 6")
            self.assertIsNone(c("Tichá"))  # такой улицы нет -> не выдумываем
            self.assertEqual(watcher.street_from("Prenájom zar. garsónky na Bajkalskej, Ružinov"), "Bajkalská")
            self.assertEqual(watcher.street_from("Nový byt SLNEČNICE, Labutia ulica, Bratislava"), "Labutia")
            self.assertEqual(watcher.street_from("Tichá ulica, byt na Labutej ulici"), "Labutia")

    def test_format_with_info(self):
        x = watcher.Listing("topreality", "1", "Byt", "900 €/mes.", "Krátka, Bratislava-Staré Mesto", "https://x.sk/")
        i = watcher.Info(rooms="2-izb", street="Krátka", agency=True, rent=900, energies_state="included",
                         monthly=900, deposit=900, deposit_mentioned=True)
        msg = watcher.format_listing(x, i)
        self.assertIn("🏠 2-izb · <b>Byt</b>", msg)
        self.assertIn("💶 900 €/мес, энергии включены", msg)
        self.assertIn('query=Kr%C3%A1tka%2C+Bratislava">Krátka</a> · Bratislava-Staré Mesto', msg)
        self.assertIn("провизия не указана", msg)
        self.assertIn("Первый платёж: от 1 800 € = 900 (1-й мес.) + 900 (депозит) + ? (провизия)", msg)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "seen.json"
        self.sent = []
        pages = {"topreality": read("topreality.html"), "bazos": read("bazos.html"),
                 "nehnutelnosti": read("nehnutelnosti.html")}
        self.pages = pages

        def fake_fetch(url, attempts=3):
            for k, v in self.pages.items():
                if k in url:
                    return v
            raise AssertionError(url)

        self.patches = [
            mock.patch.object(watcher, "STATE_FILE", self.state),
            mock.patch.object(watcher, "fetch", side_effect=fake_fetch),
            mock.patch.object(watcher, "tg_send", side_effect=lambda t, preview=True: self.sent.append(t)),
            mock.patch.object(watcher.time, "sleep"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_first_run_is_silent_then_only_new(self):
        self.assertEqual(watcher.run(), 0)
        self.assertEqual(len(self.sent), 3)  # три сообщения "начал следить"
        self.assertTrue(all("Начал следить" in s for s in self.sent))
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state["seen"]["topreality"]), 3)

        self.sent.clear()
        self.assertEqual(watcher.run(), 0)
        self.assertEqual(self.sent, [])  # ничего нового

        # появилось новое объявление на bazos
        self.pages["bazos"] = self.pages["bazos"].replace("197652800", "199999999")
        self.sent.clear()
        watcher.run()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("199999999", self.sent[0])
        self.assertIn("199999999", json.loads(self.state.read_text())["seen"]["bazos"])

    def test_broken_site_does_not_block_others_and_alerts_once(self):
        watcher.run()
        self.pages["topreality"] = "<html><body>maintenance</body></html>"
        self.pages["bazos"] = self.pages["bazos"].replace("197652800", "199999999")
        self.sent.clear()
        for _ in range(watcher.ALERT_AFTER_FAILURES + 2):
            watcher.run()
        alerts = [s for s in self.sent if "TopReality" in s and "подряд" in s]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(sum("199999999" in s for s in self.sent), 1)

        self.pages["topreality"] = read("topreality.html")
        self.sent.clear()
        watcher.run()
        self.assertTrue(any("снова работает" in s for s in self.sent))

    def test_detail_page_down_still_sends_basic_message(self):
        watcher.run()
        self.pages["bazos"] = self.pages["bazos"].replace("197652800", "199999999")
        real_fetch = watcher.fetch.side_effect

        def fetch(url, attempts=3):
            if "/inzerat/" in url:
                raise RuntimeError("503")
            return real_fetch(url)
        watcher.fetch.side_effect = fetch
        self.sent.clear()
        watcher.run()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("199999999", self.sent[0])
        self.assertNotIn("Первый платёж", self.sent[0])

    def test_max_price_filter(self):
        with mock.patch.object(watcher, "MAX_PRICE", 901.0):
            watcher.run()
            seen = json.loads(self.state.read_text())["seen"]["bazos"]
            self.assertNotIn("197662804", seen)   # 1 200 € отфильтровано
            self.assertIn("197652800", seen)      # "Dohodou" оставляем


if __name__ == "__main__":
    unittest.main()
