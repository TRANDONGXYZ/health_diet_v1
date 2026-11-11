"""Opt-in Facebook captions/audio smoke test; prints counts only and saves no recipe."""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import store, web

parser = argparse.ArgumentParser()
parser.add_argument("url")
args = parser.parse_args()
assert web.facebook_video(args.url), "Expected a Facebook video, Reel, or share URL"
store.initialize()
started = time.monotonic()
print("Testing Facebook video retrieval (captions first, then local audio transcription).", flush=True)
result = web.extract(args.url)
assert result["method"] in {"captions", "audio"} and result["text"].strip()
print(f"PASS Facebook: {result['method']}, {len(result['text'])} transcript characters in {time.monotonic()-started:.1f}s. No recipe was saved.", flush=True)
