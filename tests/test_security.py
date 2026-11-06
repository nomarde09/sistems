"""Проверка безопасности: права ролей, подделка форм, лог, заголовки, размер форм, перебор."""

import os
import re
import unittest
from unittest import mock

from test_app import GOOD_REQUEST, csrf_from, m
from test_tech import TechBase


class AccessControlTests(TechBase):
    """Бригада и менеджер пытаются сделать то, что им нельзя."""

    def setUp(self):
        super().setUp()
        self.target = self.make_request(client_name="Цель атаки")
        self.take(self.client("brigada2"), self.target)        # заявка в работе у Бригады 2
        self.done_other = self.done_request("brigada2")        # выполненная Бригадой 2

    def snapshot(self, rid):
        row = dict(self.row(rid))
        row.pop("updated_at", None)
        return row

    def test_brigade_cannot_open_other_brigades_requests(self):
        b1 = self.client("brigada1")
        for rid in (self.target, self.done_other):
            self.assertEqual(b1.get(f"/brigada/zayavka/{rid}").status_code, 302)
            self.assertEqual(b1.get(f"/brigada/zayavka/{rid}/otchet").status_code, 302)
        self.assertNotIn("Цель атаки", b1.get("/brigada").text + b1.get("/brigada/moi").text)

    def test_brigade_cannot_open_management_pages(self):
        b1 = self.client("brigada1")
        for path in ("/ofis", "/ofis/novaya", "/ofis/statistika", "/ofis?status=all&q=Цель",
                     f"/zayavka/{self.target}", f"/zayavka/{self.target}/ispravit",
                     "/tech/", "/tech/zhurnal", "/tech/log", f"/tech/zayavka/{self.target}"):
            with self.subTest(path=path):
                self.assertIn(b1.get(path).status_code, (302, 404))

    def test_brigade_cannot_change_other_or_management_actions(self):
        b1 = self.client("brigada1")
        token = csrf_from(b1.get("/brigada").text)
        before = self.snapshot(self.target)
        for url, data in ((f"/brigada/zayavka/{self.target}/vzyat", {}),
                          (f"/brigada/zayavka/{self.target}/vernut", {}),
                          (f"/brigada/zayavka/{self.target}/otchet",
                           {"report_work": "взлом", "report_price": "1", "report_payment": "cash"}),
                          (f"/zayavka/{self.target}/otmenit", {"reason": "взлом"}),
                          (f"/zayavka/{self.target}/garantiya", {"warranty": "1"}),
                          (f"/zayavka/{self.target}/ispravit", {**GOOD_REQUEST, "client_name": "ВЗЛОМ"}),
                          ("/ofis/novaya", {**GOOD_REQUEST, "client_name": "ВЗЛОМ"})):
            b1.post(url, data={"csrf": token, **data})
        self.assertEqual(self.snapshot(self.target), before)
        with m.app.app_context():
            self.assertEqual(m.get_db().execute(
                "SELECT COUNT(*) FROM requests WHERE client_name = 'ВЗЛОМ'").fetchone()[0], 0)

    def test_manager_cannot_use_boss_or_brigade_actions(self):
        mg = self.client("manager2")
        token = csrf_from(mg.get("/ofis").text)
        self.assertEqual(mg.get("/ofis/statistika").status_code, 302)
        self.assertEqual(mg.get(f"/zayavka/{self.target}/ispravit").status_code, 302)
        before = self.snapshot(self.target)
        mg.post(f"/zayavka/{self.target}/otmenit", data={"csrf": token})
        mg.post(f"/brigada/zayavka/{self.target}/vernut", data={"csrf": token})
        self.assertEqual(self.snapshot(self.target), before)

    def test_forged_session_cookie_rejected(self):
        c = m.app.test_client()
        c.set_cookie("session", "eyJ1c2VyX2lkIjoicnVrb3ZvZGl0ZWwifQ.fake.signature")
        self.assertEqual(c.get("/ofis").status_code, 302)


