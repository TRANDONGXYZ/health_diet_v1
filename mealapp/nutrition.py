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


def identify(image):
    data = ai.ask("Identify the primary edible food in this photo. A photo cannot establish composition or weight. "
                  "Give an English search name with visible raw/cooked state, a short uncertainty explanation, and confidence low/medium/high. "
                  "If the photo is not food, return name=''. Do not give nutrition or medical facts.",
                  ai.obj({"name": ai.TEXT, "explanation": ai.TEXT, "confidence": {"type": "string", "enum": ["low", "medium", "high"]}}), image=image, max_tokens=200)
    if not data.get("name"):
        raise ValueError("No food could be identified. Try a clearer photo or enter the food name manually.")
    return data


def guidance(food):
    sources, warnings = web.guidance_sources(food)
    if not sources:
        return {"items": [], "sources": [], "warnings": warnings}
    numbered = [{"id": i + 1, **source} for i, source in enumerate(sources)]
    schema = ai.obj({"items": {"type": "array", "maxItems": 2, "items": ai.obj({"kind": {"type": "string", "enum": ["daily amount", "precaution"]}, "source_id": {"type": "integer", "enum": [s["id"] for s in numbered]}, "quote": {"type": "string", "maxLength": 220}})}})
    items = []
    try:
        output = ai.ask("Find at most TWO short exact source quotations about daily food amounts or precautions relevant to " + food + ". "
                        "Do not prescribe a personal amount. Do not turn a whole food-group recommendation into an amount for this single food. "
                        "Supply a short exact contiguous quote (max 24 words) from its source. "
                        "If no food-specific daily recommendation or precautions are found, omit those facts. "
                        'Example shape: {"items":[{"kind":"precaution","source_id":1,"quote":"Exact short sentence from the source."}]}. '
                        "SOURCES:\n" + json.dumps(numbered, ensure_ascii=False), schema, max_tokens=550)
        for item in output.get("items", [])[:2]:
            source = next((s for s in numbered if s["id"] == item.get("source_id")), None)
            quote = item.get("quote", "").strip()
            if source and quote and len(quote.split()) <= 24 and " ".join(quote.lower().split()) in " ".join(source["text"].lower().split()):
                items.append({**item, "url": source["url"]})
    except ValueError as exc:
        from . import hosted
        warnings.append(str(exc) if hosted.enabled() else "AI evidence selection was unavailable. Matching source quotations are shown when available.")
    if not items:
        # An exact source sentence remains useful even when the small model fails JSON.
        # Never synthesize advice, infer safety, or manufacture a daily food amount.
        words = [w for w in re.findall(r"[a-z]+", food.lower()) if w not in {"raw", "cooked", "fresh", "prepared", "food"}]
        for source in numbered:
            for sentence in re.split(r"(?<=[.!?])\s+|\n+", source["text"]):
                sentence = sentence.strip()
                if not 6 <= len(sentence.split()) <= 24 or not any(re.search(r"\b" + re.escape(w) + r"\b", sentence.lower()) for w in words):
                    continue
                if re.search(r"avoid|allerg|interact|affect|warfarin|pregnan|kidney|medicin", sentence, re.IGNORECASE):
                    items.append({"kind": "precaution", "source_id": source["id"], "quote": sentence, "url": source["url"]})
                    break
            if len(items) >= 2:
                break
    return {"items": items, "sources": numbered, "warnings": warnings}
