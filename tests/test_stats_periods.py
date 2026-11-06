"""Статистика по периодам (сегодня, вчера, позавчера, 7 дней, месяц, день) и с именами людей."""

import os
import re
import unittest
from datetime import timedelta
from unittest import mock

import stats
from test_app import m
from test_tech import TechBase


class PeriodTests(TechBase):
    def shift(self, rid, days):
        """Переносит все отметки времени заявки и её журнала на days дней назад."""
        with m.app.app_context():
            db = m.get_db()
            row = db.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
            updates = {}
            for col in ("created_at", "taken_at", "done_at", "cancelled_at"):
                if row[col]:
                    updates[col] = (m.datetime.fromisoformat(row[col]) - timedelta(days=days)).isoformat()
            if updates:
                db.execute("UPDATE requests SET " + ", ".join(f"{c} = ?" for c in updates) + " WHERE id = ?",
                           (*updates.values(), rid))
            for e in db.execute("SELECT id, at FROM events WHERE request_id = ?", (rid,)).fetchall():
                db.execute("UPDATE events SET at = ? WHERE id = ?",
                           ((m.datetime.fromisoformat(e["at"]) - timedelta(days=days)).isoformat(), e["id"]))
            db.commit()

    def value(self, html, label):
        found = re.search(re.escape(label) + r'</span><span class="stat-value[^"]*">([^<]+)<', html)
        return found.group(1) if found else None

    def setUp(self):
        super().setUp()
        self.today_done = self.done_request("brigada1", price="10000")             # сегодня
        self.yesterday_done = self.done_request("brigada2", price="20000")         # вчера
        self.shift(self.yesterday_done, 1)
        self.old_done = self.done_request("brigada1", price="40000")               # 2 дня назад
        self.shift(self.old_done, 2)
        self.week_ago = self.done_request("brigada2", price="80000")              # 10 дней назад
        self.shift(self.week_ago, 10)
        self.boss = self.client("rukovoditel")

    def page(self, **params):
        return self.boss.get("/ofis/statistika", query_string=params).text

    def test_period_buttons_present(self):
        html = self.page()
        for label in ("Сегодня", "Вчера", "Позавчера", "7 дней", "Месяц", "Всё время", "Конкретный день"):
            self.assertIn(label, html)

    def test_default_is_today(self):
        html = self.page()
        self.assertIn("Показано: <strong>сегодня</strong>", html)
        self.assertEqual(self.value(html, "Выполнено"), "1")
        self.assertEqual(self.value(html, "Сумма выполненных"), "10 000")

    def test_yesterday_and_day_before(self):
        self.assertEqual(self.value(self.page(p="yesterday"), "Сумма выполненных"), "20 000")
        self.assertEqual(self.value(self.page(p="day_before"), "Сумма выполненных"), "40 000")

    def test_week_has_daily_table(self):
        html = self.page(p="week")
        self.assertEqual(self.value(html, "Выполнено"), "3")
        self.assertEqual(self.value(html, "Сумма выполненных"), "70 000")
        self.assertIn("<h2>По дням</h2>", html)
        today = m.now_local().date()
        for back in range(7):
            self.assertIn((today - timedelta(days=back)).strftime("%d.%m"), html)
        self.assertNotIn((today - timedelta(days=7)).strftime("%d.%m") + "</a>", html)
        self.assertNotIn("<h2>По месяцам</h2>", html)

    def test_specific_day(self):
        day = (m.now_local().date() - timedelta(days=10)).isoformat()
        html = self.page(p="day", d=day)
        self.assertEqual(self.value(html, "Сумма выполненных"), "80 000")
        self.assertIn(f'value="{day}"', html)
        self.assertNotIn("<h2>По дням</h2>", html)

    def test_bad_day_falls_back_to_today(self):
        self.assertIn("Показано: <strong>сегодня</strong>", self.page(p="day", d="вчера"))
        self.assertIn("Показано: <strong>сегодня</strong>", self.page(p="что-то"))

    def test_all_time_has_months(self):
        html = self.page(p="all")
        self.assertEqual(self.value(html, "Выполнено"), "4")
        self.assertIn("<h2>По месяцам</h2>", html)

    def test_daily_rows_link_to_that_day(self):
        html = self.page(p="week")
        day = (m.now_local().date() - timedelta(days=1)).isoformat()
        self.assertIn(f"p=day&amp;d={day}", html)

    def test_now_counters_ignore_period(self):
        rid = self.make_request()
        self.take(self.client("brigada1"), rid)
        for p in ("today", "day_before", "all"):
            self.assertEqual(self.value(self.page(p=p), "Сейчас в работе"), "1", p)

    def test_tech_section_has_same_periods(self):
        html = self.tech_client().get("/tech/", query_string={"p": "yesterday"}).text
        self.assertIn('href="/tech/?p=week"', html)
        self.assertEqual(self.value(html, "Сумма выполненных"), "20 000")

    def test_resolve_period(self):
        today = m.now_local().date()
        self.assertEqual(stats.resolve_period("week", "", today)[2:], (today - timedelta(days=6), today))
        self.assertEqual(stats.resolve_period("month", "", today)[2], today.replace(day=1))
        self.assertEqual(stats.resolve_period("all", "", today)[2:], (None, None))


