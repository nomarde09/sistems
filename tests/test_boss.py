"""Этап 5: руководитель исправляет и отменяет заявки, офис ищет и фильтрует."""

import unittest
from datetime import timedelta

from test_app import GOOD_REQUEST, BrigadeHelpers, m

REPORT = {"report_work": "Установили 4 камеры", "report_price": "85000",
          "report_payment": "cash", "report_comment": ""}


class BossHelpers(BrigadeHelpers):
    def edit(self, c, rid, data):
        return self.post(c, f"/zayavka/{rid}/ispravit", data)

    def cancel(self, c, rid, reason=""):
        return self.post(c, f"/zayavka/{rid}/otmenit", {"reason": reason}, page=f"/zayavka/{rid}")

    def done_request(self, brigade="brigada1", price="85000", **fields):
        rid = self.make_request(**fields)
        c = self.client(brigade)
        self.take(c, rid)
        self.post(c, f"/brigada/zayavka/{rid}/otchet", {**REPORT, "report_price": price}, page="/brigada/moi")
        return rid

    def set_done_at(self, rid, when):
        with m.app.app_context():
            db = m.get_db()
            db.execute("UPDATE requests SET done_at = ? WHERE id = ?", (when.isoformat(), rid))
            db.commit()


class EditTests(BossHelpers):
    def test_boss_sees_edit_and_cancel_buttons_managers_do_not(self):
        rid = self.make_request()
        boss_page = self.client("rukovoditel").get(f"/zayavka/{rid}").text
        self.assertIn("Исправить", boss_page)
        self.assertIn("Отменить заявку", boss_page)
        manager_page = self.client("manager1").get(f"/zayavka/{rid}").text
        self.assertNotIn(f"/zayavka/{rid}/ispravit", manager_page)
        self.assertNotIn(f"/zayavka/{rid}/otmenit", manager_page)

    def test_edit_form_is_prefilled(self):
        rid = self.make_request()
        html = self.client("rukovoditel").get(f"/zayavka/{rid}/ispravit").text
        self.assertIn("Исправить заявку", html)
        self.assertIn('value="Иван Петров"', html)
        self.assertIn('value="20" selected', html)
        self.assertIn('value="2026-11" selected', html)
        self.assertIn("Сохранить исправления", html)

    def test_boss_edits_request_and_history_lists_changes(self):
        rid = self.make_request()
        boss = self.client("rukovoditel")
        r = self.edit(boss, rid, {**GOOD_REQUEST, "phone": "+7 777 000-11-22", "urgent": ""})
        self.assertEqual(r.status_code, 302)
        row = self.row(rid)
        self.assertEqual((row["phone"], row["urgent"], row["client_name"]), ("+7 777 000-11-22", 0, "Иван Петров"))
        self.assertEqual(row["created_by"], "manager1")  # автор не меняется
        page = boss.get(f"/zayavka/{rid}").text
        self.assertIn("Заявка исправлена", page)
        self.assertIn("исправлено: телефон, срочность", page)
        self.assertEqual(self.actions(rid)[-1], ("edited", "rukovoditel"))

    def test_edit_without_changes_writes_nothing(self):
        rid = self.make_request()
        boss = self.client("rukovoditel")
        r = self.edit(boss, rid, GOOD_REQUEST)
        self.assertIn("Ничего не изменилось", boss.get(r.location).text)
        self.assertEqual([a for a, _ in self.actions(rid)], ["created"])

    def test_edit_validates_like_new_form(self):
        rid = self.make_request()
        r = self.edit(self.client("rukovoditel"), rid, {**GOOD_REQUEST, "address": ""})
        self.assertIn("Укажите адрес", r.text)
        self.assertEqual(self.row(rid)["address"], GOOD_REQUEST["address"])

    def test_clear_agreed_day(self):
        rid = self.make_request()
        self.edit(self.client("rukovoditel"), rid, {**GOOD_REQUEST, "agreed_day": ""})
        self.assertIsNone(self.row(rid)["agreed_date"])

    def test_edit_in_work_keeps_brigade(self):
        rid = self.make_request()
        self.take(self.client("brigada2"), rid)
        self.edit(self.client("rukovoditel"), rid, {**GOOD_REQUEST, "description": "Новое описание"})
        row = self.row(rid)
        self.assertEqual((row["status"], row["taken_by"], row["description"]), ("in_work", "brigada2", "Новое описание"))
        self.assertIn("Новое описание", self.client("brigada2").get(f"/brigada/zayavka/{rid}").text)

    def test_boss_fixes_report_price(self):
        rid = self.done_request()
        boss = self.client("rukovoditel")
        html = boss.get(f"/zayavka/{rid}/ispravit").text
        self.assertIn("Отчёт бригады", html)
        self.assertIn('value="85000"', html)
        self.edit(boss, rid, {**GOOD_REQUEST, **REPORT, "report_price": "90 000"})
        row = self.row(rid)
        self.assertEqual((row["status"], row["report_price"]), ("done", 90000))
        self.assertIn("исправлено: цена", boss.get(f"/zayavka/{rid}").text)

    def test_report_price_required_when_editing_done(self):
        rid = self.done_request()
        r = self.edit(self.client("rukovoditel"), rid, {**GOOD_REQUEST, **REPORT, "report_price": ""})
        self.assertIn("Укажите цену", r.text)
        self.assertEqual(self.row(rid)["report_price"], 85000)

    def test_managers_and_brigades_cannot_edit(self):
        rid = self.make_request()
        for uid in ("manager1", "manager2", "brigada1"):
            with self.subTest(user=uid):
                c = self.client(uid)
                self.assertEqual(c.get(f"/zayavka/{rid}/ispravit").status_code, 302)
                self.post(c, f"/zayavka/{rid}/ispravit", {**GOOD_REQUEST, "address": "Чужой"}, page="/")
                self.assertEqual(self.row(rid)["address"], GOOD_REQUEST["address"])

    def test_past_month_stays_selectable_when_editing_old_request(self):
        rid = self.make_request(agreed_day="5", agreed_month="2026-01")
        html = self.client("rukovoditel").get(f"/zayavka/{rid}/ispravit").text
        self.assertIn('value="2026-01" selected', html)


