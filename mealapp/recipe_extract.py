"""One-pass, source-grounded ingredient extraction in the source's language."""
import json
import math
import re
import unicodedata

from . import ai, source_language, store


def normalized(text):
    return " ".join(unicodedata.normalize("NFC", str(text)).casefold().split())


def mentions(name, text):
    return bool(re.search(r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)", normalized(text)))


def structured_recipe(source):
    try:
        data = json.loads(source)
        if isinstance(data, dict):
            data = [data]
        return next((item for item in data if isinstance(item, dict) and isinstance(item.get("recipeIngredient"), list)), None)
    except (ValueError, TypeError):
        return None


def source_text(source):
    try:
        data = json.loads(source)
        if isinstance(data, (dict, list)):
            return "\n".join(source_language.text_values(data))
    except ValueError:
        pass
    return source


def source_chunks(source):
    """Short numbered excerpts preserve native text without generating quote copies."""
    chunks = []
    for part in re.split(r"\n+|(?<=[.!?;])\s+", source_text(source)):
        part = part.strip()
        while len(part) > 320:
            end = part.rfind(" ", 0, 320)
            if end < 100:
                end = 320
            chunks.append(part[:end])
            # Overlap keeps a name/amount split at a boundary available together.
            start = part.rfind(" ", max(1,end-80), end-40)
            part = part[start+1 if start >= 0 else end-48:].strip()
        if part:
            chunks.append(part)
    return chunks


NUMBER = r"\d+(?:[.,/]\d+)?"
MASS = rf"{NUMBER}\s*(?:kilograms?|kg|grams?|gam|gr|g)\b(?:\s+{NUMBER}\s*(?:grams?|gam|gr|g)\b)?"


def mass_pairs(name, source):
    food = r"(?<!\w)" + re.escape(normalized(name)) + r"(?!\w)"
    patterns = [rf"(?<![\w/]){MASS}\s*(?:of\s+)?{food}", rf"{food}\s*[:\uff1a-]?\s*{MASS}"]
    return [m[0] for pattern in patterns for m in re.finditer(pattern, normalized(source))]


def grams_from_quote(quote, name, language):
    """Use mass directly beside the ingredient, never a neighboring food's mass."""
    pairs = mass_pairs(name, quote)
    if not pairs:
        return None
    pair = pairs[0]
    total = 0
    for item in re.finditer(rf"(?<![\w/])({NUMBER})\s*(kilograms?|kg|grams?|gam|gr|g)\b", pair):
        raw = item[1]
        if "/" in raw:
            numerator, denominator = raw.split("/")
            if not float(denominator):
                return None
            amount = float(numerator) / float(denominator)
        else:
            if language == "en" and re.fullmatch(r"\d{1,3},\d{3}", raw):
                raw = raw.replace(",", "")
            elif language == "vi" and re.fullmatch(r"\d{1,3}\.\d{3}", raw):
                raw = raw.replace(".", "")
            amount = float(raw.replace(",", "."))
        total += amount * (1000 if item[2].startswith("k") else 1)
    return total


def source_mass_quote(name, source, language):
    """Recover a literal mass/name pair when a small model quotes only the name."""
    matches = mass_pairs(name, source_text(source))
    weights = {grams_from_quote(m, name, language) for m in matches}
    return matches[0] if matches and len(weights) == 1 else None


def known_name(row, foods):
    candidates = []
    for food in foods:
        for label in [food["name"], food.get("vi_name", "")] + food.get("aliases", []):
            if not label:
                continue
            match = re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", row, re.IGNORECASE)
            if match:
                candidates.append((len(match[0]), match[0]))
    return max(candidates, default=(0, ""))[1]


def source_row_ingredient(row, foods, language):
    name = known_name(row, foods)
    if not name:
        name = re.sub(r"^\s*\d+(?:[.,/]\d+)?\s*(?:(?:kg|g|grams?|ml|tbsp|tsp|cups?|tablespoons?|teaspoons?)\b\s*)?(?:of\s+)?", "", row, flags=re.IGNORECASE).strip() or row
    grams = grams_from_quote(row, name, language)
    return {"name":name, "quantity":row, "grams":grams, "source_quote":row, "weight_estimated":False, "food_id":""}


def validate_ingredients(items, source, language, structured_rows=None):
    evidence = normalized(source_text(source))
    foods = store.all_items("foods")
    accepted, rejected, seen = [], [], set()
    for item in items:
        name = str(item.get("name", "")).strip()
        quote = str(item.get("source_quote", "")).strip().strip('"\'“”‘’').strip()
        if not name or not quote or normalized(quote) not in evidence or not mentions(name, quote):
            rejected.append(name or "Unnamed ingredient")
            continue
        matching_rows = [str(row) for row in structured_rows or [] if mentions(name, str(row))]
        if matching_rows:
            # Bind a broad/name-only model excerpt to one authoritative row so
            # repeated ingredient rows retain their separate amounts exactly once.
            specific = [row for row in matching_rows if normalized(quote) in normalized(row) and re.search(r"\d", quote)]
            quote = (specific or matching_rows)[0]
        food = store.find_food(name, foods)
        identity = (food["id"] if food else normalized(name), normalized(quote) if structured_rows else "")
        if identity in seen:
            continue
        seen.add(identity)
        if grams_from_quote(quote, name, language) is None:
            # The replacement excerpt is still copied verbatim from this source.
            quote = source_mass_quote(name, source, language) or quote
        explicit = grams_from_quote(quote, name, language)
        quantity = str(item.get("quantity", "")).strip()
        quantity_grounded = bool(quantity) and normalized(quantity).replace(" ", "") in normalized(quote).replace(" ", "")
        if quantity and not quantity_grounded:
            quantity = quote if len(quote) <= 70 and re.search(r"\d", quote) else ""
        grams = item.get("grams")
        valid = isinstance(grams, (int, float)) and not isinstance(grams, bool) and math.isfinite(grams) and 0 <= grams <= 1_000_000
        # No stated numeric amount means no invented weight, even for a familiar food.
        if not valid or not quantity_grounded or not re.search(r"\d", quote):
            grams = None
        if explicit is not None:
            grams = explicit
        accepted.append({"name":name, "quantity":quantity, "grams":grams, "weight_estimated":grams is not None and explicit is None,
                         "source_quote":quote, "food_id":food["id"] if food else ""})
    # Structured ingredient lists are authoritative: keep every original row even if the model omits it.
    for row in dict.fromkeys(map(str, structured_rows or [])):
        row = str(row).strip()
        if not row or any(mentions(i["name"], row) and normalized(i["source_quote"]) in normalized(row) for i in accepted):
            continue
        item = source_row_ingredient(row, foods, language)
        food = store.find_food(item["name"], foods)
        identity = (food["id"] if food else normalized(item["name"]), normalized(row))
        if identity not in seen:
            item["food_id"] = food["id"] if food else ""
            accepted.append(item); seen.add(identity)
    if not structured_rows:
        # A small model can omit even a plainly measured ingredient. Recover only
        # literal mass/name pairs for known foods; no inferred cooking additions.
        candidates = []
        for food in foods:
            for label in [food["name"], food.get("vi_name", "")] + food.get("aliases", []):
                if label:
                    candidates.extend((quote, normalized(label), food) for quote in mass_pairs(label, source_text(source)))
        included = {i["food_id"] for i in accepted if i.get("food_id")}
        recovered_quotes = []
        for quote, name, food in sorted(candidates, key=lambda item: len(item[0]), reverse=True):
            if food["id"] in included or any(quote in prior for prior in recovered_quotes):
                continue
            grams = grams_from_quote(quote, name, language)
            if grams is None:
                continue
            accepted.append({"name":name, "quantity":quote, "grams":grams, "source_quote":quote,
                             "weight_estimated":False, "food_id":food["id"]})
            included.add(food["id"]); recovered_quotes.append(quote)
    if not accepted:
        raise ValueError("No ingredients could be verified against the source text. Check the source or enter the ingredient list manually.")
    return accepted, rejected


def stated_time(source, structured):
    if structured:
        for key in ("totalTime", "cookTime"):
            duration = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", str(structured.get(key, "")), re.I)
            if duration and (duration[1] or duration[2]):
                return (int(duration[1] or 0) * 60 + int(duration[2] or 0)) or None
    match = re.search(r"(?:total\s+(?:cooking\s+)?time|cook(?:ing)?\s+time|tổng\s+thời\s+gian|thời\s+gian\s+nấu)\s*[:：-]?\s*(\d+)\s*(minutes?|mins?|phút)", source_text(source), re.I)
    return (int(match[1]) or None) if match else None


def stated_servings(source, structured):
    value = structured.get("recipeYield") if structured else None
    if isinstance(value, (str, int, float)) and re.fullmatch(r"\s*\d+(?:\.\d+)?(?:\s+(?:servings?|people|người))?\s*", str(value), re.I):
        return float(re.search(r"\d+(?:\.\d+)?", str(value))[0])
    match = re.search(r"(?:serves?|servings?|khẩu\s+phần)\s*[:：-]?\s*(\d+)|\b(\d+)\s*(?:người|servings?|people)\b", source_text(source), re.I)
    return float(match[1] or match[2]) if match else None


def extract(source):
    if len(source) > 16000:
        raise ValueError("Trim the source to the recipe's ingredients and relevant text (under 16,000 characters).")
    language = source_language.detect(source)
    settings = ai.config()
    model = settings["vietnamese_model" if language == "vi" else "english_model"]
    label = "Vietnamese" if language == "vi" else "English"
    chunks = source_chunks(source)
    ingredient = ai.obj({"name":{"type":"string", "maxLength":100}, "quantity":{"type":"string", "maxLength":70}, "grams":ai.NULL_NUMBER,
                         "source_id":{"type":"integer", "enum":list(range(1,len(chunks)+1))}})
    schema = ai.obj({"name":{"type":"string", "maxLength":200}, "vegan":{"type":["boolean","null"]},
                     "ingredients":{"type":"array", "maxItems":80, "items":ingredient}})
    task = ("Extract ONE recipe's facts and a SINGLE ingredient list. Do not generate cooking steps, instructions, additions or substitutions. "
                    "Read the source directly in its original language; do not translate any recipe text. Copy each ingredient name from the source. "
                    "Include ALL ingredients actually mentioned, once each with the whole-recipe amount, including seasonings only if stated. "
                    "Every ingredient needs source_id: the number of the source excerpt that contains its exact name and quantity. Return the ID only, do not repeat the excerpt. "
                    "Never infer an ingredient from a dish name or cooking method. No oil, water, salt or garnish unless mentioned. "
                    "quantity copies the stated amount; grams converts g/kg exactly, or estimates count/volume weights only when reasonable. "
                    "Unknown weights stay null (quantity may be empty). "
                    "Use the source's dish name when available. Vegan status is null when uncertain. Output MINIFIED concise JSON, no indentation or extra whitespace. ")
    result = ai.ask(task + "\nNUMBERED SOURCE EXCERPTS:\n" + "\n".join(f"[{i}] {chunk}" for i,chunk in enumerate(chunks,1)),
                    schema, language=label, model=model, max_tokens=2600)
    for item in result.get("ingredients", []):
        index = item.get("source_id")
        if isinstance(index, int) and not isinstance(index, bool) and 1 <= index <= len(chunks):
            item["source_quote"] = chunks[index-1]
        name = str(item.get("name", "")).strip()
        if name and "source_id" in item and not mentions(name,item.get("source_quote", "")):
            matches = [chunk for chunk in chunks if mentions(name,chunk)]
            quantity = normalized(item.get("quantity", ""))
            matching_amount = [chunk for chunk in matches if quantity and quantity in normalized(chunk)]
            item["source_quote"] = (matching_amount or matches or [""])[0]
    structured = structured_recipe(source)
    ingredients, rejected = validate_ingredients(result.get("ingredients", []), source, language, structured.get("recipeIngredient") if structured else None)
    if structured and structured.get("name"):
        result["name"] = str(structured["name"])
    # Keep only recipe facts, regardless of any unexpected model fields.
    recipe = {"name":result.get("name", ""), "vegan":result.get("vegan"), "servings":stated_servings(source, structured),
              "estimated_minutes":stated_time(source, structured), "time_source":"source", "ingredients":ingredients,
              "notes":"", "content_language":language, "extraction_model":model, "good_for_running":False,
              "extraction_warnings":["Rejected ingredients without matching source evidence: " + ", ".join(rejected)] if rejected else []}
    matched = [store.find_food(i["name"]) for i in ingredients]
    if any(f and f.get("vegan") is False for f in matched):
        recipe["vegan"] = False
    elif matched and all(f and f.get("vegan") is True for f in matched):
        recipe["vegan"] = True
    return recipe
