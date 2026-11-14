import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app
from mealapp import ai, recipe_extract, source_language, store


class SourceExtractionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data = patch.object(store, "DATA", Path(self.folder.name)); self.data.start()
        self.config_patch = patch.object(ai, "ROOT", Path(self.folder.name)); self.config_patch.start()
        store.initialize()

    def tearDown(self):
        self.config_patch.stop(); self.data.stop(); self.folder.cleanup()

    def extract(self, source, rows):
        response = dict(name="Test", vegan=None, servings=None, estimated_minutes=None, ingredients=rows)
        with patch.object(ai, "ask", return_value=response) as model:
            recipe = ai.extract_recipe(source)
        self.assertEqual(model.call_count, 1, "No cooking or translation passes")
        self.assertNotIn("groups", recipe)
        return recipe, model.call_args

    def test_native_language_and_model_routing(self):
        for lang, source, name in [("vi", "Đậu hũ chiên. Nguyên liệu: 300 g đậu hũ. Chiên và nêm vừa ăn.", "đậu hũ"), ("en", "Tofu bowl. Ingredients: 300 g tofu. Cook the tofu until hot and serve.", "tofu")]:
            result, call = self.extract(source, [{"name":name, "quantity":"300 g", "grams":300, "source_quote":"300 g " + name}])
            self.assertEqual(result["content_language"],lang)
            self.assertEqual(result["ingredients"][0]["name"],name)
            label="Vietnamese" if lang=="vi" else "English"
            self.assertEqual(call.kwargs["model"],ai.config()["vietnamese_model" if lang=="vi" else "english_model"])
            self.assertEqual(call.kwargs["language"],label)
            self.assertTrue(all(chunk in call.args[0] for chunk in recipe_extract.source_chunks(source)))

    def test_numbered_evidence_resolves_locally_without_generated_quote(self):
        source="Tofu bowl. Use 300 g tofu. Cook until hot."
        result,call=self.extract(source,[{"name":"tofu","quantity":"300 g","grams":None,"source_id":2}])
        self.assertEqual(result["ingredients"][0]["grams"],300)
        self.assertIn("300 g tofu",result["ingredients"][0]["source_quote"])
        self.assertIn("source_id",call.args[1]["properties"]["ingredients"]["items"]["properties"])
        self.assertNotIn("source_quote",call.args[1]["properties"]["ingredients"]["items"]["properties"])

    def test_chunking_keeps_literal_text_and_bounds_long_tokens(self):
        source="Use " + "largeword"*100 + ". Add tofu."
        chunks=recipe_extract.source_chunks(source)
        self.assertLess(len(chunks),10)
        self.assertTrue(all(len(chunk)<=320 and chunk in source for chunk in chunks))

    def test_detection_handles_structured_escaped_text_and_foreign_dish_name(self):
        self.assertEqual(source_language.detect(json.dumps({"name":"Đậu hũ", "recipeIngredient":["200 g đậu hũ", "2 g muối"], "recipeInstructions":"Cho vào chảo và nấu chín."})), "vi")
        self.assertEqual(source_language.detect("Phở bò. Ingredients: beef and rice noodles. Add the beef to the water and cook until tender. Serve with the cooked noodles."), "en")
        self.assertEqual(source_language.detect("Nguyen lieu: dau hu, ca chua, toi. Cho vao chao, nau chin va nem vua an."), "vi")

    def test_rejects_fake_quotes_and_ingredients_inferred_from_word_substrings(self):
        source="Use 100 g tofu. Boil until hot; no other ingredients are listed."
        rows=[{"name":"tofu","grams":100,"source_quote":"100 g tofu"}, {"name":"salt","grams":5,"source_quote":"5 g salt"}, {"name":"oil","grams":10,"source_quote":"Boil until hot"}, {"name":"water","grams":200,"source_quote":"Use 100 g tofu"}]
        result,_=self.extract(source,rows)
        self.assertEqual([i["name"] for i in result["ingredients"]],["tofu"])
        self.assertTrue(result["extraction_warnings"])

    def test_no_verified_ingredients_raises(self):
        with self.assertRaisesRegex(ValueError,"verified"):
            self.extract("Boil the dish until hot.",[{"name":"oil","source_quote":"Boil the dish until hot."}])

    def test_source_weights_override_ai_and_unknown_weights_stay_unknown(self):
        result,_=self.extract("Use 0.5 kg tofu and salt to taste.",[{"name":"tofu","grams":5,"source_quote":"0.5 kg tofu"},{"name":"salt","grams":50,"source_quote":"salt to taste"}])
        self.assertEqual(result["ingredients"][0]["grams"],500)
        self.assertFalse(result["ingredients"][0]["weight_estimated"])
        self.assertIsNone(result["ingredients"][1]["grams"])
        self.assertFalse(store.calories(result)["complete"])

    def test_compound_thousands_and_fraction_mass(self):
        for quote,name,lang,grams in [("1 kg 200 g bò","bò","vi",1200),("1.200 g bò","bò","vi",1200),("1,200 g beef","beef","en",1200),("0,5 kg đậu hũ","đậu hũ","vi",500),("1/2 kg tofu","tofu","en",500)]:
            self.assertEqual(recipe_extract.grams_from_quote(quote,name,lang),grams)

    def test_count_weight_marked_estimated_and_quantity_grounded(self):
        result,_=self.extract("Ingredients: 2 eggs and 10 g salt.",[{"name":"eggs","quantity":"9 eggs","grams":100,"source_quote":"2 eggs"},{"name":"salt","quantity":"10 g","grams":5,"source_quote":"10 g salt"}])
        self.assertEqual(result["ingredients"][0]["quantity"],"2 eggs")
        self.assertFalse(result["ingredients"][0]["weight_estimated"])
        self.assertIsNone(result["ingredients"][0]["grams"])
        self.assertEqual(result["ingredients"][1]["grams"],10)

    def test_valid_count_conversion_is_marked_estimated(self):
        result,_=self.extract("Use 2 eggs in the recipe.",[{"name":"eggs","quantity":"2 eggs","grams":100,"source_quote":"2 eggs"}])
        self.assertEqual(result["ingredients"][0]["grams"],100)
        self.assertTrue(result["ingredients"][0]["weight_estimated"])

    def test_wrong_source_id_can_only_recover_a_name_actually_in_source(self):
        result,_=self.extract("Use tofu. Then add 2 eggs.",[{"name":"eggs","quantity":"2 eggs","grams":100,"source_id":1},{"name":"salt","quantity":"5 g","grams":5,"source_id":1}])
        self.assertEqual([i["name"] for i in result["ingredients"]],["eggs"])
        self.assertIn("2 eggs",result["ingredients"][0]["source_quote"])
        self.assertTrue(result["extraction_warnings"])

    def test_structured_rows_preserved_including_split_amounts(self):
        source=json.dumps({"name":"Tofu","recipeIngredient":["200 g tofu","100 g tofu","2 g salt"],"recipeInstructions":"Cook until hot."})
        result,_=self.extract(source,[{"name":"tofu","grams":200,"source_quote":"200 g tofu"}])
        self.assertEqual(len(result["ingredients"]),3)
        self.assertEqual(sum(i["grams"] for i in result["ingredients"]),302)
        self.assertEqual(result["name"],"Tofu")

    def test_broad_or_name_only_structured_quote_does_not_double_count_rows(self):
        source=json.dumps({"recipeIngredient":["200 g tofu","100 g tofu","2 g salt"]})
        for quote in ("tofu","200 g tofu\n100 g tofu\n2 g salt"):
            result,_=self.extract(source,[{"name":"tofu","quantity":"300 g","grams":300,"source_quote":quote}])
            self.assertEqual(len(result["ingredients"]),3)
            self.assertEqual(sum(i["grams"] for i in result["ingredients"]),302)

    def test_unknown_structured_food_names_are_kept_without_stripping_initial_letters(self):
        result,_=self.extract(json.dumps({"recipeIngredient":["gari","100 g tamarind paste"]}),[])
        self.assertEqual([i["name"] for i in result["ingredients"]],["gari","tamarind paste"])
        self.assertEqual(result["ingredients"][1]["grams"],100)

    def test_same_quote_not_double_counted(self):
        row={"name":"tofu","grams":200,"source_quote":"200 g tofu"}
        result,_=self.extract("Ingredients: 200 g tofu. Cook until hot.",[row,row.copy()])
        self.assertEqual(len(result["ingredients"]),1)

    def test_model_quote_wrappers_allowed_but_evidence_still_required(self):
        result,_=self.extract("Nguyên liệu: 300 g đậu hũ.",[{"name":"đậu hũ","grams":None,"source_quote":'"300 g đậu hũ"'}])
        self.assertEqual(result["ingredients"][0]["source_quote"],"300 g đậu hũ")
        self.assertEqual(result["ingredients"][0]["grams"],300)

    def test_source_mass_recovers_name_only_quotes_without_neighbor_weight_leak(self):
        result,_=self.extract("Ingredients: 300 g tofu, 5 g garlic. Cook tofu with garlic.",[{"name":"tofu","source_quote":"tofu","grams":None},{"name":"garlic","source_quote":"garlic","grams":None}])
        self.assertEqual([i["grams"] for i in result["ingredients"]],[300,5])
        self.assertIsNone(recipe_extract.grams_from_quote("tofu and 200 g tomatoes","tofu","en"))

    def test_omitted_explicit_common_ingredient_recovered_without_ai_retry(self):
        result,call=self.extract("Use 300 g tofu, 5 g garlic and 2 g salt. Cook the tofu.",[{"name":"tofu","grams":300,"source_quote":"300 g tofu"}])
        self.assertEqual({i["name"]:i["grams"] for i in result["ingredients"]},{"tofu":300,"garlic":5,"salt":2})

    def test_repeated_unstructured_mentions_not_counted_twice(self):
        result,_=self.extract("Use 200 g tofu. Cook the tofu until hot.",[{"name":"tofu","grams":200,"source_quote":"200 g tofu"},{"name":"tofu","grams":200,"source_quote":"Cook the tofu"}])
        self.assertEqual(len(result["ingredients"]),1)
        self.assertEqual(result["ingredients"][0]["grams"],200)

    def test_total_time_and_servings_require_source_facts(self):
        source="Use 100 g tofu. Cook for 8 minutes, then cook for 5 minutes."
        with patch.object(ai,"ask",return_value={"name":"Tofu","vegan":True,"servings":4,"estimated_minutes":10,"ingredients":[{"name":"tofu","source_quote":"100 g tofu"}]}):
            result=ai.extract_recipe(source)
        self.assertIsNone(result["servings"])
        self.assertIsNone(result["estimated_minutes"])
        self.assertEqual(recipe_extract.stated_time("Total cooking time: 25 minutes",None),25)
        self.assertEqual(recipe_extract.stated_time("",{"totalTime":"PT1H20M"}),80)
        self.assertIsNone(recipe_extract.stated_time("",{"cookTime":"PT0M"}))

    def test_legacy_recipe_flattens_without_losing_original_steps_or_calorie_references(self):
        legacy={"id":"legacy","name":"Old recipe","servings":2,"groups":[{"title":"Cook","instruction":"Original instructions","ingredients":[{"name":"tofu","grams":200,"food_id":store.find_food("tofu")["id"]}]}]}
        original=copy.deepcopy(legacy);store.put("recipes","legacy",legacy)
        result=store.get("recipes","legacy")
        self.assertNotIn("groups",result)
        self.assertEqual(result["legacy_groups"],original["groups"])
        self.assertEqual(result["ingredients"][0]["food_id"],original["groups"][0]["ingredients"][0]["food_id"])
        self.assertEqual(store.calories(result)["total"],288)
        self.assertFalse(result["good_for_running"])
        with patch.object(ai,"ask",side_effect=AssertionError("No AI on save")):
            app.post("/api/recipes",result)
        self.assertEqual(store.get("recipes","legacy")["legacy_groups"],original["groups"])

    def test_auto_language_overrides_stale_preferences_and_detection_needs_no_ai(self):
        with patch.object(ai,"get",return_value={"language":"Vietnamese"}):
            self.assertEqual(ai.config()["language"],"auto")
        app.post("/api/settings",{"text_model":"qwen2.5:1.5b","vietnamese_model":"vi-custom","english_model":"en-custom"})
        with patch.object(ai,"ask",side_effect=AssertionError("Detection is not AI")):
            self.assertEqual(app.post("/api/source-language",{"text":"Use tofu and salt in the recipe."}),{"language":"en","model":"en-custom"})
            self.assertEqual(app.post("/api/source-language",{"text":"Cho đậu hũ vào chảo và nấu chín."}),{"language":"vi","model":"vi-custom"})
