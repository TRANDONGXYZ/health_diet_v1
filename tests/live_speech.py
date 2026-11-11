"""Opt-in real audio download + local ASR smoke check; does not create recipes.

The default public 19-second video keeps CPU testing short. --sample instead uses
whisper.cpp's official speech sample to test conversion/ASR without YouTube access.
"""
import argparse
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mealapp import speech, store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://www.youtube.com/watch?v=jNQXAC9IVRw")
    parser.add_argument("--sample", action="store_true")
    args = parser.parse_args()
    store.initialize()
    started = time.monotonic()
    if args.sample:
        with tempfile.TemporaryDirectory(prefix="meal-speech-test-") as folder:
            audio = Path(folder) / "sample.wav"
            request = urllib.request.Request("https://raw.githubusercontent.com/ggml-org/whisper.cpp/v1.8.3/samples/jfk.wav", headers={"User-Agent":"MealNotebookSpeechTest/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                audio.write_bytes(response.read())
            text = speech.transcribe_file(audio, folder)
        assert "country" in text.lower(), "Expected recognizable speech from the reference sample"
    else:
        # Bypass captions deliberately so this verifies the new fallback, not caption retrieval.
        text = speech.transcribe_video(args.url)
    assert text.strip()
    print(f"PASS real local speech-to-text: {len(text)} characters in {time.monotonic()-started:.1f}s. No recipe was saved.", flush=True)


if __name__ == "__main__":
    main()
