import json
import math
import re
import urllib.parse
from datetime import datetime, timezone

from . import ai, store, web


def number(value, label, minimum=0, maximum=1_000_000, nullable=False):
    if nullable and (value is None or value == ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be a number.") from None
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return result


def search(query):
    query = str(query).strip()[:180]
    if not query:
        raise ValueError("Enter an English food name, including raw or cooked state.")
    cached = store.get("cache", "usda:" + store.key(query))
    if cached:
        return cached
    params = urllib.parse.urlencode({"api_key": ai.config()["usda_api_key"], "query": query, "pageSize": 12, "dataType": "SR Legacy,Foundation"})
    try:
        data = json.loads(web.fetch("https://api.nal.usda.gov/fdc/v1/foods/search?" + params))
    except Exception as exc:
        raise ValueError("USDA lookup is unavailable or rate-limited. Try later, add a free USDA key in Settings, or enter a label's kcal per 100 g manually.") from exc
    matches = []
    for food in data.get("foods", []):
        nutrients = [{"id": n.get("nutrientId"), "name": n.get("nutrientName"), "amount": n.get("value"), "unit": n.get("unitName")} for n in food.get("foodNutrients", []) if isinstance(n.get("value"), (int, float))]
        energy = next((n for nid in (1008, 2048, 2047) for n in nutrients if n["id"] == nid and n["unit"].lower() == "kcal"), None)
        if energy is None:
            continue
        matches.append({"fdc_id": food["fdcId"], "name": food["description"], "kcal": energy["amount"], "nutrients": nutrients,
                        "source": f"https://fdc.nal.usda.gov/food-details/{food['fdcId']}/nutrients", "retrieved_at": datetime.now(timezone.utc).isoformat()})
    # Keep USDA's ranking, with explicit state agreement preferred.
    state = "cooked" if "cooked" in query.lower() and "uncooked" not in query.lower() else "raw" if "raw" in query.lower() else None
    if state:
        matches.sort(key=lambda m: state not in m["name"].lower())
    store.put("cache", "usda:" + store.key(query), matches)
    return matches


def resolve(names):
    result = []
    for name in dict.fromkeys(str(n).strip()[:180] for n in names if str(n).strip()):
        existing = store.find_food(name)
        if existing:
            result.append({"name": name, "food": existing})
            continue
        query = name
        try:
            if re.search(r"[^\x00-\x7f]", name):
                translated = ai.ask("Translate this ingredient into a short English USDA food search query, preserving raw/cooked state. Do not add ingredients: " + name, ai.obj({"query": ai.TEXT}), max_tokens=100)
                query = str(translated.get("query", name))
            matches = search(query)
            if not matches:
                raise ValueError("No suitable USDA calorie record found. Enter a value manually.")
            match = matches[0]
            food = dict(match, id=store.key(name), name=name, aliases=[], origin="online", note=f"Automatic USDA match: {match['name']}. Review food and raw/cooked state.", matched_name=match["name"])
            store.put("foods", food["id"], food)
            result.append({"name": name, "food": food})
        except Exception as exc:
            result.append({"name": name, "error": str(exc)})
    return result
