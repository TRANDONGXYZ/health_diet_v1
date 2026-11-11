"""Opt-in real photo identification check; downloads a Wikimedia banana photo into memory."""
import base64
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import ai, nutrition, store

with tempfile.TemporaryDirectory() as folder, patch.object(store, "DATA", Path(folder)):
    store.initialize()
    request = urllib.request.Request("https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Banana-Single.jpg/330px-Banana-Single.jpg", headers={"User-Agent":"LocalMealNotebookTesting/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        raw = response.read(4_000_000)
    print(f"Downloaded a small photo ({len(raw)} bytes).",flush=True)
    started = time.monotonic()
    result = nutrition.identify(base64.b64encode(raw).decode())
    print(json.dumps({"seconds":round(time.monotonic()-started,1),"result":result},ensure_ascii=True),flush=True)
    assert "banana" in result["name"].lower(), result
    print("PASS local vision: correctly identified a real banana photograph.",flush=True)