class NamesInStatsTests(TechBase):
    NAMES = {"NAME_RUKOVODITEL": "Фарух", "NAME_MANAGER1": "Яна", "NAME_MANAGER2": "Умида",
             "NAME_BRIGADA1": "Иван", "NAME_BRIGADA2": "Жахонгир"}

    def test_people_names_instead_of_positions(self):
        self.done_request("brigada1", price="10000")
        with mock.patch.dict(os.environ, self.NAMES):
            for html in (self.client("rukovoditel").get("/ofis/statistika").text,
                         self.tech_client().get("/tech/").text):
                for name in self.NAMES.values():
                    self.assertIn(f"<td>{name}</td>", html)
                for position in ("Бригада 1", "Бригада 2", "Менеджер 1", "Менеджер 2"):
                    self.assertNotIn(f"<td>{position}</td>", html)
                self.assertRegex(html, r"<td>Иван</td><td>1</td><td>1</td>")

    def test_names_everywhere_on_site(self):
        with mock.patch.dict(os.environ, self.NAMES):
            done = self.done_request("brigada2", price="5000")       # создал менеджер 1, выполнил Жахонгир
            work = self.make_request(client_name="В работе у Ивана")
            self.take(self.client("brigada1"), work)

            boss = self.client("rukovoditel")
            filters = boss.get("/ofis?status=all").text
            for name in ("Иван", "Жахонгир", "Фарух", "Яна", "Умида"):
                self.assertRegex(filters, rf'<option value="[a-z0-9]+" *>{name}</option>')
            self.assertIn("Создал: Яна", filters)
            self.assertIn("В работе · Иван", filters)
            self.assertIn("Выполнил(а) Жахонгир", filters)

            card = boss.get(f"/zayavka/{done}").text
            self.assertIn("<strong>Яна</strong> — создал(а) заявку", card)
            self.assertIn("<strong>Жахонгир</strong> — выполнил(а)", card)

            brigade_card = self.client("brigada1").get(f"/brigada/zayavka/{work}").text
            self.assertIn("<dt>Заявку дал</dt><dd>Яна</dd>", brigade_card)

            tech = self.tech_client().get("/tech/zhurnal").text
            self.assertIn("<td>Иван</td>", tech)
            self.assertIn(">Умида</option>", tech)

            # фильтр по имени работает так же, как раньше по должности
            only_ivan = boss.get("/ofis", query_string={"status": "all", "brigada": "brigada1"}).text
            self.assertIn("В работе у Ивана", only_ivan)
            self.assertNotIn("Выполнил(а) Жахонгир", only_ivan)

    def test_positions_when_names_not_set(self):
        html = self.client("rukovoditel").get("/ofis/statistika").text
        self.assertIn("<td>Бригада 1</td>", html)


if __name__ == "__main__":
    unittest.main()
