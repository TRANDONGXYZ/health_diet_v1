import hashlib
import io
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app
from mealapp import ai, speech, store, web


URL = "https://www.youtube.com/watch?v=abcdefghijk"
WORDS = "Đậu hũ sốt cà chua. Dùng 300 g đậu hũ, 200 g cà chua. Chiên đậu và nấu sốt."


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(store, "DATA", Path(self.folder.name) / "data")
        self.data_patch.start()
        self.config_patch = patch.object(ai, "ROOT", Path(self.folder.name))
        self.config_patch.start()
        store.initialize()

    def tearDown(self):
        self.config_patch.stop()
        self.data_patch.stop()
        self.folder.cleanup()

    def test_real_captions_do_not_download_audio_or_models(self):
        with patch.object(web, "public_url"), patch.object(web, "youtube_captions", return_value=WORDS), patch.object(web.subprocess, "run") as downloader, patch.object(speech, "transcribe_video") as audio:
            result = web.extract(URL)
        self.assertEqual(result["text"], WORDS)
        self.assertEqual(result["method"], "captions")
        downloader.assert_not_called()
        audio.assert_not_called()

    def test_missing_empty_or_placeholder_captions_fall_back_to_audio(self):
        for captions in ("", " \n ", "Caption is updating ...", "Captions are being updated", "Phụ đề đang cập nhật…"):
            with self.subTest(captions=captions), patch.object(web, "public_url"), patch.object(web, "youtube_captions", return_value=captions), patch.object(web.subprocess, "run"), patch.object(speech, "transcribe_video", return_value=WORDS) as audio:
                result = web.extract(URL)
                self.assertEqual(result["method"], "audio")
                self.assertEqual(result["text"], WORDS)
                audio.assert_called_once_with(URL)

    def test_caption_reader_failure_still_tries_audio(self):
        with patch.object(web, "public_url"), patch.object(web, "youtube_captions", side_effect=ValueError("Unavailable")), patch.object(web.subprocess, "run", side_effect=subprocess.TimeoutExpired("captions", 65)), patch.object(speech, "transcribe_video", return_value=WORDS):
            self.assertEqual(web.video_text(URL), WORDS)

    def test_secondary_reader_uses_valid_english_after_vietnamese_placeholder(self):
        def captions(command, **kwargs):
            folder = Path(command[command.index("--paths") + 1])
            (folder / "captions.vi.vtt").write_text("WEBVTT\nCaption is updating ...", encoding="utf-8")
            (folder / "captions.en.vtt").write_text("WEBVTT\n00:00:00.000 --> 00:00:02.000\nCook the tofu with tomatoes.", encoding="utf-8")
        with patch.object(web, "public_url"), patch.object(web, "youtube_captions", return_value=""), patch.object(web.subprocess, "run", side_effect=captions), patch.object(speech, "transcribe_video") as audio:
            self.assertEqual(web.video_text(URL), "Cook the tofu with tomatoes.")
        audio.assert_not_called()

    def test_audio_text_goes_through_existing_recipe_import(self):
        recipe = {"name":"Đậu hũ sốt cà chua", "servings":2, "vegan":True, "estimated_minutes":20, "groups":[{"title":"Cook", "instruction":"Cook tofu", "ingredients":[{"name":"tofu", "grams":300}]}]}
        with patch.object(web, "video_source", return_value={"text":WORDS, "method":"audio"}), patch.object(ai, "extract_recipe", return_value=recipe) as extractor, patch.object(app.nutrition, "resolve", return_value=[]):
            source = app.post("/api/extract", {"url":URL})
            result = app.post("/api/import", {"url":source["url"], "text":source["text"]})
        extractor.assert_called_once_with(WORDS)
        self.assertEqual(result["recipe"]["source_text"], WORDS)
        self.assertEqual(result["recipe"]["source_url"], URL)
        self.assertEqual(store.all_items("recipes"), [])  # A draft still needs review and Save.

    def test_audio_download_is_cached_and_temporary_files_are_cleaned(self):
        folders = []
        def download(command, timeout, failure):
            folder = Path(command[command.index("--paths") + 1])
            folders.append(folder)
            (folder / "audio.m4a").write_bytes(b"audio")
            self.assertIn("--no-playlist", command)
            self.assertNotIn("--skip-download", command)
            self.assertEqual(command[-2:], ["--", URL])
        with patch.object(web, "public_url"), patch.object(speech, "ensure_assets", return_value=("cli", "model", "ffmpeg")), patch.object(speech, "run", side_effect=download) as downloader, patch.object(speech, "transcribe_file", return_value=WORDS) as transcriber:
            self.assertEqual(speech.transcribe_video(URL), WORDS)
            self.assertEqual(speech.transcribe_video(URL), WORDS)
        self.assertEqual(downloader.call_count, 1)
        self.assertEqual(transcriber.call_count, 1)
        self.assertTrue(all(not folder.exists() for folder in folders))

    def test_failed_audio_transcription_is_not_cached_and_files_are_cleaned(self):
        folders = []
        def download(command, *args):
            folder = Path(command[command.index("--paths") + 1]); folders.append(folder)
            (folder / "audio.webm").write_bytes(b"audio")
        with patch.object(web, "public_url"), patch.object(speech, "ensure_assets", return_value=("cli", "model", "ffmpeg")), patch.object(speech, "run", side_effect=download), patch.object(speech, "transcribe_file", side_effect=ValueError("No speech")):
            with self.assertRaisesRegex(ValueError, "No speech"):
                speech.transcribe_video(URL)
        self.assertTrue(all(not folder.exists() for folder in folders))
        with store.db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cache").fetchone()[0], 0)

    def test_pcm_conversion_and_cpu_multilingual_transcription(self):
        folder = Path(self.folder.name)
        calls = []
        def command(args, *unused):
            calls.append(args)
            if args[0] == "ffmpeg":
                self.assertEqual(args[args.index("-ar") + 1], "16000")
                self.assertEqual(args[args.index("-ac") + 1], "1")
                with wave.open(args[-1], "wb") as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(b"\0" * 32000)
            else:
                output = Path(args[args.index("-of") + 1]).with_suffix(".txt")
                output.write_text(WORDS, encoding="utf-8")
        with patch.object(speech, "run", side_effect=command):
            result = speech.transcribe_file(folder / "audio.webm", folder, ("cli", "model", "ffmpeg"))
        self.assertEqual(result, WORDS)
        self.assertIn("-ng", calls[1])
        self.assertIn("auto", calls[1])
        self.assertNotIn("-tr", calls[1])  # Preserve Vietnamese instead of translating speech.
        self.assertTrue(ai.LOCK.acquire(blocking=False)); ai.LOCK.release()

    def test_download_integrity_failure_never_publishes_partial_asset(self):
        target = Path(self.folder.name) / "model.bin"
        with patch.object(speech.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong download")):
            with self.assertRaisesRegex(ValueError, "integrity"):
                speech.download("https://example.com/model", target, "0" * 64)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_suffix(".bin.part").exists())

    def test_audio_subprocess_timeout_is_actionable(self):
        with patch.object(speech.subprocess, "run", side_effect=subprocess.TimeoutExpired("whisper", 1800)):
            with self.assertRaisesRegex(ValueError, "timed out"):
                speech.run(["whisper"], 1800, "Speech recognition failed.")


if __name__ == "__main__":
    unittest.main()
