"""Run with python app.py. Core app uses only Python's standard library."""
import argparse
import base64
import json
import os
import secrets
import tempfile
import threading
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mealapp import ai, nutrition, store, web, weekly, plan_jobs, dish_photos, backups, hosted

PUBLIC = store.ROOT / "public"
TOKEN = secrets.token_urlsafe(32)


def text(value, maximum=2000):
    return str(value or "").strip()[:maximum]


def clean_recipe(data):
    name = text(data.get("name"), 200)
    if not name:
        raise ValueError("Enter a dish name.")
    vegan = data.get("vegan")
    if vegan is not None and not isinstance(vegan, bool):
        raise ValueError("Vegan status must be yes, no, or unsure.")
    source = text(data.get("source_url"), 2000)
    if source and (urllib.parse.urlsplit(source).scheme not in ("http", "https") or not urllib.parse.urlsplit(source).hostname):
        raise ValueError("Source must be an http or https link.")
    recipe = {"id": text(data.get("id"), 100) or str(uuid.uuid4()), "name": name, "vegan": vegan, "source_url": source,
              "servings": nutrition.number(data.get("servings"), "Servings", 0.01, 10000, True),
              "measured_minutes": nutrition.number(data.get("measured_minutes"), "Measured cooking minutes", 1, 10080, True),
              "estimated_minutes": nutrition.number(data.get("estimated_minutes"), "Estimated minutes", 1, 10080, True),
              "notes": text(data.get("notes"), 5000), "source_text": text(data.get("source_text"), 30000)}
    recipe["content_language"] = data.get("content_language") if data.get("content_language") in ("vi", "en") else None
    recipe["photo_url"] = dish_photos.validate_url(data.get("photo_url", ""))
    running = data.get("good_for_running", False)
    if not isinstance(running, bool):
        raise ValueError("Good for Running must be true or false.")
    recipe["good_for_running"] = running
    recipe["time_source"] = "source" if data.get("time_source") == "source" else "estimated"
    recipe["extraction_model"] = text(data.get("extraction_model"), 150)
    recipe["extraction_warnings"] = [text(w, 1000) for w in data.get("extraction_warnings", [])][:30]
    recipe["ingredients"] = []
    items = store.recipe_ingredients(data)
    if not isinstance(items, list) or len(items) > 200:
        raise ValueError("Use at most 200 ingredients per recipe.")
    for ingredient in items:
        name = text(ingredient.get("name"), 180)
        if not name:
            raise ValueError("Every ingredient needs a name. Remove unused rows.")
        recipe["ingredients"].append({"name":name, "quantity":text(ingredient.get("quantity"), 150),
            "grams":nutrition.number(ingredient.get("grams"), "Ingredient grams", 0, 1_000_000, True),
            "food_id":text(ingredient.get("food_id"), 200), "weight_estimated":bool(ingredient.get("weight_estimated", False)),
            "source_quote":text(ingredient.get("source_quote"), 1500)})
    legacy = data.get("legacy_groups") or data.get("groups")
    if legacy:
        recipe["legacy_groups"] = legacy
    return recipe


def make_plan(data):
    return weekly.build(data)

