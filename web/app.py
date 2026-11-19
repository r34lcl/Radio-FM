"""Web mínima para la radio FM: reproductor, cambio de frecuencia y modo estéreo/mono.

No controla Docker: escribe la sintonía deseada en STATE_DIR (freq, stereo, multi y
el plan resultante) y el contenedor `tuner` la detecta y se re-sintoniza. El audio
se sirve en /stream?f=<MHz>, reenviando el mountpoint de Icecast de esa emisora,
para que todo funcione desde un solo origen.

Multi-emisora: además de la emisora principal se pueden transmitir otras ("extras",
elegidas en el admin) siempre que quepan junto a ella en el bloque de espectro que
capta el dongle.

Acceso:
  - Público: / (solo reproductor), /stream y /api/status (solo lectura).
  - Admin: /admin, /api/listeners (registro de oyentes) y POST /api/tune, con usuario/contraseña (cookie de sesión
    firmada con HMAC) y Cloudflare Turnstile en el formulario de login.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get("STATE_DIR", "/state")
ICECAST_URL = os.environ.get("ICECAST_URL", "http://icecast:8000")
DEFAULT_FREQ = os.environ.get("FREQ", "98.1")
DEFAULT_STEREO = os.environ.get("STEREO", "1")
DEFAULT_GAIN = os.environ.get("GAIN", "auto")
# Ganancias del R820T (el escaneo guarda la lista real del dongle en scan_result.json)
R820T_GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4,
               28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6]
FREQ_MIN, FREQ_MAX = 87.5, 108.0

# Multi-emisora: el dongle captura 2,4 MHz; las emisoras deben quedar a ≤ 1 MHz del
# centro (los bordes del bloque se degradan) y lejos de 0 Hz (pico de DC del RTL-SDR).
MULTI_HALF_SPAN = 1.0
MULTI_MAX_STATIONS = 6

WEB_USER = os.environ["WEB_USER"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"].encode()
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() == "true"
# true solo si la web está detrás de un proxy inverso que pone X-Real-IP / X-Forwarded-For
TRUST_PROXY = os.environ.get("TRUST_PROXY", "false").lower() == "true"
TURNSTILE_SITE_KEY = os.environ.get("TURNSTILE_SITE_KEY", "")
TURNSTILE_SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "")
COOKIE = "radio_session"

# Límite de intentos fallidos de login por IP
MAX_FAILS, FAIL_WINDOW = 5, 600
_fails = {}
_fails_lock = threading.Lock()

try:
    with open(os.path.join(APP_DIR, "VERSION"), encoding="utf-8") as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "?"

PUBLIC_PAGES = {
    "/": ("listen.html", "text/html; charset=utf-8"),
    "/embed": ("listen.html", "text/html; charset=utf-8"),   # mismo reproductor, en modo insertado
    "/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),   # traducciones (español / inglés)
}
# Solo /embed se puede mostrar dentro de un <iframe> en otros sitios
EMBEDDABLE = {"/embed"}
ADMIN_PAGES = {
    "/admin": ("index.html", "text/html; charset=utf-8"),
    "/admin/config": ("config.html", "text/html; charset=utf-8"),
}
NAME_MAX = 40
_stations_lock = threading.Lock()


# ---------------------------------------------------------------- sintonía

def read_state(name, default):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return f.read().strip() or default
    except FileNotFoundError:
        return default


def write_state(name, value):
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(value + "\n")
    os.replace(tmp, path)


def mount_for(freq):
    return f"fm{freq.replace('.', '')}.mp3"


def icecast_sources():
    """{mountpoint: datos} de las fuentes conectadas a Icecast."""
    try:
        with urllib.request.urlopen(f"{ICECAST_URL}/status-json.xsl", timeout=3) as r:
            sources = json.load(r)["icestats"].get("source")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return {}
    if isinstance(sources, dict):
        sources = [sources]
    return {s.get("listenurl", "").rsplit("/", 1)[-1]: s for s in sources or []}


def load_stations():
    """Lista de emisoras [{freq, snr, name}] en STATE_DIR (editable desde el admin).
    La primera vez se inicializa con web/stations.json (vacía: se llena con el escaneo de la banda)."""
    path = os.path.join(STATE_DIR, "stations.json")
    if not os.path.exists(path):
        path = os.path.join(APP_DIR, "stations.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_stations(stations):
    stations.sort(key=lambda s: float(s["freq"]))
    write_state("stations.json", json.dumps(stations, ensure_ascii=False, indent=1))


def station_name(freq):
    for s in load_stations():
        if s["freq"] == freq:
            return s.get("name") or None
    return None


def now_playing():
    """{frecuencia: canción} identificadas por el servicio songid."""
    try:
        with open(os.path.join(STATE_DIR, "nowplaying.json"), encoding="utf-8") as f:
            np = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    # heartbeat viejo = songid caído: no mostrar algo que puede estar desactualizado
    if time.time() - np.get("heartbeat", 0) > 300:
        return {}
    return np.get("stations") or {}


def songid_capturing():
    """Mountpoint que songid está grabando (cuenta como oyente en Icecast), o None."""
    path = os.path.join(STATE_DIR, "songid.capturing")
    try:
        if time.time() - os.path.getmtime(path) < 30:
            with open(path) as f:
                return f.read().strip()
    except FileNotFoundError:
        pass
    return None


def read_extras():
    return read_state("extras", "").split()


def fits(freqs):
    fs = [float(f) for f in freqs]
    return max(fs) - min(fs) <= 2 * MULTI_HALF_SPAN + 1e-6


def compute_plan(principal, extras):
    """(centro_MHz o None, [frecuencias]) a transmitir; la principal va primero.

    De los extras se usan los que quepan en el bloque junto con la principal (los más
    cercanos primero), y como centro el punto que deja a todas lo más lejos posible
    de 0 Hz. Sin extras se usa el modo de una emisora (sintonía directa)."""
    p = float(principal)
    group = [principal]
    for f in sorted(set(extras) - {principal}, key=lambda f: abs(float(f) - p)):
        if len(group) < MULTI_MAX_STATIONS and fits(group + [f]):
            group.append(f)
    if len(group) == 1:
        return None, group
    fs = [float(f) for f in group]
    lo, hi = max(fs) - MULTI_HALF_SPAN, min(fs) + MULTI_HALF_SPAN
    candidates = [round(lo + i * 0.05, 2) for i in range(int((hi - lo) / 0.05 + 1e-6) + 1)]
    mid = (min(fs) + max(fs)) / 2
    center = max(candidates, key=lambda c: (min(min(abs(f - c) for f in fs), 0.3), -abs(c - mid)))
    return center, [principal] + sorted(group[1:], key=float)


def update_plan():
    """Recalcula el plan según la configuración y lo escribe si cambió (el tuner lo vigila)."""
    center, freqs = compute_plan(read_state("freq", DEFAULT_FREQ), read_extras())
    plan = f"{center:.2f} {' '.join(freqs)}" if center else f"0 {freqs[0]}"
    if read_state("plan", "") != plan:
        write_state("plan", plan)


def read_plan():
    """(centro_MHz o None, [frecuencias]) que está usando el tuner."""
    parts = read_state("plan", "").split()
    if len(parts) < 2:
        return None, [read_state("freq", DEFAULT_FREQ)]
    return (float(parts[0]) or None), parts[1:]


# ---------------------------------------------------------------- escaneo / ganancia

def read_json(name):
    try:
        with open(os.path.join(STATE_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def scanning():
    """True desde que se pide un escaneo hasta que el tuner termina."""
    if os.path.exists(os.path.join(STATE_DIR, "scan_request")):
        return True
    st = read_json("scan_status.json") or {}
    return st.get("state") == "running" and time.time() - st.get("updated", 0) < 60


def valid_gains():
    result = read_json("scan_result.json") or {}
    return result.get("gains") or R820T_GAINS


def scan_info():
    return {
        "gain": read_state("gain", DEFAULT_GAIN),
        "gains": valid_gains(),
        "scanning": scanning(),
        "status": read_json("scan_status.json"),
        "result": read_json("scan_result.json"),
        "stations": load_stations(),
    }


def apply_scan(use_gain, mode):
    """Aplica el último escaneo. mode: "keep" (misma ubicación: actualiza señal y
    conserva nombres), "clear" (nueva ubicación: borra nombres y emisoras extra)."""
    result = read_json("scan_result.json")
    if not result:
        raise ValueError("No hay un escaneo para aplicar")
    if use_gain:
        write_state("gain", str(result["gain"]))
    if mode in ("keep", "clear"):
        found = {s["freq"]: s["snr"] for s in result["stations"]}
        stations = [{"freq": f, "snr": snr} for f, snr in found.items()]
        if mode == "keep":
            for old in load_stations():
                if not old.get("name"):
                    continue
                entry = next((s for s in stations if s["freq"] == old["freq"]), None)
                if entry is None:  # con nombre pero ya no se recibe: se conserva sin señal
                    entry = {"freq": old["freq"], "snr": None}
                    stations.append(entry)
                entry["name"] = old["name"]
        else:
            write_state("extras", "")
        save_stations(stations)
        update_plan()


def status():
    sources = icecast_sources()
    capturing = songid_capturing()
    songs = now_playing()
    names = {s["freq"]: s.get("name") for s in load_stations()}
    center, freqs = read_plan()
    stations = []
    for freq in freqs:
        mount = mount_for(freq)
        src = sources.get(mount)
        listeners = src.get("listeners", 0) if src else 0
        if capturing == mount:
            listeners = max(0, listeners - 1)
        stations.append({
            "freq": freq,
            "name": names.get(freq) or None,
            "song": songs.get(freq),
            "on_air": src is not None,
            "on_air_name": src.get("server_name") if src else None,
            "stream_start": src.get("stream_start_iso8601") if src else None,
            "listeners": listeners,
        })
    # Campos de primer nivel = emisora principal
    return {
        **stations[0],
        "stereo": read_state("stereo", DEFAULT_STEREO) == "1",
        "scanning": scanning(),
        "center": center,
        "extras": read_extras(),
        "max_stations": MULTI_MAX_STATIONS,
        "half_span": MULTI_HALF_SPAN,
        "total_listeners": sum(s["listeners"] for s in stations),
        "stations": stations,
    }


def parse_freq(value):
    try:
        freq = round(float(value), 1)
    except (ValueError, TypeError):
        raise ValueError("Frecuencia inválida") from None
    if not FREQ_MIN <= freq <= FREQ_MAX:
        raise ValueError(f"La frecuencia debe estar entre {FREQ_MIN} y {FREQ_MAX} MHz")
    return freq


# ---------------------------------------------------------------- registro de oyentes
#
# Cada conexión a /stream queda en STATE_DIR/listeners.db: inicio, última actividad,
# fin (NULL = escuchando ahora), IP, emisora, navegador y bytes enviados. Las
# reconexiones automáticas del reproductor (cambio de sintonía, cortes) se suman a la
# misma fila si llegan dentro de LOG_MERGE segundos.

LOG_KEEP_DAYS = int(os.environ.get("LISTENER_LOG_DAYS", "90"))
LOG_MERGE = 30
LOG_TOUCH = 15     # cada cuántos segundos se actualiza la fila mientras se escucha
_db_lock = threading.Lock()
_db = None


def db():
    global _db
    if _db is None:
        _db = sqlite3.connect(os.path.join(STATE_DIR, "listeners.db"), check_same_thread=False)
        _db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY, freq TEXT, ip TEXT, ua TEXT, admin INTEGER,
            started REAL, last_seen REAL, ended REAL, bytes INTEGER DEFAULT 0)""")
        # Sitio donde está insertado el reproductor (/embed), si se escucha desde otra página
        if "origin" not in [r[1] for r in _db.execute("PRAGMA table_info(sessions)")]:
            _db.execute("ALTER TABLE sessions ADD COLUMN origin TEXT")
        _db.execute("CREATE INDEX IF NOT EXISTS sessions_started ON sessions(started)")
        # Las que quedaron abiertas por un reinicio de la web ya no están escuchando
        _db.execute("UPDATE sessions SET ended = last_seen WHERE ended IS NULL")
        _db.execute("DELETE FROM sessions WHERE started < ?", (time.time() - LOG_KEEP_DAYS * 86400,))
        _db.commit()
    return _db


