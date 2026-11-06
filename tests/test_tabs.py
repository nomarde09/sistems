"""Вкладки статусов в списке офиса и видимость взятой заявки."""

import re
import unittest

from test_app import m
from test_boss import BossHelpers


class TabsTests(BossHelpers):
    def setUp(self):
        super().setUp()
        self.new = self.make_request(client_name="Клиент новый")
        self.work = self.make_request(client_name="Клиент в работе")
        self.take(self.client("brigada1"), self.work)
        self.done = self.done_request("brigada2", client_name="Клиент выполнен")
        self.cancelled = self.make_request(client_name="Клиент отменён")
        self.cancel(self.client("rukovoditel"), self.cancelled)
        self.office = self.client("manager1")

    NAMES = ("Клиент новый", "Клиент в работе", "Клиент выполнен", "Клиент отменён")

    def shown(self, html):
        return {n for n in self.NAMES if n in html}

    def count(self, html, label):
        found = re.search(label + r' <span class="count">(\d+)</span>', html)
        return int(found.group(1)) if found else None

    def test_default_tab_is_new(self):
        html = self.office.get("/ofis").text
        self.assertEqual(self.shown(html), {"Клиент новый"})
        self.assertIn('class="status-tab tab-new active"', html)

    def test_each_tab_shows_only_its_status(self):
        expected = {"new": {"Клиент новый"}, "in_work": {"Клиент в работе"},
                    "done": {"Клиент выполнен"}, "cancelled": {"Клиент отменён"},
                    "all": set(self.NAMES)}
        for tab, names in expected.items():
            with self.subTest(tab=tab):
                self.assertEqual(self.shown(self.office.get(f"/ofis?status={tab}").text), names)

    def test_tab_counts(self):
        html = self.office.get("/ofis").text
        for label, n in (("Новые", 1), ("В работе", 1), ("Выполнены", 1), ("Отменены", 1), ("Все", 4)):
            self.assertEqual(self.count(html, label), n, label)

    def test_counts_follow_filters(self):
        html = self.office.get("/ofis?status=all&brigada=brigada1").text
        self.assertEqual(self.count(html, "В работе"), 1)
        self.assertEqual(self.count(html, "Выполнены"), 0)
        self.assertEqual(self.count(html, "Все"), 1)

    def test_tab_links_keep_filters(self):
        html = self.office.get("/ofis?status=all&brigada=brigada2").text
        self.assertIn("/ofis?status=done&amp;brigada=brigada2", html)

    def test_search_without_tab_looks_everywhere(self):
        html = self.office.get("/ofis?q=выполнен").text
        self.assertEqual(self.shown(html), {"Клиент выполнен"})
        self.assertIn('class="status-tab tab-all active"', html)

    def test_period_or_payment_opens_done_tab(self):
        html = self.office.get("/ofis?period=today").text
        self.assertIn('class="status-tab tab-done active"', html)
        self.assertEqual(self.shown(html), {"Клиент выполнен"})

    def test_empty_tab_points_to_other_tabs(self):
        self.cancel(self.client("rukovoditel"), self.new)
        html = self.office.get("/ofis").text
        self.assertIn("В этой вкладке заявок нет", html)
        self.assertIn("В работе (1)", html)

    def test_in_work_badge_names_the_brigade(self):
        html = self.office.get("/ofis?status=in_work").text
        self.assertIn("В работе · Бригада 1", html)
        card = self.office.get(f"/zayavka/{self.work}").text
        self.assertIn("В работе · Бригада 1", card)

    def test_taken_request_hidden_from_other_brigade_visible_to_office(self):
        rid = self.make_request(client_name="Свежая", address="ул. Видимая, 1")
        b1, b2 = self.client("brigada1"), self.client("brigada2")
        self.assertIn("ул. Видимая, 1", b2.get("/brigada").text)
        self.take(b1, rid)
        self.assertNotIn("ул. Видимая, 1", b2.get("/brigada").text)
        self.assertNotIn("ул. Видимая, 1", b2.get("/brigada/moi").text)
        for uid in ("rukovoditel", "manager1", "manager2"):
            html = self.client(uid).get("/ofis?status=in_work").text
            self.assertIn("Свежая", html, uid)
            self.assertIn("В работе · Бригада 1", html, uid)
            self.assertNotIn("Свежая", self.client(uid).get("/ofis").text, uid)  # не во «Новых»

    def test_back_link_returns_to_requests_tab(self):
        html = self.office.get(f"/zayavka/{self.done}").text
        self.assertIn('href="/ofis?status=done"', html)
        self.assertIn("← Выполнены", html)


if __name__ == "__main__":
    unittest.main()
