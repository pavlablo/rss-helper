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


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "seen.json"
        self.sent = []
        pages = {"topreality": read("topreality.html"), "bazos": read("bazos.html"),
                 "nehnutelnosti": read("nehnutelnosti.html")}
        self.pages = pages

        def fake_fetch(url):
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

    def test_max_price_filter(self):
        with mock.patch.object(watcher, "MAX_PRICE", 901.0):
            watcher.run()
            seen = json.loads(self.state.read_text())["seen"]["bazos"]
            self.assertNotIn("197662804", seen)   # 1 200 € отфильтровано
            self.assertIn("197652800", seen)      # "Dohodou" оставляем


if __name__ == "__main__":
    unittest.main()
