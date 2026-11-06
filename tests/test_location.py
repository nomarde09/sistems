"""Геолокация в заявке: ссылка на карту или координаты; кнопки карты у бригады и офиса."""

import unittest

from test_app import GOOD_REQUEST, m
from test_boss import BossHelpers

LINK_2GIS = "https://2gis.kz/almaty/geo/70000001234567"
SHARED = "Вот моя геолокация: https://go.2gis.com/abc12 приезжайте после обеда"


class LocationTests(BossHelpers):
    def test_form_has_location_field_and_button(self):
        html = self.client("manager1").get("/ofis/novaya").text
        self.assertIn('name="location"', html)
        self.assertIn("Взять моё местоположение", html)

    def test_location_is_optional(self):
        rid = self.make_request()
        self.assertIsNone(self.row(rid)["location"])

    def test_map_link_saved_and_shown_to_brigade(self):
        rid = self.make_request(location=LINK_2GIS)
        self.assertEqual(self.row(rid)["location"], LINK_2GIS)
        page = self.client("brigada1").get(f"/brigada/zayavka/{rid}").text
        self.assertIn(f'href="{LINK_2GIS}"', page)
        self.assertIn("Открыть на карте", page)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertIn("📍 на карте", self.client("brigada1").get("/brigada").text)

    def test_link_taken_out_of_shared_message(self):
        rid = self.make_request(location=SHARED)
        self.assertEqual(self.row(rid)["location"], "https://go.2gis.com/abc12")

    def test_coordinates_get_three_map_buttons(self):
        rid = self.make_request(location="43,238949 76,889709")
        self.assertEqual(self.row(rid)["location"], "43.238949, 76.889709")
        page = self.client("brigada1").get(f"/brigada/zayavka/{rid}").text
        self.assertIn("https://2gis.kz/geo/76.889709,43.238949", page)
        self.assertIn("https://yandex.kz/maps/?pt=76.889709,43.238949", page)
        self.assertIn("query=43.238949,76.889709", page)

    def test_without_location_brigade_can_search_address(self):
        rid = self.make_request(address="ул. Абая 10, Алматы")
        page = self.client("brigada1").get(f"/brigada/zayavka/{rid}").text
        self.assertIn("Найти адрес в 2ГИС", page)
        self.assertIn("https://2gis.kz/search/", page)

    def test_bad_location_rejected(self):
        c = self.client("manager1")
        for bad in ("рядом с рынком", "javascript:alert(1)", "99.0, 200.0"):
            with self.subTest(value=bad):
                r = self.post(c, "/ofis/novaya", {**GOOD_REQUEST, "location": bad})
                self.assertIn("Вставьте ссылку на карту", r.text)
                self.assertIn(bad.replace("'", "&#39;"), r.text)  # введённое не стирается
        with m.app.app_context():
            self.assertEqual(m.get_db().execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)

    def test_office_card_shows_location(self):
        rid = self.make_request(location=LINK_2GIS)
        page = self.client("rukovoditel").get(f"/zayavka/{rid}").text
        self.assertIn("Геолокация", page)
        self.assertIn(LINK_2GIS, page)

    def test_boss_can_add_and_remove_location(self):
        rid = self.make_request()
        boss = self.client("rukovoditel")
        self.edit(boss, rid, {**GOOD_REQUEST, "location": "43.2389, 76.8897"})
        self.assertEqual(self.row(rid)["location"], "43.238900, 76.889700")
        self.assertIn("исправлено: геолокация", boss.get(f"/zayavka/{rid}").text)
        self.edit(boss, rid, {**GOOD_REQUEST, "location": ""})
        self.assertIsNone(self.row(rid)["location"])

    def test_parse_coords(self):
        self.assertEqual(m.parse_coords("43.2389, 76.8897"), (43.2389, 76.8897))
        self.assertEqual(m.parse_coords("-33.86;151.2"), (-33.86, 151.2))
        self.assertIsNone(m.parse_coords("ул. Абая 10"))
        self.assertIsNone(m.parse_coords("120, 50"))


if __name__ == "__main__":
    unittest.main()