class FormAndLogTests(TechBase):
    def test_csrf_with_non_latin_letters_is_400_not_crash(self):
        c = m.app.test_client()
        c.get("/vhod")
        r = c.post("/vhod", data={"csrf": "ключ-по-русски", "login": "x", "password": "y"})
        self.assertEqual(r.status_code, 400)
        b1 = self.client("brigada1")
        self.assertEqual(b1.post("/brigada/zayavka/1/vernut", data={"csrf": "чужой"}).status_code, 400)

    def test_csrf_missing_in_session_rejected(self):
        c = m.app.test_client()  # ни разу не открывал сайт — ключа в сессии нет
        self.assertEqual(c.post("/vhod", data={"csrf": "", "login": "x", "password": "y"}).status_code, 400)

    def test_log_cannot_be_forged_through_url(self):
        fake = "2026-01-01 00:00:00 WARNING ТЕХ-ВХОД успешно с 6.6.6.6"
        m.app.test_client().get("/abc%0A" + fake.replace(" ", "%20"))
        log_text = m.LOG_FILE.read_text(encoding="utf-8")
        self.assertNotIn("\n" + fake, log_text)
        self.assertIn("\\x0a" + fake, log_text)

    def test_very_long_url_is_shortened_in_log(self):
        m.app.test_client().get("/" + "a" * 2000)
        last = m.LOG_FILE.read_text(encoding="utf-8").splitlines()[-1]
        self.assertLess(len(last), 500)

    def test_security_headers_on_every_page(self):
        c = self.client("brigada1")
        for path in ("/vhod", "/brigada", "/static/style.css", "/tech/vhod", "/nesushchestvuet"):
            with self.subTest(path=path):
                h = c.get(path).headers
                self.assertEqual(h["X-Frame-Options"], "DENY")
                self.assertEqual(h["X-Content-Type-Options"], "nosniff")
                self.assertEqual(h["Referrer-Policy"], "same-origin")
                self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
                self.assertIn("geolocation=(self)", h["Permissions-Policy"])

    def test_huge_form_rejected(self):
        c = m.app.test_client()
        token = csrf_from(c.get("/vhod").text)
        r = c.post("/vhod", data={"csrf": token, "login": "x" * 1_000_000, "password": "y"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("Слишком много текста", r.text)

    def test_failed_login_memory_is_bounded(self):
        c = m.app.test_client()
        token = csrf_from(c.get("/vhod").text)
        with mock.patch.object(m.time, "monotonic", side_effect=lambda: 1_000_000.0):
            for i in range(30):
                c.post("/vhod", data={"csrf": token, "login": f"bot{i}", "password": "y"})
        # 15 минут спустя все эти записи устарели и чистятся при следующей проверке
        m._login_fails.update({(f"ip{i}", "x"): [1_000_000.0] for i in range(2100)})
        with mock.patch.object(m.time, "monotonic", side_effect=lambda: 1_000_000.0 + 16 * 60):
            m._fails(("1.1.1.1", "kto-to"))
        self.assertLess(len(m._login_fails), 10)

    def test_cookie_flags(self):
        self.assertTrue(m.app.config["SESSION_COOKIE_HTTPONLY"])  # скрипты не читают cookie входа
        self.assertEqual(m.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertFalse(m.app.config["SESSION_COOKIE_SECURE"])  # локально — без https (на хостинге COOKIE_SECURE=1)

    def test_no_secrets_in_pages(self):
        pages = [self.client(uid).get(p).text for uid, p in
                 (("rukovoditel", "/ofis?status=all"), ("rukovoditel", "/ofis/statistika"), ("brigada1", "/brigada"))]
        for text in pages:
            self.assertNotIn(os.environ["SECRET_KEY"], text)
            self.assertNotIn(os.environ["SUPERADMIN_PASSWORD"], text)
            self.assertIsNone(re.search(r'value="\d{4}"\s+name="password"', text))


if __name__ == "__main__":
    unittest.main()
