"""Opt-in public caption retrieval check. Prints metadata only, never transcript content."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import web

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=aircAruvnKk"
transcript = web.video_text(url)
assert len(transcript) > 100
print(f"PASS video captions: retrieved {len(transcript)} characters.", flush=True)
