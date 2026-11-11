"""Bounded public web fetches and recipe/transcript extraction."""
import concurrent.futures
import html
import ipaddress
import json
import re
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path


def public_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
        raise ValueError("Enter a public http or https link using a standard web port.")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Links to local or private network addresses are not supported.")
    return url


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, timeout=25, maximum=3_000_000):
    public_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "LocalMealPlanner/1.0 (personal recipe reader)", "Accept": "text/html,application/json,text/plain,*/*"})
    with urllib.request.build_opener(PublicRedirect()).open(request, timeout=timeout) as response:
        raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError("Source is too large. Paste the recipe text instead.")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace")


class Article(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.structured, self.scripts = [], [], []
        self.ignored = 0
        self.in_json = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.ignored += 1
        if tag == "script" and "ld+json" in attrs.get("type", ""):
            self.in_json = True
            self.scripts = []
        if tag in ("p", "li", "div", "h1", "h2", "h3", "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.in_json:
            try:
                self.structured.append(json.loads("".join(self.scripts)))
            except ValueError:
                pass
            self.in_json = False
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if self.in_json:
            self.scripts.append(data)
        elif not self.ignored:
            self.parts.append(data)

    def text(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def recipe_nodes(value):
    if isinstance(value, list):
        for item in value:
            yield from recipe_nodes(item)
    if isinstance(value, dict):
        types = value.get("@type", [])
        if types == "Recipe" or isinstance(types, list) and "Recipe" in types:
            yield value
        for item in value.values():
            if isinstance(item, (list, dict)):
                yield from recipe_nodes(item)


def article_text(url):
    parser = Article()
    parser.feed(fetch(url))
    recipes = list(recipe_nodes(parser.structured))
    if recipes:
        fields = ("name", "description", "recipeYield", "prepTime", "cookTime", "totalTime", "recipeIngredient", "recipeInstructions", "suitableForDiet")
        # Prefer the structured recipe over navigation, advertising, or a long blog introduction.
        return json.dumps([{k: r[k] for k in fields if k in r} for r in recipes], ensure_ascii=False)
    text = parser.text()
    if len(text) < 100:
        raise ValueError("The article did not expose recipe text. Paste its ingredients and instructions instead.")
    return text[:28000]


def youtube_id(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        value = p.path.strip("/").split("/")[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"}:
        value = urllib.parse.parse_qs(p.query).get("v", [""])[0] if p.path == "/watch" else p.path.strip("/").split("/")[-1]
    else:
        return None
    return value if re.fullmatch(r"[\w-]{11}", value) else None


def facebook_video(url):
    """Recognize video, Reel and share URLs without accepting lookalike domains."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in {"fb.watch", "www.fb.watch"}:
        return bool(parsed.path.strip("/"))
    if host != "facebook.com" and not host.endswith(".facebook.com"):
        return False
    path = parsed.path.lower()
    query = urllib.parse.parse_qs(parsed.query)
    return bool(re.search(r"/(?:videos?|reels?)(?:/|\.php|$)", path) or
                path.startswith(("/watch", "/share/v/", "/share/r/", "/plugins/video.php")) or
                path == "/story.php" and "v" in query)


def clean_captions(text):
    lines, seen = [], set()
    # yt-dlp requests VTT; avoid importing timestamp and cue metadata into the recipe.
    for line in text.splitlines():
        line = re.sub(r"<[^>]+>", "", html.unescape(line)).strip()
        if not line or "-->" in line or line.isdigit() or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if line not in seen:
            lines.append(line)
            seen.add(line)
    return "\n".join(lines)


def usable_captions(text):
    normalized = " ".join(text.lower().split()).strip(" .…![]")
    if not normalized:
        return False
    if len(normalized) < 250 and any(message in normalized for message in (
        "caption is updating", "captions are updating", "subtitles are updating",
        "caption is being updated", "captions are being updated", "subtitles are being updated",
        "phụ đề đang cập nhật", "đang cập nhật phụ đề",
    )):
        return False
    return True


def youtube_captions(video_id):
    from requests import Session
    from youtube_transcript_api import YouTubeTranscriptApi

    class BoundedSession(Session):
        def request(self, *args, **kwargs):
            kwargs.setdefault("timeout", (10, 20))
            return super().request(*args, **kwargs)

    with BoundedSession() as session:
        available = list(YouTubeTranscriptApi(http_client=session).list(video_id))
        if not available:
            return ""
        chosen = next((t for lang in ("vi", "en") for t in available if t.language_code.split("-")[0] == lang), available[0])
        return "\n".join(item.text for item in chosen.fetch())


def video_source(url):
    public_url(url)
    from . import hosted
    if hosted.enabled() and not (youtube_id(url) or facebook_video(url)):
        raise ValueError('The beta retrieves only public YouTube and Facebook videos. Paste a transcript or upload an audio/video file you are allowed to use.')
    video_id = youtube_id(url)
    if video_id:
        try:
            text = youtube_captions(video_id)
            if usable_captions(text):
                return {"text": text, "method": "captions"}
        except Exception:
            pass
    # Try subtitles from other public sites, or a second caption reader for YouTube.
    try:
        with tempfile.TemporaryDirectory(prefix="meal-captions-") as folder:
            command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "vi,en", "--sub-format", "vtt", "--socket-timeout", "15", "--retries", "0", "--paths", folder, "-o", "captions.%(ext)s", "--", url]
            subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=65, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            files = sorted(Path(folder).glob("*.vtt"), key=lambda f: (".vi." not in f.name, f.name))
            for file in files:
                text = clean_captions(file.read_text(encoding="utf-8-sig"))
                if usable_captions(text):
                    return {"text": text, "method": "captions"}
    except (OSError, subprocess.TimeoutExpired):
        pass
    from .speech import transcribe_video
    return {"text": transcribe_video(url), "method": "audio"}


def video_text(url):
    return video_source(url)["text"]


def extract(url, kind="auto"):
    is_video = kind == "video" or youtube_id(url) is not None or facebook_video(url)
    source = video_source(url) if is_video else {"text": article_text(url), "method": "article"}
    text = source["text"]
    if not text.strip():
        raise ValueError("The source returned no text. Paste the recipe or transcript instead.")
    from . import hosted
    kind = ("audio transcript" if hosted.enabled() else "locally generated audio transcript") if source["method"] == "audio" else "video transcript" if is_video else "article"
    return {"text": text, "kind": kind, "method": source["method"], "url": url}


TRUSTED = ("nhs.uk", "ods.od.nih.gov", "niddk.nih.gov", "fda.gov", "who.int", "myplate.gov", "foodsafety.gov")


def trusted(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in TRUSTED)


class SearchLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        link = dict(attrs).get("href", "")
        if "uddg=" in link:
            link = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("uddg", [""])[0]
        if link.startswith("http") and trusted(link) and link not in self.links:
            self.links.append(link)


def guidance_sources(food):
    """Fetch current primary sources. Missing sources remain explicit, never fabricated."""
    links = ["https://www.who.int/news-room/fact-sheets/detail/healthy-diet"]
    # Known primary-source entry points keep common-food guidance useful when search is blocked.
    # Their contents are still fetched live and must support any displayed claim.
    words = set(re.findall(r"[a-z]+", food.lower()))
    if words & {"broccoli", "spinach", "kale", "lettuce", "cabbage", "grapefruit", "cranberry"}:
        links.append("https://www.nhs.uk/medicines/warfarin/")
    if words & {"milk", "egg", "eggs", "peanut", "peanuts", "soy", "soya", "tofu", "chickpeas", "almonds", "cashews", "wheat", "shrimp", "fish", "salmon", "sesame"}:
        links.append("https://www.nhs.uk/conditions/food-allergy/")
    if words & {"fish", "salmon", "tuna", "shrimp", "tilapia", "mackerel", "sardines", "cod"}:
        links.append("https://www.fda.gov/food/consumers/questions-answers-fdaepa-advice-about-eating-fish-those-who-might-become-or-are-pregnant-or")
    warnings = []
    try:
        query = urllib.parse.quote_plus(food + " food daily portion precautions site:nhs.uk OR site:ods.od.nih.gov OR site:niddk.nih.gov OR site:fda.gov")
        parser = SearchLinks()
        parser.feed(fetch("https://html.duckduckgo.com/html/?q=" + query, timeout=15))
        links.extend(parser.links[:2])
        if not parser.links:
            warnings.append("Online search returned no additional primary sources. Available reference pages are shown below.")
    except Exception:
        warnings.append("Online source search was unavailable. Available reference pages are shown below.")

    def read(link):
        try:
            parser = Article()
            parser.feed(fetch(link, timeout=15))
            text = parser.text()
            if len(text) < 300:
                return None
            # Select context around food and safety words rather than a page's navigation.
            lines = text.splitlines()
            food_words = [w for w in re.findall(r"[a-z]+", food.lower()) if w not in {"raw", "cooked", "fresh", "prepared", "food"}]
            safety_words = ["daily", "portion", "avoid", "allerg", "pregnan", "kidney", "400", "salt"]
            hits = [i for i, line in enumerate(lines) if any(re.search(r"\b" + re.escape(w) + r"\b", line.lower()) for w in food_words)]
            hits += [i for i, line in enumerate(lines) if any(w in line.lower() for w in safety_words)]
            relevant, used = [], set()
            for i in hits:
                for index in range(max(0, i - 1), min(len(lines), i + 3)):
                    if index not in used:
                        relevant.append(index)
                        used.add(index)
            excerpt = "\n".join(lines[i] for i in relevant)[:3000] or text[:3000]
            return {"url": link, "text": excerpt}
        except Exception:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        sources = [result for result in pool.map(read, list(dict.fromkeys(links))[:4]) if result]
    if not sources:
        warnings.append("No guidance pages could be retrieved. Guidance is unavailable; try again with an internet connection.")
    return sources, warnings
