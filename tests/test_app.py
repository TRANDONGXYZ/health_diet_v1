import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from datetime import date
from unittest.mock import patch

import app
from mealapp import ai, nutrition, store, web, weekly


def sample(name="Tofu bowl", vegan=True):
    return app.clean_recipe({"name": name, "vegan": vegan, "servings": 2, "estimated_minutes": 20,
        "ingredients": [{"name": "tofu", "grams": 200}, {"name": "cooked rice", "grams": 300}]})


class AppTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(weekly, "today", return_value=date(2026,11,9)); self.clock.start()
        self.folder = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(store, "DATA", Path(self.folder.name))
        self.data_patch.start()
        self.config_patch = patch.object(ai, "ROOT", Path(self.folder.name))
        self.config_patch.start()
        store.initialize()

    def tearDown(self):
        self.clock.stop()
        self.config_patch.stop()
        self.data_patch.stop()
        self.folder.cleanup()

    def test_exact_calories_and_servings(self):
        result = store.calories(sample())
        self.assertTrue(result["complete"])
        self.assertEqual(result["total"], 678)
        self.assertEqual(result["per_serving"], 339)

    def test_vietnamese_alias_and_raw_cooked_are_distinct(self):
        self.assertEqual(store.find_food("ĐẬU HŨ")["kcal"], 144)
        self.assertNotEqual(store.find_food("gạo")["kcal"], store.find_food("cơm")["kcal"])

    def test_manual_override_recalculates_saved_recipes(self):
        recipe = sample()
        app.post("/api/recipes", recipe)
        tofu = store.find_food("tofu")
        app.post("/api/foods", dict(tofu, kcal=100))
        saved = store.get("recipes", recipe["id"])
        self.assertEqual(store.calories(saved)["total"], 590)

    def test_unknown_weight_and_unknown_food_never_count_as_complete(self):
        recipe = sample()
        recipe["ingredients"] += [{"name": "unknown", "grams": 10}, {"name": "salt", "grams": None}]
        result = store.calories(recipe)
        self.assertFalse(result["complete"])
        self.assertEqual(result["total"], 678)
        self.assertEqual(len(result["missing"]), 2)

    def test_zero_calorie_food_is_known(self):
        recipe = sample()
        recipe["ingredients"] = [{"name": "water", "grams": 100}]
        self.assertTrue(store.calories(recipe)["complete"])
        self.assertEqual(store.calories(recipe)["total"], 0)

    def test_invalid_numbers_rejected(self):
        for value in (-1, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.clean_recipe(dict(sample(), servings=value))

    def test_unknown_servings_not_invented(self):
        recipe = sample()
        recipe["servings"] = None
        self.assertIsNone(store.calories(recipe)["per_serving"])

    def test_rotation_repeats_saved_dishes_only(self):
        recipe = sample()
        store.put("recipes", recipe["id"], recipe)
        plan = app.make_plan({"week_start": "2026-11-09", "simple": True})
        self.assertEqual(len(plan["days"]), 7)
        self.assertEqual({d["recipe_id"] for d in plan["days"]}, {recipe["id"]})

    def test_no_matching_vegan_dishes(self):
        r = sample(vegan=False)
        store.put("recipes", r["id"], r)
        with self.assertRaisesRegex(ValueError, "matching dish"):
            app.make_plan({"week_start": "2026-11-09", "simple": True, "vegan_only": True})

    def test_ai_plan_cannot_invent_recipe_ids(self):
        r = sample()
        store.put("recipes", r["id"], r)
        with patch.object(ai, "ask", return_value={"days": [{"recipe_id": "invented", "timing": "morning"}] * 7}):
            with self.assertRaisesRegex(ValueError, "invalid plan"):
                app.make_plan({"week_start": "2026-11-09"})
        self.assertIsNone(store.get("state", "plan"))

    def test_plan_save_rejects_deleted_recipe(self):
        r = sample()
        store.put("recipes", r["id"], r)
        plan = app.make_plan({"week_start": "2026-11-09", "simple": True})
        store.remove_recipe(r["id"])
        with self.assertRaises(ValueError):
            weekly.commit(plan)

    def test_large_library_uses_bounded_prompt_and_maps_ids_to_saved_dishes(self):
        for i in range(35):
            r = sample(name=f"Dish {i}")
            r["notes"] = "Long instruction " * 500
            store.put("recipes", r["id"], r)
        def model(task, schema, **kwargs):
            self.assertLess(len(task), 18000)
            return {"days":[{"recipe_id":"r1","timing":"morning","prep":"Cân đong nguyên liệu.","reason":"Nấu mới."} for _ in range(7)]}
        with patch.object(ai, "ask", side_effect=model):
            plan = app.make_plan({"week_start":"2026-11-09"})
        saved_ids = {r["id"] for r in store.all_items("recipes")}
        self.assertTrue(all(d["recipe_id"] in saved_ids for d in plan["days"]))

    def test_recipe_jsonld_is_extracted_before_article_noise(self):
        body = '<html><script type="application/ld+json">' + json.dumps({"@graph": [{"@type": ["Recipe"], "name": "Test", "recipeIngredient": ["100 g tofu"], "recipeInstructions": "Fry it"}]}) + '</script><p>Lots of unrelated ads.</p></html>'
        with patch.object(web, "fetch", return_value=body):
            result = web.article_text("https://example.com")
        self.assertIn("100 g tofu", result)
        self.assertNotIn("unrelated ads", result)

    def test_caption_cleanup(self):
        result = web.clean_captions("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nChop <c>tofu</c>\nChop tofu\nFry it")
        self.assertEqual(result, "Chop tofu\nFry it")

    def test_public_url_rejects_private_networks(self):
        for url in ("http://127.0.0.1/test", "http://192.168.1.1/", "file:///etc/passwd", "http://[::1]/", "https://example.com:8765/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                web.public_url(url)

    def test_usda_zero_energy_not_dropped_and_lookup_cached(self):
        payload = {"foods": [{"fdcId": 1, "description": "Water", "foodNutrients": [{"nutrientId": 1008, "nutrientName": "Energy", "value": 0, "unitName": "KCAL"}]}]}
        with patch.object(web, "fetch", return_value=json.dumps(payload)) as fetch:
            self.assertEqual(nutrition.search("water")[0]["kcal"], 0)
            nutrition.search("water")
            self.assertEqual(fetch.call_count, 1)

    def test_failed_online_lookup_does_not_store_fabricated_calories(self):
        with patch.object(nutrition, "search", side_effect=ValueError("Offline")):
            result = nutrition.resolve(["new food"])
        self.assertIn("error", result[0])
        self.assertIsNone(store.find_food("new food"))

    def test_data_persists_after_reinitialization(self):
        r = sample()
        store.put("recipes", r["id"], r)
        store.initialize()
        self.assertEqual(store.get("recipes", r["id"])["name"], r["name"])



class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        cls.patch = patch.object(store, "DATA", Path(cls.folder.name))
        cls.patch.start()
        cls.config_patch = patch.object(ai, "ROOT", Path(cls.folder.name))
        cls.config_patch.start()
        store.initialize()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.config_patch.stop()
        cls.patch.stop()
        cls.folder.cleanup()

    def test_web_page_and_same_origin_post(self):
        with urllib.request.urlopen(self.url) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"Meal Notebook", response.read())
        request = urllib.request.Request(self.url + "/api/recipes", data=json.dumps(sample()).encode(), headers={"Content-Type": "application/json", "X-App-Token": app.TOKEN})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)["calories"]["total"], 678)

    def test_retired_routes_cannot_invoke_ai(self):
        with patch.object(ai,'ask',side_effect=AssertionError('Retired route invoked AI')) as ask:
            for prefix in ('/api','/api/online'):
                for name in ('identify','search','guidance'):
                    request=urllib.request.Request(self.url+prefix+'/nutrition/'+name,data=b'{}',headers={'Content-Type':'application/json','X-App-Token':app.TOKEN})
                    with self.subTest(prefix=prefix,name=name),self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request)
                    self.assertIn(error.exception.code,(400,404));error.exception.close()
            ask.assert_not_called()

    def test_csrf_and_external_origin_rejected(self):
        for headers in ({}, {"X-App-Token": app.TOKEN, "Origin": "https://evil.example"}):
            request = urllib.request.Request(self.url + "/api/settings", data=b"{}", headers={"Content-Type": "application/json", **headers})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_static_path_traversal_unavailable(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.url + "/../app.py")
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
