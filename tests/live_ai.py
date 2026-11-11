"""Opt-in checks against a real local Ollama model. No recipe data is retained."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app
from mealapp import ai, store, weekly

SOURCE = "Đậu hũ sốt cà chua. Khẩu phần: 2 người. Nguyên liệu: 300 g đậu hũ, 200 g cà chua, 10 g dầu ăn, 5 g tỏi, 2 g muối. Bước 1: Băm tỏi và cắt cà chua. Bước 2: Chiên đậu hũ với dầu ăn trong 8 phút. Bước 3: Phi tỏi, thêm cà chua và muối, nấu 5 phút. Thêm đậu hũ và nấu thêm 3 phút."


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model")
    parser.add_argument("--recipe-only", action="store_true")
    parser.add_argument("--cached-source", action="store_true", help="Retry the longest cached audio transcript without saving or printing its contents")
    args = parser.parse_args()
    values = ai.config()
    if args.model:
        values["vietnamese_model"] = args.model
        values["english_model"] = args.model
    source = SOURCE
    if args.cached_source:
        store.initialize()
        with store.db() as connection:
            cached = [json.loads(row[0]).get("text", "") for row in connection.execute("SELECT body FROM cache WHERE id LIKE 'speech:%'")]
        if not cached:
            raise SystemExit("No cached speech transcript was found.")
        source = max(cached, key=len)
    with tempfile.TemporaryDirectory() as folder, patch.object(store, "DATA", Path(folder)), patch.object(ai, "config", return_value=values):
        store.initialize()
        started = time.monotonic()
        print(f"Testing real extraction: {len(source)} source characters, {values['language']} output, {values['ai_timeout_seconds']}s budget.", flush=True)
        recipe = app.clean_recipe(ai.extract_recipe(source))
        assert recipe["name"] and recipe["ingredients"]
        if args.cached_source:
            assert all(i.get("source_quote") for i in recipe["ingredients"])
            print(json.dumps({"seconds": round(time.monotonic()-started,1), "ingredients":len(recipe["ingredients"]), "model":recipe["extraction_model"]}),flush=True)
            print("PASS cached-transcript extraction. Source and recipe contents were not printed or saved.",flush=True)
        else:
            assert recipe["servings"] == 2
            expected = {"dau hu":300,"ca chua":200,"dau an":10,"toi":5,"muoi":2}
            ingredients = {store.key(i["name"]):i["grams"] for i in recipe["ingredients"]}
            assert ingredients == expected, (ingredients, expected)
            print("PASS Vietnamese extraction: all five ingredient weights and servings match the source.",flush=True)
        if args.recipe_only:
            return
        store.put("recipes",recipe["id"],recipe)
        started = time.monotonic()
        plan = app.make_plan({})
        assert len(plan["days"]) == len(weekly.calendar()["dates"])
        assert {d["recipe_id"] for d in plan["days"]} == {recipe["id"]}
        print(f"PASS real AI plan: remaining days use saved dishes only ({time.monotonic()-started:.1f}s).",flush=True)


if __name__ == "__main__":
    main()
