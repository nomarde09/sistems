"""Статистика руководителя: та же, что в техническом разделе, но без журнала, лога и системы."""

import re
import unittest

from test_app import m
from test_tech import TechBase


def without_tech_parts(html):
    """Только тело статистики: от первой плитки до блока «Система» (если он есть)."""
    body = html[html.index('<div class="stat-grid">'):]
    body = body.split("<h2>Система</h2>")[0].split("</main>")[0].strip()
    return re.sub(r'href="[^"]*"', 'href=""', body)  # ссылки ведут на свои страницы — сравниваем цифры


class BossStatsTests(TechBase):
    def setUp(self):
        super().setUp()
        self.done_request("brigada1", price="30000")
        self.done_request("brigada2", price="45 000")
        self.make_request()

    def test_boss_has_menu_with_statistics(self):
        html = self.client("rukovoditel").get("/ofis").text
        self.assertIn('href="/ofis/statistika"', html)
        self.assertIn(">Статистика</a>", html)

    def test_boss_sees_full_statistics(self):
        html = self.client("rukovoditel").get("/ofis/statistika?p=all").text
        for text in ("Создано заявок", "Бригады", "Офис", "Оплата выполненных", "По месяцам",
                     "Бригада 1", "Бригада 2", "Менеджер 1", "Наличными",
                     m.format_month(m.now_local().strftime("%Y-%m"))):
            self.assertIn(text, html)
        self.assertIn("75 000", html)  # 30 000 + 45 000

    def test_same_numbers_as_tech_section(self):
        for p in ("today", "week", "all"):
            boss = self.client("rukovoditel").get(f"/ofis/statistika?p={p}").text
            tech = self.tech_client().get(f"/tech/?p={p}").text
            self.assertEqual(without_tech_parts(boss), without_tech_parts(tech), p)

    def test_no_journal_log_or_system_for_boss(self):
        html = self.client("rukovoditel").get("/ofis/statistika").text
        for text in ("Журнал действий", "Лог приложения", "Система", "Python", "/tech"):
            self.assertNotIn(text, html)

    def test_only_boss_can_open(self):
        for uid in ("manager1", "manager2", "brigada1", "brigada2"):
            with self.subTest(user=uid):
                c = self.client(uid)
                self.assertEqual(c.get("/ofis/statistika").status_code, 302)
                self.assertNotIn("/ofis/statistika", c.get("/", follow_redirects=True).text)
        self.assertIn("/vhod", m.app.test_client().get("/ofis/statistika").location)

    def test_menu_marks_current_page(self):
        c = self.client("rukovoditel")
        stats_html = c.get("/ofis/statistika").text
        self.assertRegex(stats_html, r'class="active">Статистика</a>')
        list_html = c.get("/ofis").text
        self.assertRegex(list_html, r'class="active">Заявки</a>')

    def test_empty_database(self):
        with m.app.app_context():
            db = m.get_db()
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM requests")
            db.commit()
        html = self.client("rukovoditel").get("/ofis/statistika?p=all").text
        self.assertIn("выполненных заявок нет", html)
        self.assertIn("Заявок пока нет", html)
        self.assertTrue(re.search(r"Создано заявок</span><span class=\"stat-value\">0<", html))


if __name__ == "__main__":
    unittest.main()