class CancelTests(BossHelpers):
    def test_boss_cancels_new_request_with_reason(self):
        rid = self.make_request(address="ул. Лесная, 1")
        boss = self.client("rukovoditel")
        r = self.cancel(boss, rid, "Клиент передумал")
        page = boss.get(r.location).text
        self.assertIn("отменена", page)
        self.assertIn("Причина: Клиент передумал", page)
        row = self.row(rid)
        self.assertEqual((row["status"], row["cancelled_by"], row["cancel_reason"]),
                         ("cancelled", "rukovoditel", "Клиент передумал"))
        self.assertEqual(self.actions(rid)[-1], ("cancelled", "rukovoditel"))
        self.assertNotIn("ул. Лесная, 1", self.client("brigada1").get("/brigada").text)
        self.assertNotIn("Исправить</a>", page)

    def test_cancel_in_work_removes_it_from_brigade(self):
        rid = self.make_request(address="ул. Лесная, 1")
        b1 = self.client("brigada1")
        self.take(b1, rid)
        self.assertIn("сейчас у бригады", self.client("rukovoditel").get(f"/zayavka/{rid}").text)
        self.cancel(self.client("rukovoditel"), rid)
        self.assertNotIn("ул. Лесная, 1", b1.get("/brigada/moi").text)
        self.post(b1, f"/brigada/zayavka/{rid}/otchet", REPORT, page="/brigada/moi")
        self.assertEqual(self.row(rid)["status"], "cancelled")

    def test_done_request_cannot_be_cancelled(self):
        rid = self.done_request()
        boss = self.client("rukovoditel")
        self.assertNotIn(f"/zayavka/{rid}/otmenit", boss.get(f"/zayavka/{rid}").text)
        self.cancel(boss, rid)
        self.assertEqual(self.row(rid)["status"], "done")

    def test_managers_cannot_cancel(self):
        rid = self.make_request()
        self.cancel(self.client("manager1"), rid)
        self.assertEqual(self.row(rid)["status"], "new")

    def test_cancelled_request_cannot_be_edited(self):
        rid = self.make_request()
        boss = self.client("rukovoditel")
        self.cancel(boss, rid)
        r = boss.get(f"/zayavka/{rid}/ispravit")
        self.assertEqual(r.status_code, 302)


