"""Identifica la canción que suena en la radio con ShazamIO (API no oficial de Shazam).

Cada SONGID_INTERVAL segundos, para cada emisora en el aire que tenga oyentes, graba
~12 s de su stream en Icecast, los consulta a Shazam y escribe el resultado en
STATE_DIR/nowplaying.json, que la web lee para mostrar "Artista – Canción".
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request

from shazamio import Shazam

ICECAST_URL = os.environ.get("ICECAST_URL", "http://icecast:8000")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
DEFAULT_FREQ = os.environ.get("FREQ", "98.1")
INTERVAL = int(os.environ.get("SONGID_INTERVAL", "45"))
CAPTURE_SECONDS = 12
# Sin coincidencias durante este tiempo (locución, publicidad) se deja de mostrar la canción
CLEAR_AFTER = int(os.environ.get("SONGID_CLEAR_AFTER", "240"))

NOWPLAYING = os.path.join(STATE_DIR, "nowplaying.json")
# Mientras existe, la web descuenta esta conexión del número de oyentes
CAPTURING_FLAG = os.path.join(STATE_DIR, "songid.capturing")


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def read_freqs():
    """Emisoras en el aire según el plan del tuner (la principal primero)."""
    try:
        with open(os.path.join(STATE_DIR, "plan")) as f:
            parts = f.read().split()
        if len(parts) >= 2:
            return parts[1:]
    except FileNotFoundError:
        pass
    try:
        with open(os.path.join(STATE_DIR, "freq")) as f:
            return [f.read().strip() or DEFAULT_FREQ]
    except FileNotFoundError:
        return [DEFAULT_FREQ]


def mount_for(freq):
    return f"fm{freq.replace('.', '')}.mp3"


def save(found):
    np = {
        "heartbeat": int(time.time()),
        "stations": {freq: st["song"] for freq, st in found.items() if st["song"]},
    }
    tmp = NOWPLAYING + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(np, f, ensure_ascii=False)
    os.replace(tmp, NOWPLAYING)


def listeners():
    """{mountpoint: oyentes} de las fuentes conectadas a Icecast."""
    try:
        with urllib.request.urlopen(f"{ICECAST_URL}/status-json.xsl", timeout=5) as r:
            sources = json.load(r)["icestats"].get("source")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return {}
    if isinstance(sources, dict):
        sources = [sources]
    return {s.get("listenurl", "").rsplit("/", 1)[-1]: s.get("listeners", 0) for s in sources or []}


def capture(mount):
    with open(CAPTURING_FLAG, "w") as f:
        f.write(mount)
    try:
        with urllib.request.urlopen(f"{ICECAST_URL}/{mount}", timeout=10) as r:
            start, data = time.time(), bytearray()
            while time.time() - start < CAPTURE_SECONDS:
                chunk = r.read(8192)
                if not chunk:
                    break
                data += chunk
            return bytes(data)
    finally:
        try:
            os.remove(CAPTURING_FLAG)
        except FileNotFoundError:
            pass


def parse_track(track):
    return {
        "title": track.get("title", ""),
        "artist": track.get("subtitle", ""),
        "cover": (track.get("images") or {}).get("coverart", ""),
        "url": track.get("url", ""),
    }


async def main():
    shazam = Shazam(language="es-CL", endpoint_country="CL")
    found = {}  # frecuencia -> {"song", "matched_at"}
    save(found)
    log(f"songid iniciado: cada {INTERVAL} s con oyentes, limpia tras {CLEAR_AFTER} s sin coincidencias")

    while True:
        freqs = read_freqs()
        found = {f: st for f, st in found.items() if f in freqs}  # emisoras que salieron del aire
        counts = listeners()
        for freq in freqs:
            mount = mount_for(freq)
            st = found.setdefault(freq, {"song": None, "matched_at": 0})
            if counts.get(mount):  # solo con oyentes (y antes de conectarnos, para no contarnos)
                try:
                    data = await asyncio.to_thread(capture, mount)
                    result = await shazam.recognize(data)
                    track = result.get("track")
                    if freq not in read_freqs():
                        pass  # se re-sintonizó durante la captura: descartar
                    elif track:
                        song = parse_track(track)
                        if song != st["song"]:
                            log(f"{freq} MHz: {song['artist']} – {song['title']}")
                        st["song"], st["matched_at"] = song, int(time.time())
                except Exception as e:  # red, Shazam caído o cambios en su API
                    log(f"{freq} MHz: error al identificar: {e!r}")
            if st["song"] and time.time() - st["matched_at"] > CLEAR_AFTER:
                st["song"] = None
            save(found)
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    asyncio.run(main())
