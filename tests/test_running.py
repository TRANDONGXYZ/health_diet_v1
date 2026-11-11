import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch
import app
from mealapp import ai, nutrition, store, weekly


class RunningPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.clock=patch.object(weekly,"today",return_value=date(2026,11,9));self.clock.start()
        self.folder=tempfile.TemporaryDirectory()
        self.data=patch.object(store,"DATA",Path(self.folder.name));self.data.start();store.initialize()
        self.config_patch=patch.object(ai,"ROOT",Path(self.folder.name));self.config_patch.start()

    def tearDown(self):
        self.clock.stop();self.config_patch.stop();self.data.stop();self.folder.cleanup()

    def dish(self,name,running=False,vegan=True):
        r=app.clean_recipe({"name":name,"good_for_running":running,"vegan":vegan,"ingredients":[{"name":"tofu","grams":100}]})
        store.put("recipes",r["id"],r);return r

    def test_quick_toggle_without_ai_or_lookup_preserves_recipe_and_calories(self):
        r=self.dish("Tofu");before=store.calories(r)
        with patch.object(ai,"ask",side_effect=AssertionError("No AI")),patch.object(nutrition,"resolve",side_effect=AssertionError("No lookup")):
            app.post("/api/recipes/running",{"id":r["id"],"good_for_running":True})
        store.initialize();saved=store.get("recipes",r["id"])
        self.assertTrue(saved["good_for_running"])
        self.assertEqual(store.calories(saved),before)
        self.assertEqual(saved["ingredients"],r["ingredients"])

    def test_invalid_toggle_rejected(self):
        r=self.dish("Tofu")
        for value in ("true",None,1):
            with self.assertRaises(ValueError):app.post("/api/recipes/running",{"id":r["id"],"good_for_running":value})
        with self.assertRaisesRegex(ValueError,"not found"):app.post("/api/recipes/running",{"id":"missing","good_for_running":True})

    def test_priority_repairs_ai_ignoring_flags_and_replaces_stale_advice(self):
        marked=[self.dish("Marked "+str(i),True) for i in range(3)];self.dish("Other")
        def respond(task,schema,**kwargs):
            rows=json.loads(task.split("LIBRARY:\n")[1]);unmarked=next(r for r in rows if not r["good_for_running"])
            return {"days":[{"recipe_id":unmarked["id"],"timing":"morning","prep":"Advice for other dish","reason":"Other"} for _ in range(7)]}
        with patch.object(ai,"ask",side_effect=respond) as model:plan=app.make_plan({"week_start":"2026-11-09","prioritize_running":True})
        self.assertEqual(model.call_count,1);ids={r["id"] for r in marked}
        self.assertTrue(ids.issubset({d["recipe_id"] for d in plan["days"]}))
        self.assertTrue(all(d["prep"]!="Advice for other dish" for d in plan["days"] if d["recipe_id"] in ids))
        weekly.commit(plan);self.assertTrue(store.get("plans",plan["week_start"])["prioritize_running"])

    def test_running_dish_included_in_large_library_candidate_pool(self):
        for i in range(35):self.dish("Other "+str(i))
        marked=self.dish("Running",True)
        def respond(task,schema,**kwargs):
            rows=json.loads(task.split("LIBRARY:\n")[1]);self.assertEqual(len(rows),12)
            row=next(r for r in rows if r["good_for_running"])
            return {"days":[{"recipe_id":row["id"],"timing":"morning","prep":"Review source","reason":"Marked"} for _ in range(7)]}
        with patch.object(ai,"ask",side_effect=respond):plan=app.make_plan({"week_start":"2026-11-09","prioritize_running":True})
        self.assertEqual({d["recipe_id"] for d in plan["days"]},{marked["id"]})

    def test_vegan_filter_takes_precedence_and_no_running_match_explained(self):
        self.dish("Non-vegan runner",True,False);vegan=self.dish("Vegan",False,True)
        plan=app.make_plan({"week_start":"2026-11-09","vegan_only":True,"prioritize_running":True,"simple":True})
        self.assertEqual({d["recipe_id"] for d in plan["days"]},{vegan["id"]})
        self.assertRegex(plan["warnings"][0],"No matching saved dishes")

    def test_priority_off_keeps_unmarked_dishes_eligible(self):
        self.dish("Marked",True);other=self.dish("Other")
        plan=app.make_plan({"week_start":"2026-11-09","prioritize_running":False,"simple":True})
        self.assertIn(other["id"],{d["recipe_id"] for d in plan["days"]})

    def test_more_than_seven_marked_gives_seven_distinct_saved_dishes(self):
        marked=[self.dish(str(i),True) for i in range(10)]
        with patch.object(ai,"ask",return_value={"days":[{"recipe_id":"r1","timing":"morning"} for _ in range(7)]}):plan=app.make_plan({"week_start":"2026-11-09","prioritize_running":True})
        ids={d["recipe_id"] for d in plan["days"]}
        self.assertEqual(len(ids),7);self.assertTrue(ids.issubset({r["id"] for r in marked}))
