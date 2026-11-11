"""Opt-in real native-language extraction checks. Never saves recipe data."""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import ai, recipe_extract, source_language, store

parser=argparse.ArgumentParser()
parser.add_argument("--facebook-url")
parser.add_argument("--facebook-only",action="store_true")
parser.add_argument("--vietnamese-model",help="Compare an installed local model without changing saved settings")
args=parser.parse_args()
values=ai.config()
if args.vietnamese_model:
    values["vietnamese_model"]=args.vietnamese_model
sources=[("English article",json.dumps({"name":"Fried tofu", "recipeYield":"2 servings", "recipeIngredient":["300 g tofu","40 g garlic","5 g ginger","10 g vegetable oil"],"recipeInstructions":"Cut tofu. Heat the oil and cook the garlic, ginger and tofu."}),"en"),
         ("Vietnamese text","Đậu hũ sốt cà chua. Khẩu phần: 2 người. Nguyên liệu: 300 g đậu hũ, 200 g cà chua, 10 g dầu ăn, 5 g tỏi, 2 g muối. Băm tỏi và cắt cà chua. Chiên đậu hũ với dầu ăn trong 8 phút. Phi tỏi, thêm cà chua và muối, nấu 5 phút. Thêm đậu hũ và nấu thêm 3 phút.","vi")]
if args.facebook_url:
    store.initialize()
    with store.db() as connection:
        cached=connection.execute("SELECT body FROM cache WHERE id LIKE ?",("speech:v1:"+args.facebook_url+":%",)).fetchall()
    assert cached,"Retrieve/transcribe the Facebook video first"
    source=json.loads(cached[0][0])["text"]
    sources.append(("Facebook audio transcript",source,source_language.detect(source)))
if args.facebook_only:
    assert args.facebook_url,"--facebook-only requires --facebook-url"
    sources=sources[-1:]
with tempfile.TemporaryDirectory() as folder,patch.object(store,"DATA",Path(folder)),patch.object(ai,"config",return_value=values):
    store.initialize()
    for label,source,language in sources:
        started=time.monotonic()
        print(f"Testing {label}: {len(source)} source characters; {language}.",flush=True)
        result=app.clean_recipe(ai.extract_recipe(source))
        assert result["ingredients"] and "groups" not in result
        assert result["content_language"]==language
        assert result["extraction_model"]==values["english_model" if language=="en" else "vietnamese_model"]
        evidence=recipe_extract.normalized(recipe_extract.source_text(source))
        for item in result["ingredients"]:
            assert recipe_extract.normalized(item["source_quote"]) in evidence
            assert recipe_extract.mentions(item["name"],item["source_quote"])
        expected={"tofu":300,"garlic":40,"ginger":5,"vegetable oil":10} if label=="English article" else {"đậu hũ":300,"cà chua":200,"dầu ăn":10,"tỏi":5,"muối":2} if label=="Vietnamese text" else None
        if expected:
            grams={store.key(i["name"]):i["grams"] for i in result["ingredients"]}
            assert grams=={store.key(k):v for k,v in expected.items()},(grams,expected)
            assert result["servings"]==2
            assert store.calories(result)["complete"]
        print(f"PASS {label}: model={result['extraction_model']}, {len(result['ingredients'])} verified ingredients, {time.monotonic()-started:.1f}s; no recipe saved.",flush=True)
