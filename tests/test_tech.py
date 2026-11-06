"""Технический раздел суперадмина: изоляция, вход из окружения, скрытность, только чтение."""

import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import tech
from test_app import GOOD_REQUEST, csrf_from, m
from test_boss import BossHelpers

LOGIN, PASSWORD = os.environ["SUPERADMIN_LOGIN"], os.environ["SUPERADMIN_PASSWORD"]
TECH_PAGES = ("/tech/", "/tech/zhurnal", "/tech/log")


class TechBase(BossHelpers):
    def setUp(self):
        super().setUp()
        tech._failed_attempts.clear()

    def tech_login(self, c=None, login=LOGIN, password=PASSWORD):
        c = c or m.app.test_client()
        token = csrf_from(c.get("/tech/vhod").text)
        r = c.post("/tech/vhod", data={"csrf": token, "login": login, "password": password})
        return c, r

    def tech_client(self):
        c, r = self.tech_login()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.location.endswith("/tech/"))
        return c


class AccessTests(TechBase):
    def test_login_with_env_credentials(self):
        c = self.tech_client()
        html = c.get("/tech/").text
        self.assertIn("Сквозная статистика", html)
        self.assertIn("только чтение", html)

    def test_wrong_credentials_rejected(self):
        for login, password in ((LOGIN, "wrong-password-xx"), ("admin", PASSWORD), ("", "")):
            with self.subTest(login=login):
                c, r = self.tech_login(login=login, password=password)
                self.assertIn("Неверный логин или пароль", r.text)
                self.assertEqual(c.get("/tech/").status_code, 302)

    def test_pages_closed_without_tech_login(self):
        c = m.app.test_client()
        for path in TECH_PAGES:
            r = c.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/tech/vhod", r.location, path)

    def test_employee_sessions_do_not_open_tech(self):
        for uid in m.USERS:
            with self.subTest(user=uid):
                c = self.client(uid)
                for path in TECH_PAGES:
                    self.assertIn("/tech/vhod", c.get(path).location, path)

    def test_employee_pin_does_not_work_for_tech(self):
        pin = os.environ[m.pin_env_name("rukovoditel")]
        _, r = self.tech_login(login="rukovoditel", password=pin)
        self.assertIn("Неверный логин или пароль", r.text)

    def test_tech_session_does_not_open_employee_pages(self):
        c = self.tech_client()
        for path in ("/ofis", "/ofis/novaya", "/brigada", "/brigada/moi"):
            r = c.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/vhod", r.location, path)

    def test_superadmin_cannot_login_through_employee_form(self):
        c = m.app.test_client()
        token = csrf_from(c.get("/vhod").text)
        r = c.post("/vhod", data={"csrf": token, "login": LOGIN, "password": PASSWORD})
        self.assertIn("Неверный", r.text)

    def test_disabled_without_env(self):
        for env in ({"SUPERADMIN_LOGIN": ""}, {"SUPERADMIN_PASSWORD": ""},
                    {"SUPERADMIN_PASSWORD": "short-pw"}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                c = m.app.test_client()
                for path in ("/tech/vhod", *TECH_PAGES):
                    self.assertEqual(c.get(path).status_code, 404, path)

    def test_brute_force_lockout(self):
        c = m.app.test_client()
        for _ in range(tech.MAX_FAILS):
            self.tech_login(c, password="wrong-password-xx")
        _, r = self.tech_login(c)  # даже верный пароль сейчас не пускает
        self.assertIn("Слишком много попыток", r.text)
        self.assertEqual(c.get("/tech/").status_code, 302)

    def test_session_expires_after_8_hours(self):
        c = self.tech_client()
        with c.session_transaction() as s:
            s["tech_since"] = (m.now_local() - timedelta(hours=9)).isoformat()
        self.assertIn("/tech/vhod", c.get("/tech/").location)

    def test_logout(self):
        c = self.tech_client()
        token = csrf_from(c.get("/tech/").text)
        c.post("/tech/vyhod", data={"csrf": token})
        self.assertEqual(c.get("/tech/").status_code, 302)

    def test_login_form_requires_csrf(self):
        c = m.app.test_client()
        r = c.post("/tech/vhod", data={"login": LOGIN, "password": PASSWORD})
        self.assertEqual(r.status_code, 400)

    def test_pages_not_cached(self):
        r = self.tech_client().get("/tech/")
        self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.assertIn("noindex", r.headers["X-Robots-Tag"])


class HiddenTests(TechBase):
    def test_not_shown_anywhere_for_employees(self):
        rid = self.make_request()
        self.tech_client().get(f"/tech/zayavka/{rid}")  # суперадмин что-то смотрел
        pages = {"/vhod": m.app.test_client()}
        for uid, path in (("rukovoditel", "/ofis?status=all"), ("rukovoditel", f"/zayavka/{rid}"),
                          ("manager1", "/ofis/novaya"), ("brigada1", "/brigada"),
                          ("brigada1", f"/brigada/zayavka/{rid}")):
            html = self.client(uid).get(path).text
            for secret in (LOGIN, "/tech", "Технический", "суперадмин"):
                self.assertNotIn(secret, html, f"{uid} {path}: {secret}")
        login_html = pages["/vhod"].get("/vhod").text
        self.assertNotIn("/tech", login_html)
        self.assertNotIn(LOGIN, login_html)

    def test_employee_header_not_shown_in_tech_section(self):
        c = self.client("rukovoditel")
        html = c.get("/tech/vhod").text
        self.assertIn("Технический вход", html)
        self.assertNotIn("Руководитель", html)

    def test_superadmin_is_not_a_company_user(self):
        self.assertNotIn(LOGIN, m.USERS)
        self.assertNotIn("superadmin", {u["role"] for u in m.USERS.values()})

    def test_tech_reading_leaves_no_trace_in_request_history(self):
        rid = self.make_request()
        before = self.actions(rid)
        self.tech_client().get(f"/tech/zayavka/{rid}")
        self.assertEqual(self.actions(rid), before)


class ReadOnlyTests(TechBase):
    def test_stats_cover_whole_company(self):
        rid = self.done_request("brigada2", price="40000")
        self.make_request()
        html = self.tech_client().get("/tech/?p=all").text
        self.assertIn("Создано заявок", html)
        for name in ("Бригада 1", "Бригада 2", "Руководитель", "Менеджер 1", "Менеджер 2", "Наличными"):
            self.assertIn(name, html)
        self.assertIn("40 000", html)
        self.assertIn(m.format_month(m.now_local().strftime("%Y-%m")), html)

    def test_journal_lists_and_filters_events(self):
        rid = self.make_request(client_name="Клиент журнала")
        self.take(self.client("brigada1"), rid)
        c = self.tech_client()
        html = c.get("/tech/zhurnal").text
        self.assertIn("Клиент журнала", html)
        self.assertIn("Взял(а) в работу", html)
        only_brigade = c.get("/tech/zhurnal", query_string={"user": "brigada1"}).text
        self.assertNotIn("Создал(а) заявку</td>", only_brigade)
        by_id = c.get("/tech/zhurnal", query_string={"request_id": str(rid)}).text
        self.assertIn(f"№{rid}", by_id)

    def test_request_card_shows_all_fields(self):
        rid = self.done_request("brigada1")
        html = self.tech_client().get(f"/tech/zayavka/{rid}").text
        for field in ("client_name", "phone", "report_price", "report_payment", "taken_by"):
            self.assertIn(field, html)
        self.assertEqual(self.tech_client().get("/tech/zayavka/99999").status_code, 404)

    def test_app_log_records_requests_and_logins(self):
        self.client("manager2").get("/ofis")
        m.app.test_client().post("/vhod")  # без ключа формы → 400, тоже в логе
        c = self.tech_client()
        html = c.get("/tech/log").text
        self.assertIn("ВХОД успешно: manager2", html)
        self.assertIn("manager2 GET /ofis 200", html)
        self.assertIn("ТЕХ-ВХОД успешно", html)
        logins = c.get("/tech/log", query_string={"only": "logins"}).text
        self.assertNotIn("GET /ofis 200", logins)

    def test_log_never_contains_passwords(self):
        self.client("rukovoditel")
        self.tech_login(password="wrong-password-xx")
        self.tech_client()
        text = m.LOG_FILE.read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("wrong-password-xx", text)
        self.assertNotIn(os.environ[m.pin_env_name("rukovoditel")] + " ", text)

    def test_no_write_routes_in_tech_section(self):
        writable = [rule.rule for rule in m.app.url_map.iter_rules()
                    if rule.rule.startswith("/tech") and "POST" in rule.methods]
        self.assertEqual(sorted(writable), ["/tech/vhod", "/tech/vyhod"])

    def test_tech_cannot_change_requests_via_employee_routes(self):
        rid = self.make_request()
        c = self.tech_client()
        token = csrf_from(c.get("/tech/").text)
        for url in (f"/zayavka/{rid}/otmenit", f"/brigada/zayavka/{rid}/vzyat",
                    f"/zayavka/{rid}/ispravit"):
            c.post(url, data={"csrf": token, **GOOD_REQUEST, "address": "Изменено"})
        row = self.row(rid)
        self.assertEqual((row["status"], row["address"]), ("new", GOOD_REQUEST["address"]))


if __name__ == "__main__":
    unittest.main()
