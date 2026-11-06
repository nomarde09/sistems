"""Автопроверки системы заявок. Запуск: .venv/Scripts/python -m unittest -v"""

import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Каждый запуск — с чистой временной базой, рабочая база не трогается.
_tmp = tempfile.TemporaryDirectory()
os.environ["DATABASE_PATH"] = str(Path(_tmp.name) / "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as m  # noqa: E402

OFFICE = [u for u, v in m.USERS.items() if v["role"] in ("boss", "office")]
BRIGADES = [u for u, v in m.USERS.items() if v["role"] == "brigade"]

GOOD_REQUEST = {
    "client_name": "Иван Петров",
    "phone": "+7 900 123-45-67",
    "address": "ул. Садовая, 5",
    "work_type": "4 камеры по периметру",
    "agreed_day": "20", "agreed_month": "2026-11",
    "urgent": "1",
    "description": "Поставить 4 камеры\nвокруг дома",
}


def csrf_from(html):
    found = re.search(r'name="csrf" value="([^"]+)"', html)
    assert found, "на странице нет скрытого ключа формы"
    return found.group(1)


class Base(unittest.TestCase):
    def setUp(self):
        with m.app.app_context():
            db = m.get_db()
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM requests")
            db.commit()

    def client(self, user_id=None):
        c = m.app.test_client()
        if user_id:
            token = csrf_from(c.get("/vhod").text)
            pin = os.environ[m.pin_env_name(user_id)]
            r = c.post("/vhod", data={"csrf": token, "user_id": user_id, "pin": pin})
            self.assertEqual(r.status_code, 302, f"не удалось войти как {user_id}")
        return c

    def post(self, c, url, data, page=None):
        token = csrf_from(c.get(page or url, follow_redirects=True).text)
        return c.post(url, data={"csrf": token, **data})


class LoginTests(Base):
    def test_anonymous_goes_to_login(self):
        c = m.app.test_client()
        for url in ("/", "/ofis", "/ofis/novaya", "/brigada", "/zayavka/1"):
            r = c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/vhod", r.location, url)

    def test_login_page_lists_everyone(self):
        html = m.app.test_client().get("/vhod").text
        for u in m.USERS.values():
            self.assertIn(u["name"], html)

    def test_each_user_logs_in_and_out(self):
        for uid, u in m.USERS.items():
            with self.subTest(user=u["name"]):
                c = self.client(uid)
                home = "Свободные заявки" if u["role"] == "brigade" else "Все заявки"
                page = c.get("/", follow_redirects=True).text
                self.assertIn(home, page)
                self.assertIn(u["name"], page)
                other = "/ofis" if u["role"] == "brigade" else "/brigada"
                self.assertEqual(c.get(other).status_code, 302)
                self.post(c, "/vyhod", {}, page="/")
                self.assertEqual(c.get("/").status_code, 302)

    def test_wrong_pin_rejected(self):
        c = m.app.test_client()
        for uid in m.USERS:
            pin = os.environ[m.pin_env_name(uid)]
            bad = "0000" if pin != "0000" else "1111"
            r = self.post(c, "/vhod", {"user_id": uid, "pin": bad})
            self.assertIn("Неверный пароль", r.text)

    def test_unknown_user_and_empty_choice(self):
        c = m.app.test_client()
        self.assertIn("Неверный", self.post(c, "/vhod", {"user_id": "hacker", "pin": ""}).text)
        self.assertIn("Выберите", self.post(c, "/vhod", {"pin": "1234"}).text)

    def test_form_without_csrf_refused(self):
        c = m.app.test_client()
        uid = OFFICE[0]
        r = c.post("/vhod", data={"user_id": uid, "pin": os.environ[m.pin_env_name(uid)]})
        self.assertEqual(r.status_code, 400)


class CreateRequestTests(Base):
    def test_office_creates_request(self):
        for uid in OFFICE:
            with self.subTest(user=uid):
                c = self.client(uid)
                r = self.post(c, "/ofis/novaya", GOOD_REQUEST)
                self.assertEqual(r.status_code, 302)
                page = c.get(r.location).text
                self.assertIn("Заявка создана", page)
                self.assertIn("Иван Петров", page)
                self.assertIn("Новая", page)
                self.assertIn("Срочно", page)
                self.assertIn("20 ноября", page)
                self.assertIn(m.USERS[uid]["name"], page)

        with m.app.app_context():
            rows = m.get_db().execute("SELECT * FROM requests").fetchall()
        self.assertEqual(len(rows), len(OFFICE))
        self.assertEqual({r["created_by"] for r in rows}, set(OFFICE))
        self.assertTrue(all(r["status"] == "new" for r in rows))

    def test_list_shows_requests_and_author(self):
        c = self.client("manager2")
        self.post(c, "/ofis/novaya", GOOD_REQUEST)
        self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "client_name": "ООО Ромашка",
                                      "work_type": "Ворота откатные", "urgent": ""})
        page = self.client("rukovoditel").get("/ofis").text
        self.assertIn("Иван Петров", page)
        self.assertIn("ООО Ромашка", page)
        self.assertIn("Ворота откатные", page)
        self.assertIn("Создал: Менеджер 2", page)

    def test_empty_form_shows_errors_and_saves_nothing(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {})
        self.assertEqual(r.status_code, 200)
        for text in ("Укажите имя клиента", "Укажите телефон", "Укажите адрес",
                     "Напишите, что монтируем", "Опишите"):
            self.assertIn(text, r.text)
        with m.app.app_context():
            self.assertEqual(m.get_db().execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_filled_fields_kept_after_error(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "address": ""})
        self.assertIn("Укажите адрес", r.text)
        self.assertIn("Иван Петров", r.text)

    def test_date_is_optional(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "agreed_day": ""})
        self.assertEqual(r.status_code, 302)
        self.assertIn("не указано", c.get(r.location).text)

    def test_brigade_cannot_create_or_open_office_pages(self):
        c = self.client(BRIGADES[0])
        self.assertEqual(c.get("/ofis/novaya").status_code, 302)
        token = csrf_from(c.get("/brigada").text)
        r = c.post("/ofis/novaya", data={"csrf": token, **GOOD_REQUEST})
        self.assertEqual(r.status_code, 302)
        with m.app.app_context():
            self.assertEqual(m.get_db().execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_html_in_fields_is_not_executed(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "client_name": "<script>alert(1)</script>"})
        page = c.get(r.location).text
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_work_type_is_free_text(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "work_type": "Домофон и 2 камеры у подъезда"})
        self.assertIn("Домофон и 2 камеры у подъезда", c.get(r.location).text)
        self.assertIn("Напишите, что монтируем",
                      self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "work_type": "   "}).text)

    def test_old_choice_values_become_text(self):
        with m.app.app_context():
            db = m.get_db()
            db.execute("""INSERT INTO requests (client_name, phone, address, work_type, description,
                          created_by, created_at) VALUES ('А', '12345', 'Б', 'both', 'В', 'manager1',
                          '2026-11-06T10:00:00')""")
            db.commit()
        m.init_db()
        with m.app.app_context():
            self.assertEqual(m.get_db().execute("SELECT work_type FROM requests").fetchone()[0],
                             "Камеры и ворота")

    def test_no_such_day_in_month(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "agreed_day": "31", "agreed_month": "2026-11"})
        self.assertIn("нет 31-го числа", r.text)
        self.assertIn('value="31" selected', r.text)

    def test_month_list_starts_now_and_year_is_automatic(self):
        with m.app.test_request_context():
            options = m.month_options()
        today = m.now_local().date()
        self.assertEqual(len(options), 12)
        self.assertEqual(options[0], (f"{today.year}-{today.month:02d}", m.MONTHS[today.month - 1]))
        self.assertTrue(all(str(today.year) not in label for value, label in options
                            if value.startswith(str(today.year))))
        self.assertTrue(all(str(today.year + 1) in label for value, label in options
                            if value.startswith(str(today.year + 1))))

    def test_next_year_date_saved_and_shown_with_year(self):
        c = self.client("manager1")
        year = m.now_local().year + 1
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "agreed_day": "15", "agreed_month": f"{year}-01"})
        self.assertIn(f"15 января {year}", c.get(r.location).text)
        with m.app.app_context():
            self.assertEqual(m.get_db().execute("SELECT agreed_date FROM requests").fetchone()[0],
                             f"{year}-01-15")

    def test_form_has_quick_date_buttons_and_current_month(self):
        c = self.client("manager1")
        html = c.get("/ofis/novaya").text
        today = m.now_local().date()
        for text in ("Сегодня", "Завтра", "Послезавтра", "Не знаем"):
            self.assertIn(text, html)
        self.assertIn(f'value="{today.year}-{today.month:02d}" selected', html)
        self.assertIn(f'data-day="{today.day}"', html)

    def test_missing_request_is_404(self):
        self.assertEqual(self.client("manager1").get("/zayavka/999").status_code, 404)