def log_open(freq, ip, ua, admin, origin=None):
    now = time.time()
    with _db_lock:
        c = db()
        row = c.execute(
            "SELECT id FROM sessions WHERE ip = ? AND ua = ? AND freq = ? AND origin IS ? AND ended > ? "
            "ORDER BY ended DESC LIMIT 1", (ip, ua, freq, origin, now - LOG_MERGE)).fetchone()
        if row:
            c.execute("UPDATE sessions SET ended = NULL, last_seen = ? WHERE id = ?", (now, row[0]))
            sid = row[0]
        else:
            sid = c.execute(
                "INSERT INTO sessions (freq, ip, ua, admin, started, last_seen, origin) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (freq, ip, ua, int(admin), now, now, origin)).lastrowid
        c.commit()
    return sid


def log_update(sid, nbytes, end=False):
    now = time.time()
    with _db_lock:
        c = db()
        c.execute("UPDATE sessions SET last_seen = ?, bytes = bytes + ?, ended = ? WHERE id = ?",
                  (now, nbytes, now if end else None, sid))
        c.commit()


def listener_log(limit=200):
    with _db_lock:
        rows = db().execute(
            "SELECT id, freq, ip, ua, admin, started, last_seen, ended, bytes, origin FROM sessions "
            "ORDER BY ended IS NOT NULL, started DESC LIMIT ?", (limit,)).fetchall()
        day = db().execute(
            "SELECT COUNT(DISTINCT ip) FROM sessions WHERE last_seen > ?", (time.time() - 86400,)).fetchone()[0]
    keys = ("id", "freq", "ip", "ua", "admin", "started", "last_seen", "ended", "bytes", "origin")
    sessions = [dict(zip(keys, r)) for r in rows]
    names = {st["freq"]: st.get("name") for st in load_stations()}
    for x in sessions:
        x["name"] = names.get(x["freq"]) or None
        x["admin"] = bool(x["admin"])
        x["online"] = x["ended"] is None
    return {"now": time.time(), "online": sum(x["online"] for x in sessions),
            "unique_ips_24h": day, "sessions": sessions}


# ---------------------------------------------------------------- sesión

def _sign(payload: bytes) -> str:
    return hmac.new(SESSION_SECRET, payload, hashlib.sha256).hexdigest()


def make_session(user):
    payload = base64.urlsafe_b64encode(
        json.dumps({"u": user, "exp": int(time.time()) + SESSION_DAYS * 86400}).encode()
    )
    return f"{payload.decode()}.{_sign(payload)}"


def check_session(value):
    try:
        payload, sig = value.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode())):
            return False
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("u") == WEB_USER and data.get("exp", 0) > time.time()
    except (ValueError, TypeError):
        return False


