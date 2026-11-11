"""Opt-in live network smoke checks. Uses one USDA demo-key request (cached)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import nutrition, store, web

store.initialize()
matches = nutrition.search("broccoli raw")
assert matches and any("broccoli" in m["name"].lower() for m in matches)
assert any(n["id"] == 1162 for m in matches for n in m["nutrients"]), "Expected vitamin C"
print("PASS USDA: sourced calories and vitamin/mineral values per 100 g", flush=True)
article = web.article_text("https://www.bbcgoodfood.com/recipes/easy-pancakes")
assert "pancake" in article.lower() and len(article) > 100
print("PASS article: extracted public recipe", len(article), "characters", flush=True)
sources, warnings = web.guidance_sources("broccoli raw")
print(json.dumps({"source_urls": [s["url"] for s in sources], "warnings": warnings}), flush=True)
assert any("nhs.uk" in s["url"] for s in sources), "Expected retrieved food-specific primary source"
print("PASS guidance: retrieved primary sources online", flush=True)
