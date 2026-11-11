"""Local CPU speech-to-text fallback, independent of Python ML package versions."""
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import wave
import zipfile
from pathlib import Path

from . import ai, store

VERSION = "1.8.3"  # Stable release with official Windows CPU binaries.
WHISPER_URL = f"https://github.com/ggml-org/whisper.cpp/releases/download/v{VERSION}/whisper-bin-x64.zip"
WHISPER_SHA256 = "d824b1e37599f882b396e73f1ee0bfd5d0529f700314c48311dcbd00b803321d"
MODELS = {"tiny": "bd577a113a864445d4c299885e0cb97d4ba92b5f", "base": "465707469ff3a37a2b9b8d8f89f2f99de7299dac", "small": "55356645c2b361a969dfd0ef2c5a50d530afd8d5"}
INSTALL_LOCK = threading.Lock()
AUDIO_LOCK = threading.Lock()
HIDDEN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def download(url, destination, expected_hash, algorithm="sha256"):
    """Publish only complete, verified assets; interrupted downloads are never reused."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.new(algorithm)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MealNotebook/1.0"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
                digest.update(block)
        if digest.hexdigest() != expected_hash:
            raise ValueError("The speech model or tool download failed its integrity check. Try setup.cmd again.")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_assets():
    settings = ai.config()
    model_name = settings.get("speech_model", "base")
    if model_name not in MODELS:
        raise ValueError("speech_model must be tiny, base, or small (multilingual, without .en).")
    tools = store.DATA / "speech"
    binary_folder = tools / f"whisper-{VERSION}"
    custom_cli = settings.get("whisper_cli_path", "")
    with INSTALL_LOCK:
        cli = Path(custom_cli).expanduser() if custom_cli else next(binary_folder.rglob("whisper-cli.exe"), None) if binary_folder.exists() else None
        if custom_cli and not cli.is_file():
            raise ValueError("whisper_cli_path does not point to an existing whisper-cli executable.")
        if cli is None:
            if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
                external = shutil.which("whisper-cli")
                if not external:
                    raise ValueError("Automatic speech setup supports Windows x64. Install whisper.cpp and set whisper_cli_path in config.local.json on other platforms.")
                cli = Path(external)
            else:
                archive = tools / f"whisper-{VERSION}.zip"
                download(WHISPER_URL, archive, WHISPER_SHA256)
                binary_folder.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive) as bundle:
                    target = binary_folder.resolve()
                    if any(not (target / member.filename).resolve().is_relative_to(target) for member in bundle.infolist()):
                        raise ValueError("Invalid speech tool archive.")
                    bundle.extractall(binary_folder)
                cli = next(binary_folder.rglob("whisper-cli.exe"), None)
                if cli is None:
                    raise ValueError("The speech tool archive has no whisper-cli.exe. Run setup.cmd again.")
        model = tools / "models" / f"ggml-{model_name}.bin"
        if not model.is_file():
            download(f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{model_name}.bin", model, MODELS[model_name], "sha1")
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise ValueError("Audio conversion needs FFmpeg. Run setup.cmd to install the bundled audio tools, then restart the app.") from exc
    return cli.resolve(), model.resolve(), ffmpeg


def run(command, timeout, failure):
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, creationflags=HIDDEN)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(failure + " The operation timed out; try a shorter video.") from exc
    except OSError as exc:
        raise ValueError(failure + " Run setup.cmd and restart the app. Windows may also need the Microsoft Visual C++ x64 runtime.") from exc
    if result.returncode:
        raise ValueError(failure)
    return result


def transcribe_file(audio_path, folder, assets=None):
    """Normalize any downloaded audio to mono 16 kHz PCM, then transcribe locally."""
    cli, model, ffmpeg = assets or ensure_assets()
    settings = ai.config()
    max_seconds = int(settings.get("speech_max_video_minutes", 60)) * 60
    wav = Path(folder) / "speech.wav"
    run([ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-t", str(max_seconds + 1), str(wav)], 180, "Could not convert the video's audio for speech recognition.")
    try:
        with wave.open(str(wav), "rb") as data:
            seconds = data.getnframes() / data.getframerate()
    except (wave.Error, OSError, EOFError) as exc:
        raise ValueError("The downloaded audio could not be read.") from exc
    if seconds > max_seconds:
        raise ValueError(f"The video exceeds the {max_seconds // 60}-minute transcription limit. Use a shorter video or change speech_max_video_minutes in config.local.json.")
    language = str(settings.get("speech_language", "auto"))
    if language != "auto" and (len(language) not in (2, 3) or not language.isalpha()):
        raise ValueError("speech_language must be auto or a language code such as vi or en.")
    output = Path(folder) / "transcript"
    threads = max(1, min(int(settings.get("speech_threads", 4)), os.cpu_count() or 4))
    if not ai.LOCK.acquire(blocking=False):
        raise ValueError("Another AI task is running. Wait for it to finish, then retrieve the source again.")
    try:
        run([str(cli), "-m", str(model), "-f", str(wav), "-l", language, "-t", str(threads), "-bs", "1", "-bo", "1", "-otxt", "-of", str(output), "-nt", "-ng"], int(settings.get("speech_timeout_seconds", 1800)), "Local speech recognition failed. Check the speech tools and try a shorter video.")
    finally:
        ai.LOCK.release()
    transcript = output.with_suffix(".txt")
    text = transcript.read_text(encoding="utf-8-sig").strip() if transcript.is_file() else ""
    if not text:
        raise ValueError("No speech was recognized in the audio. Try another video or paste a transcript.")
    return text


def transcribe_video(url):
    """Download audio only when captions failed; delete audio and WAV on every exit."""
    from .web import public_url, youtube_id, facebook_video
    public_url(url)
    settings = ai.config()
    identifier = youtube_id(url) or url
    cache_key = f"speech:v1:{identifier}:{settings.get('speech_model', 'base')}:{settings.get('speech_language', 'auto')}"
    cached = store.get("cache", cache_key)
    if cached:
        return cached["text"]
    if not AUDIO_LOCK.acquire(blocking=False):
        raise ValueError("Another audio transcription is running. Wait for it to finish, then try again.")
    try:
        assets = ensure_assets()
        max_seconds = int(settings.get("speech_max_video_minutes", 60)) * 60
        with tempfile.TemporaryDirectory(prefix="meal-audio-") as folder:
            command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress", "--socket-timeout", "15", "--retries", "1", "--fragment-retries", "1", "--max-filesize", "200M", "--match-filters", f"!is_live & duration <= {max_seconds}", "-f", "bestaudio[ext=m4a]/bestaudio/best", "--paths", folder, "-o", "audio.%(ext)s", "--", url]
            platform_name = "Facebook" if facebook_video(url) else "YouTube" if youtube_id(url) else "the video site"
            run(command, int(settings.get("video_download_timeout_seconds", 300)), f"Could not download the video's audio from {platform_name}. The video may be private, require sign-in, be unavailable, live, or above the duration/size limit. Use a publicly accessible video or paste its transcript. No browser cookies or sign-in credentials are read.")
            files = [p for p in Path(folder).glob("audio.*") if p.is_file() and p.suffix not in {".part", ".ytdl"}]
            if len(files) != 1:
                raise ValueError("No complete audio file was downloaded. The video may be live, unavailable, or above the 200 MB/configured duration limit.")
            if files[0].stat().st_size > 200 * 1024 * 1024:
                raise ValueError("The downloaded audio exceeds the 200 MB limit. Try a shorter video.")
            text = transcribe_file(files[0], folder, assets)
        store.put("cache", cache_key, {"text": text})
        return text
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError("Speech tools could not be downloaded. Check your internet connection and run setup.cmd, then retrieve the source again.") from exc
    finally:
        AUDIO_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description="Prepare local multilingual speech-to-text tools")
    parser.add_argument("--setup", action="store_true")
    parser.parse_args()
    store.initialize()
    print("Preparing Whisper CPU tools, a multilingual speech model, and FFmpeg. First setup downloads about 150 MB.", flush=True)
    try:
        cli, model, ffmpeg = ensure_assets()
        print(f"Speech-to-text ready.\nTool: {cli}\nModel: {model}\nFFmpeg: {ffmpeg}", flush=True)
    except Exception as exc:
        print(f"Speech setup failed: {exc}", flush=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