def check_credentials(user, password):
    ok_user = hmac.compare_digest(user.encode(), WEB_USER.encode())
    ok_pass = hmac.compare_digest(password.encode(), WEB_PASSWORD.encode())
    return ok_user and ok_pass


def verify_turnstile(token, ip):
    if not TURNSTILE_SECRET_KEY:
        return True  # Turnstile no configurado (uso local)
    if not token:
        return False
    data = urllib.parse.urlencode(
        {"secret": TURNSTILE_SECRET_KEY, "response": token, "remoteip": ip}
    ).encode()
    try:
        with urllib.request.urlopen(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data, timeout=10
        ) as r:
            result = json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    if not result.get("success"):
        print(f"Turnstile rechazado: {result.get('error-codes')}", flush=True)
    return bool(result.get("success"))


def too_many_fails(ip):
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def register_fail(ip):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


LANGUAGES = ("es", "en")


def language():
    """Idioma de la web (lo elige el admin); la página pública y el login también lo usan."""
    lang = read_state("language", "es")
    return lang if lang in LANGUAGES else "es"


def localize(page):
    """El idioma va en <html lang>; i18n.js lo lee de ahí al cargar."""
    return page.replace('<html lang="es">', f'<html lang="{language()}">', 1)


def login_page(error=""):
    with open(os.path.join(APP_DIR, "login.html"), encoding="utf-8") as f:
        page = f.read()
    widget = (
        f'<div class="cf-turnstile" data-sitekey="{html.escape(TURNSTILE_SITE_KEY)}" '
        f'data-theme="dark" data-language="auto"></div>'
        if TURNSTILE_SITE_KEY else ""
    )
    return (
        localize(page).replace("{{TURNSTILE}}", widget)
        .replace("{{ERROR}}", html.escape(error))
        .replace("__VERSION__", html.escape(VERSION))
        .encode()
    )


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "fm-radio-web"

    def log_message(self, fmt, *args):
        if not self.path.startswith("/api/status"):
            print(f"{self.client_ip()} - {fmt % args}", flush=True)

    def client_ip(self):
        # Detrás de un proxy inverso la IP real del cliente viene en sus cabeceras; sin proxy
        # no se les puede creer (cualquiera las puede inventar).
        if TRUST_PROXY:
            ip = self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            if ip:
                return ip
        return self.client_address[0]

    def authed(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and check_session(value):
                return True
        return False

    def send_body(self, code, body, ctype, extra=(), embeddable=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if embeddable:
            self.send_header("Content-Security-Policy", "frame-ancestors *")
        else:
            self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_body(code, json.dumps(obj).encode(), "application/json")

    def redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def cookie_header(self, value, max_age):
        secure = "; Secure" if COOKIE_SECURE else ""
        return f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"

    # ---- GET

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/login":
            if self.authed():
                self.redirect("/admin")
            else:
                self.send_body(200, login_page(), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self.send_body(200, b"ok", "text/plain")
        elif path in PUBLIC_PAGES:
            self.send_file(*PUBLIC_PAGES[path], embeddable=path in EMBEDDABLE)
        elif path == "/api/status":
            self.send_json(200, status())
        elif path == "/stream":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            self.proxy_stream(query.get("f", [""])[0], query.get("src", [""])[0])
        elif path == "/api/scan":
            if self.authed():
                self.send_json(200, scan_info())
            else:
                self.send_json(401, {"error": "No autenticado"})
        elif path == "/api/listeners":
            if self.authed():
                self.send_json(200, listener_log())
            else:
                self.send_json(401, {"error": "No autenticado"})
        elif path == "/api/stations":
            if self.authed():
                self.send_json(200, load_stations())
            else:
                self.send_json(401, {"error": "No autenticado"})
        elif path in ADMIN_PAGES:
            if self.authed():
                self.send_file(*ADMIN_PAGES[path])
            elif path == "/admin":
                self.redirect("/login")
            else:
                self.send_json(401, {"error": "No autenticado"})
        else:
            self.send_error(404)

    def send_file(self, name, ctype, embeddable=False):
        with open(os.path.join(APP_DIR, name), "rb") as f:
            body = f.read()
        if ctype.startswith("text/html"):
            body = localize(body.decode()).replace("__VERSION__", html.escape(VERSION)).encode()
        self.send_body(200, body, ctype, embeddable=embeddable)

    def proxy_stream(self, freq, origin=""):
        _, freqs = read_plan()
        if not freq:
            freq = freqs[0]
        if freq not in freqs:
            self.send_error(404, "Esa emisora no se está transmitiendo")
            return
        try:
            upstream = urllib.request.urlopen(f"{ICECAST_URL}/{mount_for(freq)}", timeout=10)
        except (urllib.error.URLError, OSError):
            self.send_error(503, "Sin señal en este momento")
            return
        with upstream:
            self.send_response(200)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "audio/mpeg"))
            self.send_header("Cache-Control", "no-cache, no-store")
            # Que un proxy nginx no acumule el audio en buffer: el stream es en vivo.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            # src: sitio que tiene insertado el reproductor (lo informa /embed); solo un nombre de host
            origin = re.sub(r"[^a-z0-9.:-]", "", origin.lower())[:100] or None
            sid = log_open(freq, self.client_ip(), self.headers.get("User-Agent", "")[:300], self.authed(), origin)
            sent, last = 0, time.time()
            try:
                while chunk := upstream.read(8192):
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    if time.time() - last > LOG_TOUCH:
                        log_update(sid, sent)
                        sent, last = 0, time.time()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # el oyente cerró, o la fuente se re-sintonizó
            finally:
                log_update(sid, sent, end=True)

    # ---- POST

    def read_body(self, limit=4096):
        length = min(int(self.headers.get("Content-Length", 0) or 0), limit)
        return self.rfile.read(length)

    def do_POST(self):
        if self.path == "/login":
            self.handle_login()
        elif self.path == "/logout":
            self.redirect("/", cookie=self.cookie_header("", 0))
        elif self.path in ("/api/tune", "/api/stations", "/api/scan", "/api/scan/apply", "/api/gain", "/api/language"):
            if not self.authed():
                self.send_json(401, {"error": "No autenticado"})
            elif not self.headers.get("Content-Type", "").startswith("application/json"):
                self.send_json(415, {"error": "Se espera JSON"})
            elif self.path == "/api/tune":
                self.handle_tune()
            elif self.path == "/api/stations":
                self.handle_station_name()
            elif self.path == "/api/language":
                self.handle_language()
            else:
                self.handle_config()
        else:
            self.send_error(404)

    def handle_login(self):
        ip = self.client_ip()
        if too_many_fails(ip):
            self.send_body(429, login_page("Demasiados intentos. Espera unos minutos."),
                           "text/html; charset=utf-8")
            return
        form = urllib.parse.parse_qs(self.read_body().decode(errors="replace"))
        user = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
        token = form.get("cf-turnstile-response", [""])[0]

        if not verify_turnstile(token, ip):
            self.send_body(400, login_page("No se pudo verificar que no eres un robot. Intenta de nuevo."),
                           "text/html; charset=utf-8")
            return
        if not check_credentials(user, password):
            register_fail(ip)
            print(f"Login fallido para '{user}' desde {ip}", flush=True)
            self.send_body(401, login_page("Usuario o contraseña incorrectos."),
                           "text/html; charset=utf-8")
            return
        self.redirect("/admin", cookie=self.cookie_header(make_session(user), SESSION_DAYS * 86400))

    def handle_station_name(self):
        """Pone (o borra, con nombre vacío) el nombre de una frecuencia."""
        try:
            req = json.loads(self.read_body(1024) or b"{}")
            freq = f"{parse_freq(req.get('freq')):.1f}"
            name = " ".join(str(req.get("name") or "").split())[:NAME_MAX]
        except (ValueError, TypeError) as e:
            self.send_json(400, {"error": str(e) or "Petición inválida"})
            return
        with _stations_lock:
            stations = load_stations()
            entry = next((s for s in stations if s["freq"] == freq), None)
            if entry is None and name:
                entry = {"freq": freq, "snr": None}
                stations.append(entry)
            if entry is not None:
                if name:
                    entry["name"] = name
                elif entry.get("snr") is None:
                    stations.remove(entry)  # agregada a mano solo para ponerle nombre
                else:
                    entry.pop("name", None)
            save_stations(stations)
        self.send_json(200, stations)

    def handle_language(self):
        try:
            lang = json.loads(self.read_body(256) or b"{}").get("language")
        except (ValueError, AttributeError):
            lang = None
        if lang not in LANGUAGES:
            self.send_json(400, {"error": "Idioma no válido"})
            return
        write_state("language", lang)
        self.send_json(200, {"language": lang})

    def handle_config(self):
        try:
            req = json.loads(self.read_body(1024) or b"{}")
            if self.path == "/api/scan":
                if not scanning():
                    write_state("scan_request", str(int(time.time())))
                    print(f"Escaneo de la banda solicitado desde {self.client_ip()}", flush=True)
            elif self.path == "/api/gain":
                gain = str(req.get("gain"))
                if gain != "auto":
                    try:
                        ok = float(gain) in valid_gains()
                    except ValueError:
                        ok = False
                    if not ok:
                        raise ValueError("Ganancia no válida para este dongle")
                write_state("gain", gain)
            else:
                if scanning():
                    raise ValueError("Espera a que termine el escaneo")
                mode = req.get("stations")
                if mode not in ("keep", "clear", "none"):
                    raise ValueError("Opción de emisoras no válida")
                with _stations_lock:
                    apply_scan(bool(req.get("gain")), mode)
        except (ValueError, TypeError, KeyError) as e:
            self.send_json(400, {"error": str(e) or "Petición inválida"})
            return
        self.send_json(200, scan_info())

    def handle_tune(self):
        try:
            req = json.loads(self.read_body(1024) or b"{}")
            if "freq" in req:
                write_state("freq", f"{parse_freq(req['freq']):.1f}")
            if "stereo" in req:
                write_state("stereo", "1" if req["stereo"] else "0")
            if "extras" in req:
                extras = sorted({f"{parse_freq(f):.1f}" for f in req["extras"][:20]}, key=float)
                write_state("extras", " ".join(extras))
            update_plan()
        except (ValueError, TypeError) as e:
            self.send_json(400, {"error": str(e) or "Petición inválida"})
            return
        self.send_json(200, status())


if __name__ == "__main__":
    os.makedirs(STATE_DIR, exist_ok=True)
    update_plan()
    db()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