def post(route, data):
    if route == "/api/dish-photo":
        return dish_photos.save(data)
    if route == "/api/calculate":
        return store.calories(clean_recipe(dict(data, name=data.get("name") or "Draft")))
    if route == "/api/recipes":
        recipe = clean_recipe(data)
        warnings = []
        names = [i["name"] for i in recipe["ingredients"] if not i.get("food_id") and not store.find_food(i["name"])]
        for result in nutrition.resolve(names[:15]):
            if result.get("error"):
                warnings.append(result["name"] + ": " + result["error"])
        store.put("recipes", recipe["id"], recipe)
        return {"recipe": recipe, "calories": store.calories(recipe), "warnings": warnings}
    if route == "/api/recipes/delete":
        with plan_jobs.LOCK:
            store.remove_recipe(text(data.get("id"), 100))
        return {"ok": True}
    if route == "/api/extract":
        return web.extract(text(data.get("url"), 2000), data.get("kind", "auto"))
    if route == "/api/import":
        source_text = text(data.get("text"), 30000)
        if len(source_text) < 30:
            raise ValueError("Paste ingredients and instructions, or retrieve a source first.")
        recipe = ai.extract_recipe(source_text)
        recipe.update(source_url=text(data.get("url"), 2000), source_text=source_text)
        recipe = clean_recipe(recipe)
        resolved = nutrition.resolve([i["name"] for i in recipe["ingredients"]][:30])
        return {"recipe": recipe, "calories": store.calories(recipe), "resolved": resolved}
    if route == "/api/source-language":
        from mealapp.source_language import detect
        lang = detect(text(data.get("text"), 30000))
        settings = ai.config()
        return {"language":lang, "model":settings["vietnamese_model" if lang == "vi" else "english_model"]}
    if route == "/api/recipes/running":
        recipe = store.get("recipes", text(data.get("id"), 100))
        if not recipe:
            raise ValueError("Recipe not found.")
        if not isinstance(data.get("good_for_running"), bool):
            raise ValueError("Good for Running must be true or false.")
        recipe["good_for_running"] = data["good_for_running"]
        store.put("recipes", recipe["id"], recipe)
        return {"recipe":recipe}
    if route == "/api/foods":
        name = text(data.get("name"), 180)
        if not name:
            raise ValueError("Enter a food name.")
        existing = store.get("foods", text(data.get("id"))) if data.get("id") else store.find_food(name)
        food = dict(existing or {}, id=existing["id"] if existing else store.key(name), name=name,
                    kcal=nutrition.number(data.get("kcal"), "Calories per 100 g", 0, 1000),
                    aliases=[text(a, 180) for a in data.get("aliases", [])][:30], origin="manual", note=text(data.get("note"), 1000) or "User-entered value", source="")
        store.put("foods", food["id"], food)
        return food
    if route == "/api/resolve":
        names = data.get("names", [])
        if not isinstance(names, list) or len(names) > 30:
            raise ValueError("Resolve at most 30 ingredient names at a time.")
        return {"results": nutrition.resolve(names)}
    if route == "/api/nutrition/search":
        return {"matches": nutrition.search(text(data.get("query"), 180))}
    if route == "/api/nutrition/identify":
        encoded = data.get("image", "")
        if not isinstance(encoded, str) or len(encoded) > 6_000_000:
            raise ValueError("Use a smaller image (under 4 MB).")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception:
            raise ValueError("Invalid image data.") from None
        if not (raw.startswith(b"\xff\xd8\xff") or raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
            raise ValueError("Use a JPEG, PNG, or WebP photo.")
        return nutrition.identify(encoded)
    if route == "/api/nutrition/guidance":
        query = text(data.get("query"), 180)
        if not query:
            raise ValueError("Enter a food name first.")
        return nutrition.guidance(query)
    if route == "/api/plan/generate":
        if data.get("method") in ("rotation", "random") or data.get("simple"):
            return plan_jobs.instant(data)
        return plan_jobs.start(data)
    if route == "/api/plan/start":
        return plan_jobs.start(data)
    if route == "/api/plan/cancel":
        return plan_jobs.cancel(text(data.get("id"),100))
    if route == "/api/plan/add":
        return plan_jobs.add_week(data)
    if route == "/api/plan/expansion":
        return weekly.save_expansion(data)
    if route == "/api/plan/delete":
        return plan_jobs.edit_week(data,delete=True)
    if route == "/api/plan/replace":
        return plan_jobs.edit_week(data)
    if route == "/api/plan/save":
        raise ValueError("Plans save automatically. Use meal replacement to change a saved date.")
    if route == "/api/settings":
        if hosted.enabled():
            raise ValueError('Hosted AI models and provider keys are managed by the beta operator. Local model settings remain available in local mode.')
        settings = {"text_model": text(data.get("text_model"), 150), "vision_model": text(data.get("vision_model"), 150), "language": "auto", "vietnamese_model": text(data.get("vietnamese_model") or ai.config()["vietnamese_model"], 150), "english_model": text(data.get("english_model") or ai.config()["english_model"], 150)}
        if any(not settings[k] for k in ("text_model", "vision_model", "vietnamese_model", "english_model")):
            raise ValueError("Choose model names.")
        previous = store.get("state", "settings", {})
        settings["usda_api_key"] = text(data.get("usda_api_key"), 150) or previous.get("usda_api_key") or ai.config()["usda_api_key"]
        store.put("state", "settings", settings)
        return {"ok": True}
    raise ValueError("Unknown action.")


class Handler(BaseHTTPRequestHandler):
    def send_response(self, code, message=None):
        self.response_code=code
        super().send_response(code,message)

    def headers_common(self, content_type, cache="no-store"):
        if getattr(self,'session_cookie',None):
            self.send_header('Set-Cookie',self.session_cookie)
            self.session_cookie=None
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")

    def reply(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(code)
        self.headers_common("application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def local_request(self):
        if hosted.enabled():return hosted.origin_allowed(self)
        host = self.headers.get("Host", "")
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return host in allowed and (not origin or origin in {"http://" + h for h in allowed})

    def do_GET(self):
        if hosted.enabled():return hosted.serve(self,self.get_request)
        return self.get_request()

    def get_request(self):
        if not self.local_request():
            return self.reply({"error": "Only local same-origin access is allowed."}, 403)
        route = urllib.parse.urlsplit(self.path).path
        if route == "/api/state":
            offset = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("timezone_offset",[None])[0]
            try:
                calendar = weekly.calendar(offset)
            except ValueError as exc:
                return self.reply({"error":str(exc)},400)
            active_job = plan_jobs.active()
            with store.LOCK:
                plans = weekly.history()
                recipes = store.all_items("recipes")
                settings = ai.config()
                payload={"token": hosted.csrf() if hosted.enabled() else TOKEN, "hosted":hosted.enabled(), "recipes": [dict(r, calories=store.calories(r)) for r in recipes], "foods": store.all_items("foods"), "plan":next((p for p in plans if p["week_start"]==calendar["week_start"]),None),
                                   "plans":plans,"calendar":calendar,"plan_job":active_job,"week_expansion":store.get('state','week_expansion',{}),
                                   "settings": {k: settings[k] for k in ("text_model", "vision_model", "vietnamese_model", "english_model", "language")}, "has_usda_key": settings["usda_api_key"] != "DEMO_KEY"}
            return self.reply(payload)
        if route == "/api/status":
            return self.reply(ai.status())
        if route == "/api/ai-progress":
            return self.reply(ai.progress())
        if route == "/api/plan/job":
            job_id = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id",[""])[0]
            try:
                return self.reply(plan_jobs.get(job_id))
            except ValueError as exc:
                return self.reply({"error":str(exc)},404)
        if route == "/api/backup/export":
            try:
                body = backups.export_zip()
            except (ValueError,OSError) as exc:
                return self.reply({"error":str(exc)},400)
            self.send_response(200)
            self.headers_common("application/zip")
            self.send_header("Content-Disposition",'attachment; filename="meal-notebook-backup.zip"')
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if route == "/api/export":
            return self.reply({"version": 2, "recipes": store.all_items("recipes"), "foods": store.all_items("foods"), "plans":weekly.history()})
        if route.startswith("/media/"):
            with store.LOCK:
                path = dish_photos.path_for(route)
                if not path or not path.is_file():
                    return self.reply({"error":"Photo not found."},404)
                body = path.read_bytes()
            self.send_response(200)
            self.headers_common("image/png","private, max-age=31536000, immutable")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        filename = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/style.css": "style.css"}.get(route)
        if not filename:
            return self.reply({"error": "Not found"}, 404)
        body = (PUBLIC / filename).read_bytes()
        self.send_response(200)
        self.headers_common({"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8"}[filename.rsplit(".", 1)[1]])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if hosted.enabled():return hosted.serve(self,self.post_request)
        return self.post_request()

    def post_request(self):
        if not self.local_request() or self.headers.get("X-App-Token") != (hosted.csrf() if hosted.enabled() else TOKEN):
            return self.reply({"error": "Reload the page before saving. Only local same-origin access is allowed."}, 403)
        try:
            with store.request_scope():
                length = int(self.headers.get("Content-Length", 0))
                if urllib.parse.urlsplit(self.path).path == '/api/transcribe':
                    if not 0 < length <= 24*1024*1024:
                        return self.reply({'error':'Choose an audio/video file under 24 MB.'},413)
                    filename=self.headers.get('X-File-Name','')
                    suffix=Path(filename).suffix.lower()
                    if suffix not in ('.mp3','.mp4','.mpeg','.mpga','.m4a','.ogg','.wav','.webm','.flac'):
                        return self.reply({'error':'Choose an MP3, MP4, M4A, OGG, WAV, WebM, MPEG or FLAC file you are allowed to use.'},400)
                    from mealapp import speech
                    with tempfile.TemporaryDirectory(prefix='health-upload-') as folder:
                        path=Path(folder)/('upload'+suffix)
                        path.write_bytes(self.rfile.read(length))
                        transcript=speech.transcribe_file(path,folder)
                    return self.reply({'text':transcript})
                if urllib.parse.urlsplit(self.path).path == "/api/backup/import":
                    limit=48*1024*1024 if hosted.enabled() else backups.MAX_ARCHIVE
                    if not 0 < length <= limit:
                        return self.reply({"error":"Choose a ZIP backup under "+('48' if hosted.enabled() else '256')+" MB."},413)
                    if self.headers.get("Content-Type",'').split(';')[0] not in ('application/zip','application/octet-stream'):
                        return self.reply({"error":"Upload a ZIP backup."},415)
                    return self.reply(backups.import_zip(self.rfile.read(length)))
                if not 0 < length <= 7_000_000:
                    return self.reply({"error": "Request is empty or too large."}, 413)
                if "application/json" not in self.headers.get("Content-Type", ""):
                    return self.reply({"error": "JSON required"}, 415)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected a JSON object.")
                result = post(urllib.parse.urlsplit(self.path).path, data)
                self.reply(result)
        except (ValueError, TypeError, KeyError) as exc:
            self.reply({"error": str(exc)}, 400)
        except Exception:
            traceback.print_exc()
            self.reply({"error": "The operation failed. Check the server window for details; your saved data is retained."}, 500)

    def log_message(self, fmt, *args):
        # Avoid printing source URLs, images, API keys or recipe bodies.
        pass


def main():
    parser = argparse.ArgumentParser(description="Local recipe and weekly meal planner")
    parser.add_argument("--port", type=int, default=int(os.environ.get('PORT',8765)))
    parser.add_argument("--host", default='0.0.0.0' if hosted.enabled() else '127.0.0.1')
    parser.add_argument("--open", action="store_true", help="Open your default browser")
    args = parser.parse_args()
    if hosted.enabled():hosted.validate_environment()
    else:store.initialize()
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError:
        print(f"Port {args.port} is busy. Close the other app, or run: python app.py --port 8766 --open")
        return
    url = f"http://127.0.0.1:{args.port}"
    print(f"Meal Notebook is running on {args.host}:{args.port}\nData: {'Supabase per-tester storage' if hosted.enabled() else store.DATA}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
