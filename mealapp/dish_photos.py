"""Store the browser's centered 500px PNG crops without extra Python dependencies."""
import base64
import hashlib
import re
import struct
import zlib
from . import store

URL = re.compile(r"/media/([a-f0-9]{64}\.png)\Z")


def path_for(url):
    match = URL.fullmatch(str(url))
    return store.DATA / "photos" / match[1] if match else None


def validate_url(url):
    if not url:
        return ""
    from . import hosted
    match=URL.fullmatch(str(url))
    if hosted.enabled() and match:hosted.ensure_photo(match[1])
    with store.LOCK:
        store.check_epoch()
        path = path_for(url)
        if not path or not path.is_file():
            raise ValueError("Upload a valid dish photo first.")
    return url


def save(data):
    if not re.search(r"\.(?:jpe?g|png)\Z", str(data.get("filename", "")), re.I):
        raise ValueError("Dish photos must be .jpg, .jpeg, or .png files.")
    try:
        raw = base64.b64decode(data.get("image", ""), validate=True)
    except (ValueError, TypeError):
        raise ValueError("Invalid dish photo.") from None
    validate_png(raw)
    name = hashlib.sha256(raw).hexdigest() + ".png"
    with store.LOCK:
        store.check_epoch()
        folder = store.DATA / "photos"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        if not path.exists():
            path.write_bytes(raw)
    return {"photo_url":"/media/" + name}


def validate_png(raw):
    if len(raw) > 4_000_000 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Upload the processed 500 by 500 PNG image.")
    offset, header, ended, compressed = 8, None, False, bytearray()
    while offset + 12 <= len(raw):
        size = int.from_bytes(raw[offset:offset+4], "big")
        kind = raw[offset+4:offset+8]
        end = offset + 12 + size
        if end > len(raw):
            break
        payload = raw[offset+8:offset+8+size]
        if zlib.crc32(kind + payload) != int.from_bytes(raw[end-4:end], "big"):
            break
        if header is None:
            if kind != b"IHDR" or size != 13:
                break
            header = struct.unpack(">IIBBBBB", payload)
        if kind == b"IDAT":
            compressed.extend(payload)
        if kind == b"IEND":
            ended = size == 0 and end == len(raw)
            break
        offset = end
    if not header or header[:2] != (500,500) or not ended or header[2] != 8 or header[3] not in (2,6) or header[4:] != (0,0,0):
        raise ValueError("Dish photos must be processed to exactly 500 by 500 pixels.")
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, 1_001_001)
        expected = 500 * (1 + 500 * (4 if header[3] == 6 else 3))
        if len(pixels) != expected or not decoder.eof:
            raise ValueError("Invalid PNG pixels.")
    except zlib.error:
        raise ValueError("Invalid PNG pixels.") from None
