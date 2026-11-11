"""Opt-in end-to-end check of live source retrieval plus local evidence selection."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import nutrition, store

store.initialize()
result = nutrition.guidance("broccoli raw")
print(json.dumps({"quotes":result["items"],"sources":[s["url"] for s in result["sources"]],"warnings":result["warnings"]},ensure_ascii=True),flush=True)
assert any("broccoli" in i["quote"].lower() and "nhs.uk" in i["url"] for i in result["items"]), "Expected a verified food-specific primary-source quotation"
print("PASS sourced guidance: a food-specific precaution was selected and its quote verified against the live page.",flush=True)