class BrigadeTests(Base):
    def make_request(self, **fields):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, **fields})
        return int(r.location.split("/zayavka/")[1].split("?")[0])

    def take(self, c, rid):
        return self.post(c, f"/brigada/zayavka/{rid}/vzyat", {}, page="/brigada")

    def give_back(self, c, rid):
        return self.post(c, f"/brigada/zayavka/{rid}/vernut", {}, page="/brigada/moi")

    def row(self, rid):
        with m.app.app_context():
            return m.get_db().execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()

    def actions(self, rid):
        with m.app.app_context():
            return [(e["action"], e["user_id"]) for e in m.get_db().execute(
                "SELECT * FROM events WHERE request_id = ? ORDER BY id", (rid,))]

    def test_free_list_shows_new_requests_with_take_button(self):
        rid = self.make_request(address="ул. Лесная, 1")
        html = self.client("brigada1").get("/brigada").text
        self.assertIn("ул. Лесная, 1", html)
        self.assertIn(f"/brigada/zayavka/{rid}/vzyat", html)
        self.assertIn("Взять", html)

    def test_take_moves_request_to_mine_and_hides_from_other_brigade(self):
        rid = self.make_request(address="ул. Лесная, 1")
        b1, b2 = self.client("brigada1"), self.client("brigada2")
        r = self.take(b1, rid)
        self.assertEqual(r.status_code, 302)
        row = self.row(rid)
        self.assertEqual((row["status"], row["taken_by"]), ("in_work", "brigada1"))
        self.assertTrue(row["taken_at"])
        self.assertIn("ул. Лесная, 1", b1.get("/brigada/moi").text)
        self.assertNotIn("ул. Лесная, 1", b1.get("/brigada").text)
        self.assertNotIn("ул. Лесная, 1", b2.get("/brigada").text)
        self.assertNotIn("ул. Лесная, 1", b2.get("/brigada/moi").text)
        # чужую заявку в работе вторая бригада не открывает
        self.assertEqual(b2.get(f"/brigada/zayavka/{rid}").status_code, 302)
        office = self.client("rukovoditel").get(f"/zayavka/{rid}").text
        self.assertIn("В работе", office)
        self.assertIn("Бригада 1", office)

    def test_second_brigade_cannot_take_taken_request(self):
        rid = self.make_request()
        b1, b2 = self.client("brigada1"), self.client("brigada2")
        self.take(b1, rid)
        r = self.take(b2, rid)
        self.assertIn("только что взяла другая бригада", b2.get(r.location).text)
        self.assertEqual(self.row(rid)["taken_by"], "brigada1")
        self.assertEqual([a for a, _ in self.actions(rid)], ["created", "taken"])

    def test_simultaneous_take_gives_request_to_exactly_one_brigade(self):
        for _ in range(15):
            rid = self.make_request()
            clients = {b: self.client(b) for b in BRIGADES}
            tokens = {b: csrf_from(c.get("/brigada").text) for b, c in clients.items()}
            start = threading.Barrier(len(clients))
            results = {}

            def press(b):
                start.wait()
                results[b] = clients[b].post(f"/brigada/zayavka/{rid}/vzyat",
                                             data={"csrf": tokens[b]}).location

            threads = [threading.Thread(target=press, args=(b,)) for b in clients]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            winners = [b for b, loc in results.items() if f"/brigada/zayavka/{rid}" in loc]
            self.assertEqual(len(winners), 1, results)
            self.assertEqual(self.row(rid)["taken_by"], winners[0])
            self.assertEqual(sum(a == "taken" for a, _ in self.actions(rid)), 1)

    def test_return_puts_request_back_for_everyone(self):
        rid = self.make_request(address="ул. Лесная, 1")
        b1, b2 = self.client("brigada1"), self.client("brigada2")
        self.take(b1, rid)
        r = self.give_back(b1, rid)
        self.assertIn("возвращена в свободные", b1.get(r.location).text)
        row = self.row(rid)
        self.assertEqual((row["status"], row["taken_by"], row["taken_at"]), ("new", None, None))
        self.assertIn("ул. Лесная, 1", b2.get("/brigada").text)
        self.take(b2, rid)
        self.assertEqual(self.row(rid)["taken_by"], "brigada2")
        self.assertEqual(self.actions(rid), [("created", "manager1"), ("taken", "brigada1"),
                                             ("returned", "brigada1"), ("taken", "brigada2")])
        history = self.client("rukovoditel").get(f"/zayavka/{rid}").text
        self.assertIn("вернула в свободные", history)

    def test_cannot_return_someone_elses_request(self):
        rid = self.make_request()
        b1, b2 = self.client("brigada1"), self.client("brigada2")
        self.take(b1, rid)
        self.give_back(b2, rid)
        self.assertEqual(self.row(rid)["taken_by"], "brigada1")
        self.give_back(b1, self.make_request())  # свободную тоже «вернуть» нельзя
        self.assertEqual(self.row(rid)["status"], "in_work")

    def test_free_list_order_urgent_then_nearest_day(self):
        late = self.make_request(address="Поздний", urgent="", agreed_day="28", agreed_month="2026-11")
        nodate = self.make_request(address="Без дня", urgent="", agreed_day="")
        soon = self.make_request(address="Ранний", urgent="", agreed_day="10", agreed_month="2026-11")
        urgent = self.make_request(address="Срочный", urgent="1", agreed_day="")
        html = self.client("brigada1").get("/brigada").text
        order = [html.index(a) for a in ("Срочный", "Ранний", "Поздний", "Без дня")]
        self.assertEqual(order, sorted(order))

    def test_counters_in_tabs(self):
        self.make_request(); rid = self.make_request()
        b1 = self.client("brigada1")
        self.take(b1, rid)
        html = b1.get("/brigada").text
        self.assertRegex(html, r'Свободные <span class="count">1</span>')
        self.assertRegex(html, r'Мои заявки <span class="count">1</span>')

    def test_office_cannot_use_brigade_actions(self):
        rid = self.make_request()
        c = self.client("manager1")
        self.post(c, f"/brigada/zayavka/{rid}/vzyat", {}, page="/ofis")
        self.assertEqual(self.row(rid)["status"], "new")


if __name__ == "__main__":
    unittest.main()
