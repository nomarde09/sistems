"""Тип заявки, оборудование, время, «Гарантийная»: хранение, показ, фильтры, статистика."""

import unittest

from test_app import GOOD_REQUEST, csrf_from, m
from test_tech import TechBase


class RequestFieldsTests(TechBase):
    def set_warranty(self, c, rid, value):
        token = csrf_from(c.get(f"/zayavka/{rid}").text)
        return c.post(f"/zayavka/{rid}/garantiya", data={"csrf": token, "warranty": value})

    def test_form_has_lists_and_no_old_field(self):
        html = self.client("manager1").get("/ofis/novaya").text
        for label in list(m.REQUEST_TYPES.values()) + list(m.EQUIPMENT.values()):
            self.assertIn(label, html)
        self.assertIn('name="agreed_time"', html)
        self.assertNotIn('name="work_type"', html)
        self.assertNotIn("Что монтируем", html)
        self.assertNotIn('name="warranty"', html)  # при создании гарантии нет

    def test_values_stored_as_separate_fields(self):
        rid = self.make_request(request_type="remont", equipment="domofon",
                                agreed_day="20", agreed_month="2026-11", agreed_time="14:30")
        row = self.row(rid)
        self.assertEqual((row["request_type"], row["equipment"], row["warranty"], row["agreed_time"]),
                         ("remont", "domofon", 0, "14:30"))

    def test_new_request_is_paid_even_if_warranty_sent(self):
        rid = self.make_request(warranty="1")
        self.assertEqual(self.row(rid)["warranty"], 0)

    def test_shown_in_lists_and_cards(self):
        rid = self.make_request(client_name="Клиент ТО", request_type="to", equipment="skud",
                                agreed_day="20", agreed_month="2026-11", agreed_time="09:00")
        listing = self.client("rukovoditel").get("/ofis").text
        self.assertIn("Плановое ТО · СКУД / замки", listing)
        self.assertIn("20 ноября, 09:00", listing)
        card = self.client("rukovoditel").get(f"/zayavka/{rid}").text
        for text in ("Плановое ТО", "СКУД / замки", "нет, платная", "20 ноября, 09:00"):
            self.assertIn(text, card)
        brigade = self.client("brigada1")
        self.assertIn("Плановое ТО · СКУД / замки", brigade.get("/brigada").text)
        self.assertIn("20 ноября, 09:00", brigade.get(f"/brigada/zayavka/{rid}").text)

    def test_time_needs_day_and_valid_value(self):
        c = self.client("manager1")
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "agreed_day": "", "agreed_time": "10:00"})
        self.assertIn("Время указано, а день — нет", r.text)
        r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "agreed_time": "25:99"})
        self.assertIn("Выберите время из списка", r.text)

    def test_warranty_switch_after_creation(self):
        rid = self.make_request()
        for uid in ("manager2", "rukovoditel"):
            with self.subTest(user=uid):
                c = self.client(uid)
                self.assertIn("Сделать гарантийной", c.get(f"/zayavka/{rid}").text)
                self.set_warranty(c, rid, "1")
                self.assertEqual(self.row(rid)["warranty"], 1)
                card = c.get(f"/zayavka/{rid}").text
                self.assertIn("Гарантия</span>", card)
                self.assertIn("Сделать платной", card)
                self.set_warranty(c, rid, "0")
                self.assertEqual(self.row(rid)["warranty"], 0)
        actions = [a for a, _ in self.actions(rid)]
        self.assertEqual(actions.count("warranty"), 4)

    def test_brigade_sees_warranty_mark_and_cannot_switch(self):
        rid = self.make_request(address="ул. Гарантийная, 1")
        self.set_warranty(self.client("rukovoditel"), rid, "1")
        b = self.client("brigada1")
        self.assertIn("Гарантия</span>", b.get("/brigada").text)
        self.assertIn("да — бесплатно для клиента", b.get(f"/brigada/zayavka/{rid}").text)
        token = csrf_from(b.get("/brigada").text)
        b.post(f"/zayavka/{rid}/garantiya", data={"csrf": token, "warranty": "0"})
        self.assertEqual(self.row(rid)["warranty"], 1)

    def test_boss_edits_type_equipment_time_warranty(self):
        rid = self.make_request()
        boss = self.client("rukovoditel")
        self.assertIn('name="warranty"', boss.get(f"/zayavka/{rid}/ispravit").text)
        self.edit(boss, rid, {**GOOD_REQUEST, "request_type": "demontazh", "equipment": "vorota",
                              "agreed_time": "18:00", "warranty": "1"})
        row = self.row(rid)
        self.assertEqual((row["request_type"], row["equipment"], row["agreed_time"], row["warranty"]),
                         ("demontazh", "vorota", "18:00", 1))
        self.assertIn("тип заявки, оборудование, время, гарантия", boss.get(f"/zayavka/{rid}").text)

    def test_filters(self):
        a = self.make_request(client_name="Монтаж камер", request_type="montazh", equipment="kamery")
        b = self.make_request(client_name="Ремонт ворот", request_type="remont", equipment="vorota")
        self.set_warranty(self.client("rukovoditel"), b, "1")
        office = self.client("manager1")

        def shown(**params):
            html = office.get("/ofis", query_string={"status": "all", **params}).text
            return {n for n in ("Монтаж камер", "Ремонт ворот") if n in html}

        self.assertEqual(shown(tip="remont"), {"Ремонт ворот"})
        self.assertEqual(shown(oborud="kamery"), {"Монтаж камер"})
        self.assertEqual(shown(garantiya="da"), {"Ремонт ворот"})
        self.assertEqual(shown(garantiya="net"), {"Монтаж камер"})
        self.assertEqual(shown(q="шлагбаум"), {"Ремонт ворот"})  # поиск по названию оборудования

    def test_stats_breakdown(self):
        self.done_request("brigada1", price="30000", request_type="montazh", equipment="kamery")
        rid = self.done_request("brigada2", price="0", request_type="remont", equipment="kamery")
        self.set_warranty(self.client("rukovoditel"), rid, "1")
        for html in (self.client("rukovoditel").get("/ofis/statistika").text,
                     self.tech_client().get("/tech/").text):
            self.assertIn("<h2>По типу заявки</h2>", html)
            self.assertRegex(html, r"<td>Монтаж \(новый объект\)</td><td>1</td><td>1</td><td class=\"price\">30 000")
            self.assertRegex(html, r"<td>Камеры / видеонаблюдение</td><td>2</td><td>2</td>")
            self.assertRegex(html, r"<td>Гарантийные</td><td>1</td><td>1</td>")
            self.assertRegex(html, r"<td>Платные</td><td>1</td><td>1</td>")

    def test_old_request_keeps_work_type(self):
        with m.app.app_context():
            db = m.get_db()
            cur = db.execute("""INSERT INTO requests (client_name, phone, address, work_type, description,
                                created_by, created_at) VALUES ('Старый', '12345', 'Б', '4 камеры', 'В',
                                'manager1', ?)""", (m.now_local().isoformat(),))
            db.commit()
            rid = cur.lastrowid
        boss = self.client("rukovoditel")
        self.assertIn("<dt>Что монтируем</dt><dd>4 камеры</dd>", boss.get(f"/zayavka/{rid}").text)
        self.assertIn("4 камеры", boss.get("/ofis").text)
        form = boss.get(f"/zayavka/{rid}/ispravit").text
        self.assertIn("Раньше в заявке было записано «Что монтируем»: 4 камеры", form)


if __name__ == "__main__":
    unittest.main()
