"""Способ оплаты в отчёте и срочные заявки наверху."""

import unittest

from test_app import GOOD_REQUEST, m
from test_boss import BossHelpers

REPORT = {"report_work": "Сделали", "report_price": "50000", "report_comment": ""}


class PaymentTests(BossHelpers):
    def taken(self, brigade="brigada1", **fields):
        rid = self.make_request(**fields)
        c = self.client(brigade)
        self.take(c, rid)
        return rid, c

    def report(self, c, rid, **data):
        return self.post(c, f"/brigada/zayavka/{rid}/otchet", {**REPORT, **data}, page="/brigada/moi")

    def test_form_shows_three_payment_options(self):
        rid, c = self.taken()
        html = c.get(f"/brigada/zayavka/{rid}/otchet").text
        for label in ("Перевод на Kaspi", "Kaspi Pay", "Наличными"):
            self.assertIn(label, html)

    def test_each_payment_is_saved_and_shown(self):
        for key, label in m.PAYMENTS.items():
            with self.subTest(payment=label):
                rid, c = self.taken()
                self.report(c, rid, report_payment=key)
                self.assertEqual(self.row(rid)["report_payment"], key)
                self.assertIn(label, self.client("rukovoditel").get(f"/zayavka/{rid}").text)
                self.assertIn(label, c.get(f"/brigada/zayavka/{rid}").text)

    def test_payment_required_when_price_above_zero(self):
        rid, c = self.taken()
        r = self.report(c, rid)
        self.assertIn("Выберите, как заплатил клиент", r.text)
        self.assertEqual(self.row(rid)["status"], "in_work")
        r = self.report(c, rid, report_payment="bitcoin")
        self.assertIn("Выберите, как заплатил клиент", r.text)

    def test_zero_price_needs_no_payment(self):
        rid, c = self.taken()
        self.report(c, rid, report_price="0")
        row = self.row(rid)
        self.assertEqual((row["status"], row["report_payment"]), ("done", None))

    def test_choice_kept_after_error(self):
        rid, c = self.taken()
        r = self.report(c, rid, report_work="", report_payment="kaspi_pay")
        self.assertIn('value="kaspi_pay" checked', r.text)

    def test_boss_can_change_payment(self):
        rid, c = self.taken()
        self.report(c, rid, report_payment="cash")
        boss = self.client("rukovoditel")
        self.assertIn('value="cash" checked', boss.get(f"/zayavka/{rid}/ispravit").text)
        self.edit(boss, rid, {**GOOD_REQUEST, **REPORT, "report_payment": "kaspi_transfer"})
        self.assertEqual(self.row(rid)["report_payment"], "kaspi_transfer")
        self.assertIn("исправлено: оплата", boss.get(f"/zayavka/{rid}").text)

    def test_list_filter_and_totals_by_payment(self):
        for brigade, price, pay in (("brigada1", "10000", "cash"), ("brigada1", "20000", "kaspi_pay"),
                                    ("brigada2", "30000", "cash")):
            rid, c = self.taken(brigade)
            self.report(c, rid, report_price=price, report_payment=pay)
        office = self.client("manager1")
        html = office.get("/ofis?status=all").text
        self.assertIn("Наличными: <strong class=\"price\">40 000", html)
        self.assertIn("Kaspi Pay: <strong class=\"price\">20 000", html)
        self.assertIn("· Наличными", html)
        cash = office.get("/ofis", query_string={"oplata": "cash"}).text
        self.assertIn("Найдено: <strong>2</strong>", cash)
        self.assertIn("40 000", cash)
        self.assertNotIn("Kaspi Pay: <strong", cash)

    def test_old_report_without_payment_counted_as_unknown(self):
        rid, c = self.taken()
        self.report(c, rid, report_payment="cash")
        with m.app.app_context():
            db = m.get_db()
            db.execute("UPDATE requests SET report_payment = NULL WHERE id = ?", (rid,))
            db.commit()
        html = self.client("manager1").get("/ofis?status=done").text
        self.assertIn("не указана: <strong", html)


class UrgentFirstTests(BossHelpers):
    def test_urgent_open_requests_are_on_top_of_office_list(self):
        plain_new = self.make_request(client_name="Обычная новая", urgent="")
        urgent_in_work = self.make_request(client_name="Срочная в работе", urgent="1")
        self.take(self.client("brigada1"), urgent_in_work)
        urgent_new = self.make_request(client_name="Срочная новая", urgent="1")
        urgent_done = self.make_request(client_name="Срочная выполненная", urgent="1")
        b2 = self.client("brigada2")
        self.take(b2, urgent_done)
        self.post(b2, f"/brigada/zayavka/{urgent_done}/otchet",
                  {**REPORT, "report_payment": "cash"}, page="/brigada/moi")

        html = self.client("rukovoditel").get("/ofis?status=all").text
        order = sorted(("Срочная новая", "Срочная в работе", "Обычная новая", "Срочная выполненная"),
                       key=html.index)
        self.assertEqual(order, ["Срочная новая", "Срочная в работе", "Обычная новая", "Срочная выполненная"])

    def test_urgent_first_for_brigade_lists(self):
        self.make_request(address="Обычный адрес", urgent="", agreed_day="7", agreed_month="2026-11")
        self.make_request(address="Срочный адрес", urgent="1", agreed_day="")
        html = self.client("brigada1").get("/brigada").text
        self.assertLess(html.index("Срочный адрес"), html.index("Обычный адрес"))


if __name__ == "__main__":
    unittest.main()
