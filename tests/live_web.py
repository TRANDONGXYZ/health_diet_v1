"""Opt-in live network smoke checks. Uses one USDA demo-key request (cached)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import nutrition, store, web

store.initialize()
matches = nutrition.search("broccoli raw")
assert matches and any("broccoli" in m["name"].lower() for m in matches)
assert all(isinstance(m["kcal"],(int,float)) for m in matches)
print("PASS USDA: sourced ingredient calories per 100 g", flush=True)
article = web.article_text("https://www.bbcgoodfood.com/recipes/easy-pancakes")
assert "pancake" in article.lower() and len(article) > 100
print("PASS article: extracted public recipe", len(article), "characters", flush=True)
