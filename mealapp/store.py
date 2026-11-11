import json
import copy
import os
import sqlite3
import unicodedata
import threading
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_LOCAL_DATA = Path(os.environ.get("MEAL_APP_DATA", ROOT / "data"))


class WorkspacePath:
    def path(self):
        from . import hosted
        return hosted.current().path if hosted.enabled() else _LOCAL_DATA

    def __truediv__(self, other):
        return self.path() / other

    def __fspath__(self):
        return str(self.path())

    def __str__(self):
        return str(self.path())

    def __getattr__(self, name):
        return getattr(self.path(), name)


DATA = WorkspacePath()
LOCK = threading.RLock()
REQUEST = threading.local()
EPOCH = 0


def epoch():
    from . import hosted
    return hosted.current().epoch if hosted.enabled() else EPOCH


def bump_epoch():
    global EPOCH
    from . import hosted
    if hosted.enabled():
        hosted.current().epoch += 1
    else:
        EPOCH += 1


def check_epoch():
    if getattr(REQUEST,'epoch',epoch())!=epoch():
        raise ValueError('Data was restored while this request was running. Reload and try again.')


@contextmanager
def request_scope():
    previous=getattr(REQUEST,'epoch',None)
    with LOCK:
        REQUEST.epoch=epoch()
    try:
        yield
    finally:
        if previous is None:
            del REQUEST.epoch
        else:
            REQUEST.epoch=previous


def key(value):
    return " ".join(unicodedata.normalize("NFD", str(value).lower().replace("đ", "d")).encode("ascii", "ignore").decode().split())


@contextmanager
def db():
    with LOCK:
        DATA.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(DATA / "meals.sqlite3", timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            if connection.total_changes:
                check_epoch()
            connection.commit()
        finally:
            connection.close()


def initialize():
    with db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS foods (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS recipes (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS state (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cache (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, body TEXT NOT NULL);
        """)
        seed = json.loads((ROOT / "ingredients.json").read_text(encoding="utf-8"))
        vietnamese_names = json.loads((ROOT / "ingredient_names_vi.json").read_text(encoding="utf-8"))
        animal = {"chicken breast, raw, skinless", "chicken breast, cooked", "chicken thigh, raw, skinless", "lean ground beef, raw", "beef sirloin, raw", "pork tenderloin, raw", "pork belly, raw", "salmon, raw", "tilapia, raw", "shrimp, raw", "egg, whole, raw", "whole milk", "plain yogurt", "cheddar cheese", "butter", "honey", "fish sauce", "oyster sauce"}
        for food in seed:
            food.update(id=key(food["name"]), origin="seed", vegan=food["name"].lower() not in animal, source="https://fdc.nal.usda.gov/", note="Approximate reference; check raw/cooked state and your product label.")
            c.execute("INSERT OR IGNORE INTO foods VALUES (?, ?)", (food["id"], json.dumps(food, ensure_ascii=False)))
            # Add classification to databases created before this field existed; preserve user values.
            existing = json.loads(c.execute("SELECT body FROM foods WHERE id=?", (food["id"],)).fetchone()[0])
            additions = {"vegan": food["vegan"], "vi_name": vietnamese_names[food["name"]]}
            if any(field not in existing for field in additions):
                for field, value in additions.items():
                    existing.setdefault(field, value)
                c.execute("UPDATE foods SET body=? WHERE id=?", (json.dumps(existing, ensure_ascii=False), food["id"]))


def all_items(table):
    assert table in {"foods", "recipes", "plans"}
    with db() as c:
        items = [json.loads(row[0]) for row in c.execute(f"SELECT body FROM {table} ORDER BY rowid")]
        return [normalize_recipe(item) for item in items] if table == "recipes" else items


def get(table, item_id, default=None):
    assert table in {"foods", "recipes", "state", "cache", "plans"}
    with db() as c:
        row = c.execute(f"SELECT body FROM {table} WHERE id=?", (item_id,)).fetchone()
        item = json.loads(row[0]) if row else default
        return normalize_recipe(item) if table == "recipes" and item is not None else item


def recipe_ingredients(recipe):
    if isinstance(recipe.get("ingredients"), list):
        return recipe["ingredients"]
    return [ingredient for group in recipe.get("groups", []) for ingredient in group.get("ingredients", [])]


def normalize_recipe(recipe):
    result = copy.deepcopy(recipe)
    result["ingredients"] = copy.deepcopy(recipe_ingredients(recipe))
    if result.get("groups"):
        result.setdefault("legacy_groups", result["groups"])
    result.pop("groups", None)
    result.setdefault("good_for_running", False)
    return result


def put(table, item_id, body):
    assert table in {"foods", "recipes", "state", "cache", "plans"}
    with db() as c:
        c.execute(f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (item_id, json.dumps(body, ensure_ascii=False)))
    return body


def remove_recipe(item_id):
    with db() as c:
        c.execute("DELETE FROM recipes WHERE id=?", (item_id,))
        # Keep every scheduled date, but discard the deleted dish's snapshot and
        # preparation details in the same transaction as the recipe deletion.
        # Also cover the legacy singleton if it has not yet been migrated.
        for table, query in (("plans", "SELECT id, body FROM plans"),
                             ("state", "SELECT id, body FROM state WHERE id='plan'")):
            for row in c.execute(query).fetchall():
                plan = json.loads(row["body"])
                if not isinstance(plan, dict):
                    continue
                changed = False
                for day in plan.get("days", []):
                    if day.get("recipe_id") == item_id:
                        scheduled = {key: day[key] for key in ("date", "recipe_id") if key in day}
                        day.clear()
                        day.update(scheduled, deleted=True)
                        changed = True
                if changed:
                    c.execute(f"UPDATE {table} SET body=? WHERE id=?", (json.dumps(plan, ensure_ascii=False), row["id"]))


def find_food(name, foods=None):
    query = key(name)
    if not query:
        return None
    for food in foods if foods is not None else all_items("foods"):
        if query in [key(food["name"]), food["id"], key(food.get("vi_name", ""))] + [key(a) for a in food.get("aliases", [])]:
            return food
    return None


def calories(recipe):
    foods = all_items("foods")
    total, missing, rows = 0, [], []
    for ingredient in recipe_ingredients(recipe):
        food = next((f for f in foods if f["id"] == ingredient.get("food_id")), None) if ingredient.get("food_id") else find_food(ingredient.get("name", ""), foods)
        grams = ingredient.get("grams")
        known = food is not None and isinstance(grams, (int, float)) and grams >= 0
        kcal = grams * food["kcal"] / 100 if known else None
        if kcal is not None:
            total += kcal
        else:
            missing.append({"name": ingredient.get("name", "Unnamed"), "reason": "Set weight in grams" if grams is None else "Set calories per 100 g"})
        rows.append({"name": ingredient.get("name"), "kcal": round(kcal, 1) if kcal is not None else None, "food": food})
    servings = recipe.get("servings")
    return {"total": round(total, 1), "per_serving": round(total / servings, 1) if servings and servings > 0 else None,
            "complete": not missing and bool(rows), "missing": missing, "ingredients": rows}