class SearchTests(BossHelpers):
    def setUp(self):
        super().setUp()
        self.a = self.make_request(client_name="Анна Смирнова", address="ул. Садовая, 5",
                                   phone="+7 701 555-12-34", equipment="kamery")
        self.b = self.done_request("brigada1", price="40000", client_name="ООО Ромашка",
                                   address="пр. Мира, 12", phone="8 702 111 22 33", equipment="vorota")
        self.c = self.done_request("brigada2", price="25 000", client_name="Пётр Иванов",
                                   address="мкр. Самал, 3", phone="+77071234567", equipment="domofon")
        boss = self.client("rukovoditel")
        self.post(boss, "/ofis/novaya", {**GOOD_REQUEST, "client_name": "От руководителя",
                                         "address": "ул. Абая, 10", "phone": "+7 700 999 88 77"})
        self.office = self.client("manager2")

    def listing(self, **params):
        params.setdefault("status", "all")
        return self.office.get("/ofis", query_string=params).text

    def shown(self, html):
        return {name for name in ("Анна Смирнова", "ООО Ромашка", "Пётр Иванов", "От руководителя")
                if name in html}

    def test_no_filters_shows_everything_and_total(self):
        html = self.listing()
        self.assertEqual(len(self.shown(html)), 4)
        self.assertRegex(html, r'Все <span class="count">4</span>')
        self.assertIn("Выполнено: <strong>2</strong> на сумму", html)
        self.assertIn("65 000", html)

    def test_search_ignores_case(self):
        self.assertEqual(self.shown(self.listing(q="ромашка")), {"ООО Ромашка"})
        self.assertEqual(self.shown(self.listing(q="САДОВАЯ")), {"Анна Смирнова"})
        self.assertEqual(self.shown(self.listing(q="домофон")), {"Пётр Иванов"})

    def test_search_by_phone_digits_any_format(self):
        self.assertEqual(self.shown(self.listing(q="701 555")), {"Анна Смирнова"})
        self.assertEqual(self.shown(self.listing(q="87021112233")), {"ООО Ромашка"})
        self.assertEqual(self.shown(self.listing(q="+7 707 123")), {"Пётр Иванов"})

    def test_search_by_request_number(self):
        self.assertEqual(self.shown(self.listing(q=f"№{self.b}")), {"ООО Ромашка"})
        self.assertEqual(self.shown(self.listing(q=str(self.c))), {"Пётр Иванов"})

    def test_filter_by_status_brigade_author(self):
        self.assertEqual(self.shown(self.listing(status="new")), {"Анна Смирнова", "От руководителя"})
        self.assertEqual(self.shown(self.listing(brigada="brigada2")), {"Пётр Иванов"})
        self.assertEqual(self.shown(self.listing(avtor="rukovoditel")), {"От руководителя"})
        self.assertEqual(self.shown(self.listing(avtor="manager1", status="done", brigada="brigada1")),
                         {"ООО Ромашка"})

    def test_filtered_total(self):
        html = self.listing(brigada="brigada2")
        self.assertIn("Найдено: <strong>1</strong>", html)
        self.assertIn("25 000", html)

    def test_period_of_completion(self):
        now = m.now_local()
        self.set_done_at(self.b, now - timedelta(days=1))
        first_of_month = now.replace(day=1)
        self.set_done_at(self.c, first_of_month - timedelta(days=3))  # прошлый месяц
        self.assertEqual(self.shown(self.listing(period="today")), set())
        self.assertEqual(self.shown(self.listing(period="yesterday")), {"ООО Ромашка"})
        self.assertEqual(self.shown(self.listing(period="prev_month")), {"Пётр Иванов"})
        self.assertEqual(self.shown(self.listing(period="week")) >= {"ООО Ромашка"}, True)

    def test_nothing_found_message(self):
        html = self.listing(q="несуществующий клиент")
        self.assertIn("Ничего не нашлось", html)
        self.assertIn("Сбросить", html)

    def test_brigade_cannot_open_office_list(self):
        self.assertEqual(self.client("brigada1").get("/ofis?q=x").status_code, 302)


class FullJourneyTest(BossHelpers):
    """Весь путь заявки, как в жизни: менеджер → бригада → отчёт → руководитель находит."""

    def test_whole_path(self):
        manager = self.client("manager2")
        r = self.post(manager, "/ofis/novaya", {**GOOD_REQUEST, "client_name": "Сквозная проверка",
                                                "phone": "8 (705) 222-33-44"})
        rid = int(r.location.split("/zayavka/")[1].split("?")[0])

        b1, b2 = self.client("brigada1"), self.client("brigada2")
        self.assertIn("Сквозная", b1.get(f"/brigada/zayavka/{rid}").text)
        self.take(b2, rid)
        page = b2.get(f"/brigada/zayavka/{rid}").text
        self.assertIn('href="tel:87052223344"', page)
        self.assertIn("Позвонить", page)
        self.assertNotIn(f"/brigada/zayavka/{rid}/vzyat", b1.get("/brigada").text)

        self.post(b2, f"/brigada/zayavka/{rid}/otchet",
                  {"report_work": "Поставили 2 камеры", "report_price": "60 000", "report_payment": "kaspi_pay"}, page="/brigada/moi")

        boss = self.client("rukovoditel")
        html = boss.get("/ofis", query_string={"brigada": "brigada2", "period": "today"}).text
        self.assertIn("Сквозная проверка", html)
        self.assertIn("Найдено: <strong>1</strong>", html)
        self.assertIn("60 000", html)
        card = boss.get(f"/zayavka/{rid}").text
        for text in ("Менеджер 2", "Бригада 2", "создал(а) заявку", "взял(а) в работу", "выполнил(а)",
                     "Поставили 2 камеры"):
            self.assertIn(text, card)
        self.assertEqual([a for a, _ in self.actions(rid)], ["created", "taken", "done"])

    def test_pages_have_phone_icon_and_manifest(self):
        html = self.client("brigada1").get("/brigada").text
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        c = m.app.test_client()
        for path in ("/static/manifest.webmanifest", "/static/icon-192.png", "/static/apple-touch-icon.png"):
            with c.get(path) as r:
                self.assertEqual(r.status_code, 200, path)

    def test_tel_link_format(self):
        self.assertEqual(m.format_tel_link("8 (705) 222-33-44"), "87052223344")
        self.assertEqual(m.format_tel_link("+7 701 555-12-34"), "+77015551234")


if __name__ == "__main__":
    unittest.main()
