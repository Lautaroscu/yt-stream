#!/usr/bin/env python3
"""
yt-stream: proxy RTSP (cámara IP) -> YouTube Live (RTMP).

Multiplataforma (Linux / Windows), sin dependencias externas de Python.
Se instala como servicio que arranca con la computadora y se recupera solo
de cortes de luz, de internet o de la cámara.

Uso:
  Linux:   sudo python3 yt_stream.py <comando>
  Windows: (consola como administrador) python yt_stream.py <comando>

Comandos:
  install    instala ffmpeg + servicio que arranca con la PC
  uninstall  saca el servicio (conserva el .conf)
  auth <client_secret.json>   autoriza la cuenta de YouTube (títulos por día)
  live       inicia la transmisión ahora (fuera de horario, hasta 'end' o fin del horario)
  pause      pausa: al aire queda una placa "en pausa" (mismo video en YouTube)
  resume     sale de la pausa
  end        finaliza la transmisión (vuelve sola en el próximo horario)
  status     estado actual + últimas líneas de log
  logs       logs en vivo
  start | stop | restart   control del servicio
  run        corre en primer plano (para probar)
"""

import base64
import ctypes
import ipaddress
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import platform
import re
import secrets
import shutil
import socket
import signal
import subprocess
import threading
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

APP = "yt-stream"
IS_WIN = platform.system() == "Windows"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

if IS_WIN:
    BASE = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / APP
    CONF = BASE / "yt-stream.conf"
    LOG = BASE / "stream.log"
    FFMPEG_DIR = BASE / "ffmpeg"
else:
    BASE = Path("/opt") / APP
    CONF = Path("/etc/yt-stream.conf")
    LOG = BASE / "stream.log"
    FFMPEG_DIR = None
    UNIT = Path("/etc/systemd/system/yt-stream.service")

INSTALLED_SCRIPT = BASE / "yt_stream.py"
CONTROL_FILE = BASE / "control.json"      # iniciar / pausar / finalizar manual
TOKEN_FILE = BASE / "youtube-token.json"   # credenciales OAuth (yt_stream.py auth)
STATE_FILE = BASE / "youtube-state.json"   # id del stream/vivo actual
FFMPEG_WIN_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
YT_RTMP = "rtmp://a.rtmp.youtube.com/live2/"

CONF_TEMPLATE = """\
# Configuración de yt-stream (clave=valor)
# Después de editar: reiniciar el servicio (yt_stream.py restart)

# URL RTSP de la cámara. EZVIZ/Hikvision: admin + código de verificación (6 letras)
CAM_URL="rtsp://admin:CODIGO@192.168.1.X:554/Streaming/Channels/101"

# Opcional: MAC de la cámara. Si la IP cambia, la busca en la red y corrige CAM_URL sola.
CAM_MAC=""

# Clave de transmisión de YouTube Studio (solo la clave, sin la URL).
# Con la cuenta autorizada (yt_stream.py auth) no hace falta: se usa como respaldo.
YT_KEY="xxxx-xxxx-xxxx-xxxx-xxxx"

# Título de cada vivo. {fecha} = día en que arranca (dd/mm/aaaa).
# Requiere autorizar la cuenta una vez: yt_stream.py auth client_secret.json
# Vacío = no usa la API (título por defecto de YouTube Studio).
TITULO="Torneo Nacional de Bowling - {fecha}"

# Privacidad de cada vivo: public | unlisted | private
PRIVACIDAD="public"

# Horario de transmisión. Vacío = siempre (24 h). "manual" = solo cuando lo inicien.
# Se aplica sin reiniciar (se relee cada pocos segundos).
# Ejemplos:
#   HORARIO="18:00-02:00"                            todos los días (cruza medianoche)
#   HORARIO="lun-vie 18:00-02:00, sab-dom 14:00-03:00"
#   HORARIO="vie 20:00-23:30, sab 20:00-23:30"
# Días: lun mar mie jue vie sab dom
HORARIO=""

# Opcional: bitrate de video cuando hay que recodificar con la CPU (H.265 -> H.264).
# Con GPU Intel (VAAPI) se usa calidad constante (qp 19, resolución original) y esto no se aplica.
VIDEO_BITRATE="4000k"

# Panel web en la red local: http://IP-DE-ESTA-PC:8080
# PANEL_PIN vacío = sin clave (cualquiera en la red puede manejarlo). PANEL_PORT vacío = 8080
PANEL_PORT="8080"
PANEL_PIN=""
"""

# mensajes normales al engancharse a mitad de un GOP: no ensucian el log
NOISE = ("Could not find ref", "reference picture missing", "Missing reference picture",
         "co located POCs unavailable", "mmco: unref short failure")

log = logging.getLogger(APP)
_child = None
_stopping = False
_wake = threading.Event()          # despierta los sleeps cuando el panel cambia algo
_restart_req = threading.Event()   # reinicio de ffmpeg pedido desde el panel
STATE = {"state": "starting", "detail": "", "codec": None, "since": time.time()}
_CRED_RE = re.compile(r"(\w+://)[^/@\s]+@")


def mask(text):
    """Oculta usuario:contraseña de las URLs (rtsp://admin:XXX@ -> rtsp://***@)."""
    text = _CRED_RE.sub(r"\1***@", text)
    return re.sub(r"(/live2/)[\w-]+", r"\1***", text)  # clave de YouTube


STATE_TXT = {"live": "En vivo", "paused": "En pausa", "waiting": "En espera", "finished": "Finalizada",
             "connecting": "Conectando", "error": "Problema", "starting": "Iniciando"}


def set_state(state, detail="", codec=None):
    if STATE["state"] != state or STATE["detail"] != detail:
        if STATE["state"] != state:
            STATE["since"] = time.time()
        STATE.update(state=state, detail=detail, codec=codec)
        (log.warning if state == "error" else log.info)(
            "%s%s", STATE_TXT.get(state, state), f": {detail}" if detail else "")


# ---------------------------------------------------------------- utilidades

def setup_logging(to_file=True):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stdout:  # con pythonw.exe (Windows) no hay consola
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    if to_file:
        try:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(LOG, maxBytes=2_000_000, backupCount=3,
                                                      encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError:
            pass


def is_admin():
    if IS_WIN:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def require_admin():
    if not is_admin():
        print("Necesita permisos de administrador:")
        print("  Linux:   sudo python3 yt_stream.py <comando>")
        print("  Windows: abrí la consola 'Como administrador'")
        sys.exit(1)


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, check=check, **kw)


def read_conf():
    conf = {}
    if not CONF.exists():
        return conf
    for line in CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        conf[k.strip()] = v.strip().strip('"').strip("'")
    return conf


def bin_path(name):
    if IS_WIN:
        p = FFMPEG_DIR / f"{name}.exe"
        if p.exists():
            return str(p)
    return shutil.which(name) or name


def prevent_sleep():
    """Windows: evita la suspensión mientras corre (en Linux lo hace systemd-inhibit)."""
    if IS_WIN:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass


# ------------------------------------------------------------------ horario

DAYS = {"lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}


def _hm(txt):
    h, m = txt.strip().split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(txt)
    return h * 60 + m


def _days(txt):
    txt = txt.strip().lower()
    if "-" in txt:
        a, b = (DAYS[x.strip()] for x in txt.split("-"))
        return {(a + i) % 7 for i in range((b - a) % 7 + 1)}
    return {DAYS[txt]}


def parse_schedule(spec):
    """'lun-vie 18:00-02:00, sab 14:00-03:00' -> [(días, inicio_min, fin_min), ...]"""
    rules = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        bits = part.split()
        days = _days(bits[0]) if len(bits) == 2 else set(range(7))
        start, end = bits[-1].split("-")
        rules.append((days, _hm(start), _hm(end)))
    return rules


def in_schedule(rules, now=None):
    if not rules:
        return True
    now = now or datetime.now()
    t, today, yday = now.hour * 60 + now.minute, now.weekday(), (now.weekday() - 1) % 7
    for days, start, end in rules:
        if start == end:  # día completo
            if today in days:
                return True
        elif start < end:
            if today in days and start <= t < end:
                return True
        else:  # cruza la medianoche: pertenece al día en que empieza
            if (today in days and t >= start) or (yday in days and t < end):
                return True
    return False


def schedule_kind(conf):
    """('always'|'manual'|'schedule', reglas). HORARIO vacío = siempre, 'manual' = solo a mano."""
    spec = conf.get("HORARIO", "").strip()
    if not spec:
        return "always", []
    if spec.lower() == "manual":
        return "manual", []
    try:
        return "schedule", parse_schedule(spec)
    except (ValueError, KeyError, IndexError):
        log.warning("HORARIO inválido ('%s'), se ignora (solo manual)", spec)
        return "manual", []


def auto_active(conf, now=None):
    kind, rules = schedule_kind(conf)
    if kind == "always":
        return True
    if kind == "manual":
        return False
    return in_schedule(rules, now)


def _scan(conf, now, want, minutes):
    """Primer instante (en pasos de 1 min) desde now en que auto_active == want."""
    t = now.replace(second=0, microsecond=0)
    for i in range(minutes + 1):
        cand = t + timedelta(minutes=i)
        if auto_active(conf, cand) == want:
            return cand if i else now
    return None


def next_start(conf, now=None):
    now = now or datetime.now()
    kind, _ = schedule_kind(conf)
    if kind != "schedule":
        return None
    return _scan(conf, now, True, 8 * 24 * 60)


def window_end(conf, now=None):
    """Fin de la ventana de horario actual, o de la próxima si empieza en menos de 12 h."""
    now = now or datetime.now()
    kind, _ = schedule_kind(conf)
    if kind != "schedule":
        return None
    start = now if auto_active(conf, now) else _scan(conf, now, True, 12 * 60)
    return _scan(conf, start, False, 24 * 60) if start else None


DIAS_TXT = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fmt_when(dt):
    today = datetime.now().date()
    if dt.date() == today:
        day = "hoy"
    elif (dt.date() - today).days == 1:
        day = "mañana"
    else:
        day = f"{DIAS_TXT[dt.weekday()]} {dt:%d/%m}"
    return f"{day} {dt:%H:%M}"


# ---- control manual (control.json): modo auto / on (forzado) / off (finalizado) + pausa

def get_control():
    c = {"mode": "auto", "until": None, "paused": False}
    c.update(_read_json(CONTROL_FILE))
    return c


def save_control(c):
    _write_json(CONTROL_FILE, c)
    _wake.set()


def decide(conf, now=None):
    """Qué hacer ahora: ('camera'|'slate'|None, estado, detalle)."""
    now = now or datetime.now()
    c, changed = get_control(), False
    if c["mode"] in ("on", "off") and c["until"] and now.timestamp() >= c["until"]:
        c.update(mode="auto", until=None)
        changed = True

    if c["mode"] == "off":
        nxt = next_start(conf, datetime.fromtimestamp(c["until"])) if c["until"] else None
        detail = f"Finalizada. Próximo inicio: {fmt_when(nxt)}" if nxt else "Finalizada. Tocá Iniciar para volver a transmitir"
        result = (None, "finished", detail)
    elif c["mode"] == "on" or auto_active(conf, now):
        result = ("slate", "paused", "Al aire se ve la placa de pausa") if c["paused"] \
            else ("camera", "live", "")
    else:
        kind, _ = schedule_kind(conf)
        nxt = next_start(conf, now)
        detail = (f"Próximo inicio: {fmt_when(nxt)}" if nxt else
                  "Sin horario programado. Tocá Iniciar para transmitir" if kind != "always" else "")
        result = (None, "waiting", detail)

    if result[0] is None and c["paused"]:  # la pausa no pasa al día siguiente
        c["paused"], changed = False, True
    if changed:
        save_control(c)
    return result


def action_start(conf):
    """Iniciar ahora. Si hay horario, dura hasta el fin de la ventana de hoy; si no, hasta Finalizar."""
    if auto_active(conf):
        save_control({"mode": "auto", "until": None, "paused": False})
    else:
        end = window_end(conf)
        save_control({"mode": "on", "until": end.timestamp() if end else None, "paused": False})


def action_end(conf):
    """Finalizar. Si estaba dentro del horario, no vuelve hasta la próxima ventana."""
    if auto_active(conf):
        end = window_end(conf)
        save_control({"mode": "off", "until": end.timestamp() if end else None, "paused": False})
    else:
        save_control({"mode": "auto", "until": None, "paused": False})


def action_pause(paused):
    c = get_control()
    c["paused"] = paused
    save_control(c)


# -------------------------------------------------------------- YouTube API
# Con la cuenta autorizada, cada sesión de transmisión (por horario o resume)
# crea su propio vivo con título, así cada día queda como un video separado.

OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube"
API = "https://www.googleapis.com/youtube/v3/"
_token_cache = {"value": None, "exp": 0}


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    if not IS_WIN:
        os.chmod(path, 0o600)


def _post_form(url, data):
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def api_enabled(conf):
    return bool(conf.get("TITULO", "").strip()) and TOKEN_FILE.exists()


def _access_token():
    if _token_cache["value"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["value"]
    t = _read_json(TOKEN_FILE)
    r = _post_form(t["token_uri"], {
        "client_id": t["client_id"], "client_secret": t["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
    _token_cache.update(value=r["access_token"], exp=time.time() + r.get("expires_in", 3600))
    return _token_cache["value"]


def _api(method, path, params, body=None):
    url = API + path + "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {_access_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"YouTube API {e.code}: {detail}") from None


def ensure_stream():
    """Stream de ingesta propio y reutilizable (no toca la clave por defecto de Studio)."""
    st = _read_json(STATE_FILE)
    if st.get("stream_id"):
        items = _api("GET", "liveStreams", {"part": "id,cdn", "id": st["stream_id"]}).get("items")
        if items:
            ing = items[0]["cdn"]["ingestionInfo"]
            return st["stream_id"], ing["ingestionAddress"].rstrip("/") + "/" + ing["streamName"]
    s = _api("POST", "liveStreams", {"part": "snippet,cdn,contentDetails"}, {
        "snippet": {"title": "yt-stream"},
        "cdn": {"ingestionType": "rtmp", "resolution": "variable", "frameRate": "variable"},
        "contentDetails": {"isReusable": True}})
    st["stream_id"] = s["id"]
    _write_json(STATE_FILE, st)
    ing = s["cdn"]["ingestionInfo"]
    log.info("Stream de ingesta creado en YouTube (%s)", s["id"])
    return s["id"], ing["ingestionAddress"].rstrip("/") + "/" + ing["streamName"]


def broadcast_status(bid):
    items = _api("GET", "liveBroadcasts", {"part": "status", "id": bid}).get("items")
    return items[0]["status"]["lifeCycleStatus"] if items else None


def start_broadcast(conf):
    """Devuelve la URL RTMP a la que empujar. Reusa el vivo en curso si sigue abierto
    (cortes cortos), si no crea uno nuevo con el título del día."""
    stream_id, rtmp_url = ensure_stream()
    st = _read_json(STATE_FILE)
    bid = st.get("broadcast_id")
    if bid and broadcast_status(bid) in ("created", "ready", "testStarting", "testing",
                                         "liveStarting", "live"):
        return rtmp_url
    title = render_title(conf["TITULO"])
    b = _api("POST", "liveBroadcasts", {"part": "snippet,status,contentDetails"}, {
        "snippet": {"title": title,
                    "scheduledStartTime": datetime.now(timezone.utc).isoformat()},
        "status": {"privacyStatus": conf.get("PRIVACIDAD", "public") or "public",
                   "selfDeclaredMadeForKids": False},
        "contentDetails": {"enableAutoStart": True, "enableAutoStop": True,
                           "enableDvr": True, "latencyPreference": "normal"}})
    _api("POST", "liveBroadcasts/bind", {"part": "id", "id": b["id"], "streamId": stream_id})
    st["broadcast_id"] = b["id"]
    _write_json(STATE_FILE, st)
    log.info("Vivo creado: \"%s\" https://youtu.be/%s", title, b["id"])
    return rtmp_url


def render_title(template):
    return template.replace("{fecha}", datetime.now().strftime("%d/%m/%Y")).strip()[:100]


def rename_broadcast(title):
    """Cambia el título del vivo en curso. Devuelve False si no hay vivo abierto."""
    bid = _read_json(STATE_FILE).get("broadcast_id")
    items = _api("GET", "liveBroadcasts", {"part": "snippet", "id": bid}).get("items") if bid else None
    if not items:
        return False
    sn = items[0]["snippet"]
    # update reemplaza el snippet entero: hay que reenviar los campos obligatorios
    _api("PUT", "liveBroadcasts", {"part": "snippet"}, {"id": bid, "snippet": {
        "title": title, "description": sn.get("description", ""),
        "scheduledStartTime": sn["scheduledStartTime"]}})
    log.info("Título del vivo cambiado a \"%s\" (https://youtu.be/%s)", title, bid)
    return True


def end_broadcast():
    st = _read_json(STATE_FILE)
    bid = st.pop("broadcast_id", None)
    _write_json(STATE_FILE, st)
    if not bid:
        return
    try:
        if broadcast_status(bid) == "live":
            _api("POST", "liveBroadcasts/transition",
                 {"part": "status", "id": bid, "broadcastStatus": "complete"})
        log.info("Vivo finalizado (https://youtu.be/%s)", bid)
    except Exception as e:  # enableAutoStop lo cierra igual
        log.warning("No se pudo cerrar el vivo (%s); YouTube lo cierra solo", e)


def auth(client_secret_path=None):
    """Autoriza la cuenta de YouTube con OAuth (una sola vez)."""
    require_admin()
    if not client_secret_path or not Path(client_secret_path).exists():
        print("Uso: yt_stream.py auth client_secret.json")
        print("(OAuth client tipo 'App de escritorio' de Google Cloud, ver README)")
        sys.exit(1)
    cs = json.loads(Path(client_secret_path).read_text(encoding="utf-8"))
    cs = cs.get("installed") or cs.get("web") or cs
    result = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (q.get("code") or [None])[0]
            result["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Listo, ya podés cerrar esta pestaña." if result["code"] else "Autorización cancelada."
            self.wfile.write(f"<h2>{msg}</h2>".encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}"
    url = cs.get("auth_uri", "https://accounts.google.com/o/oauth2/auth") + "?" + urllib.parse.urlencode({
        "client_id": cs["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": OAUTH_SCOPE, "access_type": "offline", "prompt": "consent"})
    print("Abrí este link en el navegador de ESTA computadora e iniciá sesión con la cuenta del canal:\n")
    print(url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    while "code" not in result:
        srv.handle_request()
    if not result.get("code"):
        print(f"No se autorizó: {result.get('error')}")
        sys.exit(1)
    token_uri = cs.get("token_uri", "https://oauth2.googleapis.com/token")
    tok = _post_form(token_uri, {
        "code": result["code"], "client_id": cs["client_id"], "client_secret": cs["client_secret"],
        "redirect_uri": redirect, "grant_type": "authorization_code"})
    if "refresh_token" not in tok:
        print("Google no devolvió refresh_token. Quitá el acceso de la app en tu cuenta y reintentá.")
        sys.exit(1)
    _write_json(TOKEN_FILE, {"client_id": cs["client_id"], "client_secret": cs["client_secret"],
                             "refresh_token": tok["refresh_token"], "token_uri": token_uri})
    ch = _api("GET", "channels", {"part": "snippet", "mine": "true"}).get("items", [])
    print(f"Autorizado: {ch[0]['snippet']['title'] if ch else 'cuenta sin canal'}")
    print("Reiniciá el servicio para que lo tome: yt_stream.py restart")


# ------------------------------------------------------------------- stream

def _norm_mac(mac):
    m = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    return m if len(m) == 12 else None


def arp_table():
    """{mac_normalizada: ip} de la tabla ARP del sistema."""
    table = {}
    try:
        cmd = ["arp", "-a"] if IS_WIN else ["ip", "neigh"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    if not out and not IS_WIN and Path("/proc/net/arp").exists():
        out = Path("/proc/net/arp").read_text()
    for line in out.splitlines():
        ip = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        mac = re.search(r"([0-9a-fA-F]{2}(?:[:-][0-9a-fA-F]{2}){5})", line)
        if ip and mac and _norm_mac(mac.group(1)) not in (None, "000000000000", "ffffffffffff"):
            table[_norm_mac(mac.group(1))] = ip.group(1)
    return table


def _touch(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=0.5):
            pass
    except OSError:
        pass  # aunque rechace, el host ya quedó en la tabla ARP


def find_ip_by_mac(mac, near_ip):
    """Barre la /24 de near_ip (puerto RTSP) para poblar ARP y busca la MAC."""
    want = _norm_mac(mac)
    if not want:
        return None
    try:
        net = ipaddress.ip_network(f"{near_ip}/24", strict=False)
    except ValueError:
        net = ipaddress.ip_network(f"{lan_ip()}/24", strict=False)
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(lambda h: _touch(str(h), 554), net.hosts()))
    return arp_table().get(want)


def cam_host(cam_url):
    m = re.match(r"^\w+://(?:[^@/]*@)?([^:/]+)", cam_url)
    return m.group(1) if m else None


def replace_cam_host(cam_url, new_ip):
    return re.sub(r"^(\w+://(?:[^@/]*@)?)[^:/]+", lambda m: m.group(1) + new_ip, cam_url, count=1)


def probe_codec(cam_url):
    try:
        r = subprocess.run(
            [bin_path("ffprobe"), "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000",
             "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "csv=p=0", cam_url],
            capture_output=True, text=True, timeout=20,
            creationflags=NO_WINDOW)
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else None
    except (subprocess.TimeoutExpired, OSError, IndexError):
        return None


VAAPI_DEV = "/dev/dri/renderD128"


def has_vaapi():
    return not IS_WIN and os.path.exists(VAAPI_DEV)


def build_ffmpeg_cmd(cam_url, yt_url, codec, bitrate="4000k", hw=True):
    """hw=True: si hay que recodificar y hay GPU Intel (VAAPI), la usa en vez de la CPU."""
    pre = []
    if codec == "h264":
        video = ["-c:v", "copy"]
    elif hw and has_vaapi():
        # decodifica y codifica en la GPU. El driver Intel libre solo hace CQP (sin -b:v)
        pre = ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEV,
               "-hwaccel_output_format", "vaapi"]
        # resolución original de la cámara; qp 19 ~ 10 Mbps en 1440p (rango recomendado por YouTube)
        video = ["-vf", "scale_vaapi=format=nv12",
                 "-c:v", "h264_vaapi", "-low_power", "1", "-qp", "19", "-g", "50"]
    else:
        n = int("".join(c for c in bitrate if c.isdigit()) or 4000)
        video = ["-vf", "scale=-2:1080", "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{n}k",
                 "-maxrate", f"{int(n * 1.125)}k", "-bufsize", f"{n * 2}k",
                 "-pix_fmt", "yuv420p", "-g", "50", "-r", "25"]
    return [bin_path("ffmpeg"), "-hide_banner", "-loglevel", "error", *pre,
            "-rtsp_transport", "tcp", "-timeout", "10000000", "-thread_queue_size", "512",
            "-i", cam_url,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-map", "0:v:0", "-map", "1:a:0",
            *video, "-c:a", "aac", "-b:a", "128k",
            "-f", "flv", yt_url]


def _font():
    for f in ([r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"] if IS_WIN else
              ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]):
        if Path(f).exists():
            return f.replace("\\", "/").replace(":", "\\:")
    return None


def build_slate_cmd(yt_url, text):
    """Placa 'en pausa': mantiene el vivo abierto en YouTube (mismo video)."""
    vf = "format=yuv420p"
    font = _font()
    if font:
        safe = text.replace("\\", "").replace("'", "").replace(":", "\\:").replace("%", "")
        vf = (f"drawtext=fontfile='{font}':text='{safe}':fontcolor=white:fontsize=60:"
              f"x=(w-tw)/2:y=(h-th)/2-30,"
              f"drawtext=fontfile='{font}':text='Volvemos en breve':fontcolor=0xBBBBBB:fontsize=34:"
              f"x=(w-tw)/2:y=(h/2)+50,format=yuv420p")
    return [bin_path("ffmpeg"), "-hide_banner", "-loglevel", "error", "-re",
            "-f", "lavfi", "-i", "color=c=0x141414:s=1280x720:r=25",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
            "-b:v", "1500k", "-maxrate", "1500k", "-bufsize", "3000k", "-g", "50",
            "-c:a", "aac", "-b:a", "128k", "-f", "flv", yt_url]


def _on_signal(signum, frame):
    global _stopping
    _stopping = True
    _wake.set()
    if _child and _child.poll() is None:
        _child.terminate()


def _pump_stderr(proc):
    for line in proc.stderr:
        line = line.strip()
        # ruido normal: decoder entrando a mitad de GOP / corte pedido por nosotros
        if not line or _stopping or any(n in line for n in NOISE):
            continue
        log.warning("ffmpeg: %s", mask(line))


def _terminate(proc):
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run():
    global _child
    setup_logging()
    prevent_sleep()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    log.info("yt-stream iniciado (config: %s)", CONF)
    start_panel(read_conf())
    on_air = False  # hay un vivo abierto (para cerrarlo vía API al terminar)
    cam_fails, last_mac_scan = 0, 0.0
    use_gpu, gpu_fails = True, 0  # tras 3 caídas seguidas (<30 s) con VAAPI, pasa a libx264

    while not _stopping:
        conf = read_conf()
        action, state, detail = decide(conf)
        if action is None:
            if on_air and api_enabled(conf):
                end_broadcast()  # fin de jornada / finalizado: el próximo arranque es otro video
            on_air = False
            set_state(state, detail)
            _sleep(10)
            continue

        cam, key = conf.get("CAM_URL", ""), conf.get("YT_KEY", "")
        key_ok = bool(key) and not key.startswith("xxxx")
        use_api = api_enabled(conf)
        cam_ok = cam and "CODIGO" not in cam and ".X:" not in cam
        if not (key_ok or use_api) or (action == "camera" and not cam_ok):
            set_state("error", "Falta configurar la cámara o la clave de YouTube")
            _sleep(30)
            continue

        codec = None
        if action == "camera":
            set_state("connecting", "Esperando señal de la cámara")
            codec = probe_codec(cam)
            if not codec:
                cam_fails += 1
                mac = conf.get("CAM_MAC", "")
                # tras 3 fallos seguidos, buscar la cámara por MAC (como mucho cada 5 min)
                if cam_fails >= 3 and _norm_mac(mac) and time.time() - last_mac_scan > 300:
                    last_mac_scan = time.time()
                    set_state("connecting", "Buscando la cámara en la red por su MAC")
                    old_ip = cam_host(cam)
                    new_ip = find_ip_by_mac(mac, old_ip)
                    if new_ip and new_ip != old_ip:
                        set_conf_value("CAM_URL", replace_cam_host(cam, new_ip))
                        log.info("La cámara cambió de IP: %s -> %s (CAM_URL actualizada)", old_ip, new_ip)
                        cam_fails = 0
                        continue
                    log.warning("No se encontró la MAC %s en la red", mac)
                set_state("error", "La cámara no responde (¿apagada o sin Wi-Fi?)")
                _sleep(10)
                continue
            cam_fails = 0

        target = YT_RTMP + key
        if use_api:
            try:
                target = start_broadcast(conf)
            except Exception as e:
                if not key_ok:
                    set_state("error", f"YouTube API: {e}")
                    _sleep(60)
                    continue
                log.error("YouTube API: %s. Uso YT_KEY (título por defecto)", e)

        if action == "camera":
            cmd = build_ffmpeg_cmd(cam, target, codec, conf.get("VIDEO_BITRATE", "4000k"), use_gpu)
        else:
            cmd = build_slate_cmd(target, conf.get("PAUSA_TEXTO") or "Transmisión en pausa")
        try:
            _child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE, text=True, errors="replace",
                                      bufsize=1, creationflags=NO_WINDOW)
        except OSError as e:
            set_state("error", f"No se pudo ejecutar ffmpeg: {e}")
            _sleep(30)
            continue

        threading.Thread(target=_pump_stderr, args=(_child,), daemon=True).start()
        on_air = True
        gpu_run = action == "camera" and codec != "h264" and use_gpu and has_vaapi()
        started = time.time()
        set_state(state, detail if action == "slate" else
                  ("" if codec == "h264" else
                   f"Recodificando H.265 → H.264 ({'GPU' if gpu_run else 'CPU'})"), codec)
        _restart_req.clear()

        # mientras transmite: reacciona a pausa / finalizar / horario / reinicio
        planned = False
        while _child.poll() is None and not _stopping:
            _wake.wait(3)
            _wake.clear()
            if _restart_req.is_set():
                _restart_req.clear()
                log.info("Reinicio de la transmisión pedido desde el panel")
                _terminate(_child)
                planned = True
                break
            if decide(read_conf())[0] != action:
                _terminate(_child)
                planned = True
        _child.wait()

        if gpu_run and not planned and not _stopping:
            gpu_fails = gpu_fails + 1 if time.time() - started < 30 else 0
            if gpu_fails >= 3:
                use_gpu = False
                log.warning("La GPU (VAAPI) falló 3 veces seguidas: sigo recodificando con la CPU (libx264)")
        elif gpu_run:
            gpu_fails = 0

        if not _stopping and not planned:
            set_state("connecting", "Se cortó la transmisión, reconectando")
            _sleep(5)

    log.info("yt-stream detenido")


def _sleep(seconds):
    end = time.time() + seconds
    while not _stopping and time.time() < end:
        if _wake.wait(0.5):  # el panel cambió algo: reevaluar ya
            _wake.clear()
            return


# -------------------------------------------------------------------- panel
# Panel web en la red local para manejar el stream: estado, vista de cámara,
# pausar/reanudar, reiniciar y horario. Protegido con PIN (PANEL_PIN en el .conf).

SECRET_FILE = BASE / "panel-secret"
_fails = {}               # ip -> (intentos, bloqueado_hasta)
_snap = {"t": 0, "jpg": None}
_snap_lock = threading.Lock()


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "IP-DE-ESTA-PC"


def set_conf_value(key, value):
    """Reemplaza (o agrega) KEY="value" en el .conf conservando el resto."""
    value = value.replace('"', "").replace("\n", " ").strip()
    lines = CONF.read_text(encoding="utf-8").splitlines() if CONF.exists() else []
    new, done = f'{key}="{value}"', False
    for i, line in enumerate(lines):
        if re.match(rf"^\s*{key}\s*=", line):
            lines[i], done = new, True
    if not done:
        lines.append(new)
    CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def ensure_pin():
    """PIN obligatorio: si no hay, genera uno de 6 dígitos y lo guarda en el .conf."""
    pin = read_conf().get("PANEL_PIN", "").strip()
    if not pin and CONF.exists():
        pin = f"{secrets.randbelow(10**6):06d}"
        set_conf_value("PANEL_PIN", pin)
    return pin


def _secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        if not IS_WIN:
            os.chmod(SECRET_FILE, 0o600)
    return SECRET_FILE.read_bytes()


def _session_token(pin):
    return hmac.new(_secret(), pin.encode(), hashlib.sha256).hexdigest()


def snapshot(cam):
    with _snap_lock:
        if _snap["jpg"] and time.time() - _snap["t"] < 4:
            return _snap["jpg"]
        try:
            r = subprocess.run(
                [bin_path("ffmpeg"), "-hide_banner", "-loglevel", "error",
                 "-rtsp_transport", "tcp", "-timeout", "5000000", "-i", cam,
                 "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "6",
                 "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                capture_output=True, timeout=15, creationflags=NO_WINDOW)
            if r.stdout:
                _snap.update(t=time.time(), jpg=r.stdout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return _snap["jpg"]


def last_logs(n=40):
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            return [mask(l.rstrip()) for l in f.readlines()[-n:]]
    except OSError:
        return []


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "yt-stream"

    def log_message(self, *a):
        pass

    # ---- helpers
    def _send(self, code, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(min(n, 10_000)) or b"{}")
        except ValueError:
            return {}

    def _authed(self):
        pin = read_conf().get("PANEL_PIN", "")
        if not pin:
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "yts" and hmac.compare_digest(v, _session_token(pin)):
                return True
        return False

    # ---- rutas
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._send(200, PANEL_HTML, "text/html")
        if not self._authed():
            return self._send(401, {"error": "auth"})
        if path == "/api/status":
            conf = read_conf()
            return self._send(200, {
                **STATE, "now": time.time(),
                "horario": conf.get("HORARIO", ""),
                "titulo": conf.get("TITULO", ""),
                "api": TOKEN_FILE.exists(),
                "logs": last_logs(),
            })
        if path == "/api/snapshot.jpg":
            jpg = snapshot(read_conf().get("CAM_URL", ""))
            return self._send(200, jpg, "image/jpeg") if jpg else self._send(503, {"error": "sin imagen"})
        self._send(404, {"error": "no existe"})

    def do_POST(self):
        path, ip = self.path.split("?")[0], self.client_address[0]
        if path == "/api/login":
            n, until = _fails.get(ip, (0, 0))
            if time.time() < until:
                return self._send(429, {"error": f"Demasiados intentos. Probá en {int(until - time.time())} s"})
            pin = read_conf().get("PANEL_PIN", "")
            given = str(self._json_body().get("pin", ""))
            if pin and hmac.compare_digest(given, pin):
                _fails.pop(ip, None)
                return self._send(200, {"ok": True}, headers={
                    "Set-Cookie": f"yts={_session_token(pin)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"})
            n += 1
            _fails[ip] = (0, time.time() + 60) if n >= 5 else (n, 0)
            log.warning("Panel: PIN incorrecto desde %s", ip)
            return self._send(403, {"error": "PIN incorrecto"})
        if path == "/api/logout":
            return self._send(200, {"ok": True}, headers={"Set-Cookie": "yts=; Path=/; Max-Age=0"})

        if not self._authed():
            return self._send(401, {"error": "auth"})
        conf = read_conf()
        if path == "/api/start":
            action_start(conf)
            log.info("Panel: iniciar desde %s", ip)
        elif path == "/api/end":
            action_end(conf)
            log.info("Panel: finalizar desde %s", ip)
        elif path == "/api/pause":
            action_pause(True)
            log.info("Panel: pausar desde %s", ip)
        elif path == "/api/resume":
            action_pause(False)
            log.info("Panel: reanudar desde %s", ip)
        elif path == "/api/restart":
            _restart_req.set()
            log.info("Panel: reinicio pedido desde %s", ip)
        elif path == "/api/horario":
            spec = str(self._json_body().get("horario", "")).strip()
            try:
                if spec.lower() != "manual":
                    parse_schedule(spec)
            except (ValueError, KeyError, IndexError):
                return self._send(400, {"error": "Horario inválido"})
            set_conf_value("HORARIO", spec)
            log.info("Panel: horario cambiado a '%s' desde %s", spec or "siempre (24 h)", ip)
        elif path == "/api/titulo":
            body = self._json_body()
            titulo = str(body.get("titulo", "")).replace("\n", " ").strip()
            if not titulo:
                return self._send(400, {"error": "Escribí un título"})
            if "<" in titulo or ">" in titulo:
                return self._send(400, {"error": "El título no puede tener < ni >"})
            if len(render_title(titulo)) < len(titulo.replace("{fecha}", "dd/mm/aaaa")):
                return self._send(400, {"error": "Máximo 100 caracteres"})
            if not TOKEN_FILE.exists():
                return self._send(400, {"error": "Falta autorizar la cuenta de YouTube (yt_stream.py auth)"})
            set_conf_value("TITULO", titulo)
            log.info("Panel: título cambiado a '%s' desde %s", titulo, ip)
            msg = "Título guardado para los próximos vivos"
            if body.get("actual"):
                try:
                    if rename_broadcast(render_title(titulo)):
                        msg = "Título cambiado, también en el vivo actual"
                except Exception as e:
                    log.error("YouTube API: no se pudo renombrar el vivo: %s", e)
                    return self._send(502, {"error": "Se guardó, pero YouTube no dejó cambiar el vivo actual"})
            return self._send(200, {"ok": True, "msg": msg})
        else:
            return self._send(404, {"error": "no existe"})
        _wake.set()
        self._send(200, {"ok": True})


def start_panel(conf):
    try:
        port = int(conf.get("PANEL_PORT") or 8080)
    except ValueError:
        port = 8080
    if not ensure_pin():
        log.warning("Panel desactivado: no hay .conf todavía")
        return
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), PanelHandler)
    except OSError as e:
        log.error("No se pudo abrir el panel en el puerto %s: %s", port, e)
        return
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Panel web: http://%s:%s", lan_ip(), port)


PANEL_HTML = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#141413">
<title>Transmisión</title>
<style>
:root{--bg:#f4f3ef;--card:#fff;--ink:#1c1b19;--mute:#6f6c65;--line:#e4e1d9;--live:#d62d20;--ok:#1f7a4d;--warn:#b26a00;--accent:#1c1b19;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#121211;--card:#1c1c1a;--ink:#f0efea;--mute:#9b988f;--line:#2d2c29;--accent:#f0efea;--on-accent:#121211}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:560px;margin:0 auto;padding:18px 16px 40px}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
h1{font-size:17px;margin:0}h2{font-size:15px;margin:0 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin-bottom:14px}
.status{display:flex;align-items:center;gap:12px}
.dot{width:12px;height:12px;border-radius:50%;background:var(--mute);flex:none}
.live .dot{background:var(--live);animation:p 1.6s infinite}
@keyframes p{0%{box-shadow:0 0 0 0 rgba(214,45,32,.55)}70%{box-shadow:0 0 0 10px rgba(214,45,32,0)}100%{box-shadow:0 0 0 0 rgba(214,45,32,0)}}
.paused .dot,.waiting .dot{background:var(--warn)}.error .dot{background:var(--live)}.connecting .dot{background:var(--ok)}
.st{font-weight:700;font-size:20px;letter-spacing:-.01em}.sub{color:var(--mute);font-size:13.5px}
.cam{position:relative;aspect-ratio:16/9;background:#000;border-radius:12px;overflow:hidden;margin-top:14px}
.cam img{width:100%;height:100%;object-fit:contain;display:block}.cam img:not([src]){display:none}
.cam span{position:absolute;inset:0;display:grid;place-items:center;color:#999;font-size:13px}
.actions{display:grid;gap:10px;margin-top:14px}.actions.two{grid-template-columns:1fr 1fr}
button{font:inherit;font-weight:650;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:14px 16px;cursor:pointer;min-height:50px}
button.primary{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
button.go{background:var(--live);border-color:var(--live);color:#fff;font-size:16px}
button.danger{color:var(--live)}
button:disabled{opacity:.5}
.link{background:none;border:none;color:var(--mute);padding:6px;min-height:0;font-weight:500;text-decoration:underline}
.msg{font-size:13px;margin-top:8px}.msg:empty{display:none}.msg.err{color:var(--live)}.msg.ok{color:var(--ok)}
.seg{display:grid;grid-template-columns:repeat(3,1fr);background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:3px;gap:3px}
.seg button{min-height:40px;padding:8px 4px;border:none;background:none;font-size:13.5px;border-radius:9px}
.seg button.on{background:var(--card);box-shadow:0 1px 2px rgba(0,0,0,.12)}
.help{color:var(--mute);font-size:13px;margin:10px 0 0}
.days{margin-top:12px;display:grid;gap:6px}
.day{display:grid;grid-template-columns:64px minmax(0,1fr) minmax(0,1fr);gap:8px;align-items:center}
.day label{display:flex;align-items:center;gap:8px;font-weight:600}
.day input[type=checkbox]{width:20px;height:20px;accent-color:var(--accent)}
.chk{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:14px}
.chk input{width:20px;height:20px;accent-color:var(--accent)}
input[type=time],input#pin,input#titulo{font:inherit;width:100%;min-width:0;padding:9px 8px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.day.off input[type=time]{opacity:.35}
.copy{margin-top:8px}
details summary{cursor:pointer;font-weight:650}
pre{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word;color:var(--mute);max-height:280px;overflow:auto;margin:10px 0 0}
#login{max-width:320px;margin:14vh auto 0}
#login input{font-size:26px;letter-spacing:.45em;text-align:center;padding:12px}
.hidden{display:none!important}
</style></head><body><main>

<section id="login" class="hidden"><div class="card">
  <h1 style="margin-bottom:14px">Transmisión en vivo</h1>
  <form id="lf"><input id="pin" type="password" inputmode="numeric" autocomplete="current-password" maxlength="12" placeholder="PIN" aria-label="PIN">
  <div class="msg err" id="lmsg"></div>
  <button class="primary" style="width:100%">Entrar</button></form>
</div></section>

<section id="app" class="hidden">
  <header><h1>Transmisión en vivo</h1><button class="link" id="out">Salir</button></header>

  <div class="card" id="stcard">
    <div class="status"><div class="dot"></div>
      <div><div class="st" id="st">…</div><div class="sub" id="sd"></div></div></div>
    <div class="cam"><img id="snap" alt="Vista de la cámara"><span id="nosnap">Cargando cámara…</span></div>
    <div class="actions" id="actions"></div>
    <div class="msg" id="amsg"></div>
  </div>

  <div class="card">
    <h2>Título del vivo</h2>
    <input id="titulo" maxlength="100" aria-label="Título del vivo" placeholder="Palotes Bowling en vivo - {fecha}">
    <p class="help">{fecha} se reemplaza por la fecha del día. Ej.: Torneo de los jueves - {fecha}</p>
    <label class="chk"><input type="checkbox" id="tactual" checked>Cambiar también el vivo que está al aire</label>
    <button class="primary" id="tsave" style="width:100%;margin-top:12px">Guardar título</button>
    <div class="msg" id="tmsg"></div>
  </div>

  <div class="card">
    <h2>Programación</h2>
    <div class="seg" id="seg">
      <button data-k="schedule">Por horario</button><button data-k="always">Siempre</button><button data-k="manual">Solo manual</button>
    </div>
    <div id="sched">
      <div class="days" id="days"></div>
      <button class="link copy" id="copy">Copiar el primer día marcado a todos</button>
    </div>
    <p class="help" id="khelp"></p>
    <button class="primary" id="save" style="width:100%;margin-top:12px">Guardar programación</button>
    <div class="msg" id="hmsg"></div>
  </div>

  <div class="card"><details><summary>Registro</summary><pre id="logs"></pre>
    <button class="link" id="restart" style="margin-top:8px">Reiniciar conexión</button></details></div>
</section>

<script>
const $=id=>document.getElementById(id);
const LABEL={live:"En vivo",paused:"En pausa",waiting:"En espera",finished:"Finalizada",connecting:"Conectando…",error:"Hay un problema",starting:"Iniciando…"};
const DAYS=["lun","mar","mie","jue","vie","sab","dom"],DNAME=["Lun","Mar","Mié","Jue","Vie","Sáb","Dom"];
const KHELP={schedule:"Arranca y termina sola en los horarios marcados. Si el horario termina después de medianoche (ej. 18:00 a 02:00) queda en el mismo día.",
  always:"Transmite las 24 h, todos los días.",manual:"Solo transmite cuando toquen Iniciar."};
let st={},kind="manual",dirty=false,tdirty=false,timer,snapTimer,busy=false;

async function api(path,opt={}){
  const r=await fetch(path,{credentials:"same-origin",headers:{"Content-Type":"application/json"},...opt});
  if(r.status===401){show(false);throw new Error("auth")}
  const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
function show(app){$("login").classList.toggle("hidden",app);$("app").classList.toggle("hidden",!app);
  clearInterval(timer);clearInterval(snapTimer);
  if(app){refresh(true);snap();timer=setInterval(refresh,3000);snapTimer=setInterval(snap,6000)}else{$("pin").focus()}}
function flash(el,t,ok){el.textContent=t;el.className="msg "+(ok?"ok":"err");setTimeout(()=>{if(el.textContent===t)el.textContent=""},4500)}
function dur(s){s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} h ${m} min`:m?`${m} min`:"recién"}

/* ---------- estado y botones ---------- */
const BTN={start:["Iniciar transmisión","go","/api/start"],pause:["Pausar","","/api/pause"],resume:["Reanudar","primary","/api/resume"],
  end:["Finalizar","danger","/api/end","¿Finalizar la transmisión? Se cierra el vivo en YouTube."]};
function buttonsFor(s){
  if(s==="live")return["pause","end"];if(s==="paused")return["resume","end"];
  if(s==="connecting"||s==="error")return["end"];return["start"]}
let lastBtns="";
function renderActions(state){
  const keys=buttonsFor(state),sig=keys.join();if(sig===lastBtns)return;lastBtns=sig;
  const box=$("actions");box.className="actions"+(keys.length===2?" two":"");box.innerHTML="";
  for(const k of keys){const[t,cls,url,ask]=BTN[k];const b=document.createElement("button");b.textContent=t;if(cls)b.className=cls;
    b.onclick=async()=>{if(ask&&!confirm(ask))return;busy=true;box.querySelectorAll("button").forEach(x=>x.disabled=true);
      try{await api(url,{method:"POST"});flash($("amsg"),{start:"Iniciando… tarda unos segundos",pause:"Pausando…",resume:"Volviendo a la cámara…",end:"Finalizando…"}[k],true)}
      catch(e){flash($("amsg"),e.message)}
      setTimeout(()=>{busy=false;lastBtns="";refresh()},1500)};box.appendChild(b)}}
async function refresh(first){try{
  st=await api("/api/status");
  $("stcard").className="card "+st.state;$("st").textContent=LABEL[st.state]||st.state;
  let sub=st.detail||"";if(st.state==="live")sub=`Al aire hace ${dur(st.now-st.since)}`+(sub?" · "+sub:"");
  $("sd").textContent=sub;
  if(!busy)renderActions(st.state);
  if(first||!dirty)loadSched(st.horario);
  if((first||!tdirty)&&document.activeElement!==$("titulo"))$("titulo").value=st.titulo||"";
  $("tsave").disabled=!st.api;if(!st.api)$("tmsg").textContent="Para cambiar el título hay que autorizar la cuenta de YouTube";
  const pre=$("logs"),atEnd=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-5;pre.textContent=st.logs.join("\n");if(atEnd)pre.scrollTop=pre.scrollHeight;
}catch(e){}}
function snap(){const img=new Image();img.onload=()=>{$("snap").src=img.src;$("nosnap").style.display="none"};
  img.onerror=()=>{if(!$("snap").getAttribute("src"))$("nosnap").textContent="Sin imagen de la cámara"};img.src="/api/snapshot.jpg?"+Date.now()}

/* ---------- programación ---------- */
const dayRows=DAYS.map((d,i)=>{const row=document.createElement("div");row.className="day off";
  row.innerHTML=`<label><input type="checkbox">${DNAME[i]}</label><input type="time" value="18:00" aria-label="Inicio ${DNAME[i]}"><input type="time" value="02:00" aria-label="Fin ${DNAME[i]}">`;
  $("days").appendChild(row);const[cb,a,b]=row.querySelectorAll("input");
  cb.onchange=()=>{row.classList.toggle("off",!cb.checked);dirty=true};a.oninput=b.oninput=()=>{dirty=true};return{row,cb,a,b}});
function expandDays(t){if(!t)return[0,1,2,3,4,5,6];t=t.normalize("NFD").replace(/[̀-ͯ]/g,"").toLowerCase();
  if(t.includes("-")){const[x,y]=t.split("-").map(v=>DAYS.indexOf(v.trim()));const out=[];for(let i=0;i<=(y-x+7)%7;i++)out.push((x+i)%7);return out}
  return[DAYS.indexOf(t.trim())]}
function setKind(k){kind=k;document.querySelectorAll("#seg button").forEach(b=>b.classList.toggle("on",b.dataset.k===k));
  $("sched").classList.toggle("hidden",k!=="schedule");$("khelp").textContent=KHELP[k]}
function loadSched(spec){spec=(spec||"").trim();
  if(!spec){setKind("always");return}if(spec.toLowerCase()==="manual"){setKind("manual");return}
  dayRows.forEach(r=>{r.cb.checked=false;r.row.classList.add("off")});
  for(const part of spec.split(",").map(x=>x.trim()).filter(Boolean)){const bits=part.split(/\s+/);const[s,e]=bits[bits.length-1].split("-");
    for(const i of expandDays(bits.length===2?bits[0]:"")){const r=dayRows[i];if(!r||r.cb.checked)continue;r.cb.checked=true;r.row.classList.remove("off");r.a.value=s.padStart(5,"0");r.b.value=e.padStart(5,"0")}}
  setKind("schedule")}
function buildSpec(){if(kind==="always")return"";if(kind==="manual")return"manual";
  const parts=dayRows.map((r,i)=>r.cb.checked?`${DAYS[i]} ${r.a.value}-${r.b.value}`:null).filter(Boolean);
  if(!parts.length)throw new Error("Marcá al menos un día");if(dayRows.some(r=>r.cb.checked&&(!r.a.value||!r.b.value)))throw new Error("Completá los horarios");
  return parts.join(", ")}
document.querySelectorAll("#seg button").forEach(b=>b.onclick=()=>{setKind(b.dataset.k);dirty=true});
$("copy").onclick=()=>{const f=dayRows.find(r=>r.cb.checked);if(!f)return flash($("hmsg"),"Marcá un día primero");
  dayRows.forEach(r=>{r.cb.checked=true;r.row.classList.remove("off");r.a.value=f.a.value;r.b.value=f.b.value});dirty=true};
$("save").onclick=async()=>{try{const h=buildSpec();await api("/api/horario",{method:"POST",body:JSON.stringify({horario:h})});
  dirty=false;flash($("hmsg"),"Programación guardada",true);refresh(true)}catch(e){flash($("hmsg"),e.message)}};

/* ---------- título ---------- */
$("titulo").oninput=()=>{tdirty=true};
$("tsave").onclick=async()=>{const b=$("tsave");b.disabled=true;
  try{const r=await api("/api/titulo",{method:"POST",body:JSON.stringify({titulo:$("titulo").value,actual:$("tactual").checked})});
    tdirty=false;flash($("tmsg"),r.msg,true)}catch(e){flash($("tmsg"),e.message)}b.disabled=false};

/* ---------- sesión ---------- */
$("lf").onsubmit=async e=>{e.preventDefault();try{await api("/api/login",{method:"POST",body:JSON.stringify({pin:$("pin").value})});
  $("pin").value="";$("lmsg").textContent="";show(true)}catch(err){$("lmsg").textContent=err.message;$("pin").select()}};
$("out").onclick=async()=>{await fetch("/api/logout",{method:"POST"});show(false)};
$("restart").onclick=async()=>{if(!confirm("¿Reiniciar la conexión? Se corta unos segundos."))return;
  try{await api("/api/restart",{method:"POST"});flash($("amsg"),"Reiniciando conexión…",true)}catch(e){flash($("amsg"),e.message)}};
fetch("/api/status",{credentials:"same-origin"}).then(r=>show(r.ok)).catch(()=>show(false));
</script></main></body></html>
"""


# -------------------------------------------------------------- instalación

def ensure_ffmpeg():
    if IS_WIN:
        if (FFMPEG_DIR / "ffmpeg.exe").exists():
            return
        print("Descargando ffmpeg...")
        zip_path = BASE / "ffmpeg.zip"
        urllib.request.urlretrieve(FFMPEG_WIN_URL, zip_path)
        FFMPEG_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():
                if "/bin/" in member and member.endswith(".exe"):
                    (FFMPEG_DIR / Path(member).name).write_bytes(z.read(member))
        zip_path.unlink()
    else:
        if shutil.which("ffmpeg") and shutil.which("ffprobe"):
            return
        if shutil.which("apt-get"):
            sh(["apt-get", "update"])
            sh(["apt-get", "install", "-y", "ffmpeg"])
        elif shutil.which("dnf"):
            sh(["dnf", "install", "-y", "ffmpeg"])
        else:
            print("Instalá ffmpeg con el gestor de paquetes de tu distro y volvé a correr install")
            sys.exit(1)


def install():
    require_admin()
    BASE.mkdir(parents=True, exist_ok=True)
    ensure_ffmpeg()

    shutil.copy2(Path(__file__).resolve(), INSTALLED_SCRIPT)

    if not CONF.exists():
        CONF.write_text(CONF_TEMPLATE, encoding="utf-8")
        if not IS_WIN:
            os.chmod(CONF, 0o600)
        print(f"Config creada: {CONF}")
    else:
        print(f"Config existente conservada: {CONF}")
    pin = ensure_pin()
    port = read_conf().get("PANEL_PORT") or "8080"

    if IS_WIN:
        install_windows_task()
        edit = f"notepad {CONF}"
    else:
        install_systemd()
        edit = f"sudo nano {CONF}"

    print("\nListo. El servicio arranca solo con la computadora.")
    print(f"Panel web (desde el celu en la misma red): http://{lan_ip()}:{port}   PIN: {pin}")
    print("Falta:")
    print(f"  1. {edit}   (URL de la cámara + clave de YouTube)")
    print(f"  2. {'python' if IS_WIN else 'sudo python3'} yt_stream.py restart")


def install_systemd():
    UNIT.write_text(f"""[Unit]
Description=yt-stream: RTSP -> YouTube Live
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/usr/bin/systemd-inhibit --what=sleep:idle:handle-lid-switch --why="Transmitiendo a YouTube" {sys.executable} -u {INSTALLED_SCRIPT} run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
""", encoding="utf-8")
    sh(["systemctl", "daemon-reload"])
    sh(["systemctl", "enable", "--now", APP])


def _python_for_task():
    # pythonw.exe evita la ventana de consola
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return str(w if w.exists() else exe)


def install_windows_task():
    ps = f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName '{APP}' -Confirm:$false -ErrorAction SilentlyContinue
$a = New-ScheduledTaskAction -Execute '{_python_for_task()}' -Argument '"{INSTALLED_SCRIPT}" run' -WorkingDirectory '{BASE}'
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
     -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
     -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{APP}' -Action $a -Trigger $t -Principal $p -Settings $s | Out-Null
Start-ScheduledTask -TaskName '{APP}'
# panel web accesible desde la red local
Remove-NetFirewallRule -DisplayName '{APP} panel' -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName '{APP} panel' -Direction Inbound -Protocol TCP `
  -LocalPort {read_conf().get("PANEL_PORT") or 8080} -Action Allow -Profile Private,Domain | Out-Null
"""
    sh(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps])


def uninstall():
    require_admin()
    stop()
    if IS_WIN:
        sh(["schtasks", "/Delete", "/TN", APP, "/F"], check=False, capture_output=True)
        sh(["powershell", "-NoProfile", "-Command",
            f"Remove-NetFirewallRule -DisplayName '{APP} panel' -ErrorAction SilentlyContinue"],
           check=False, capture_output=True)
        shutil.rmtree(FFMPEG_DIR, ignore_errors=True)
    else:
        sh(["systemctl", "disable", APP], check=False, capture_output=True)
        UNIT.unlink(missing_ok=True)
        sh(["systemctl", "daemon-reload"], check=False)
    INSTALLED_SCRIPT.unlink(missing_ok=True)
    print(f"Servicio eliminado. Config conservada en {CONF}")


def start():
    require_admin()
    if IS_WIN:
        sh(["schtasks", "/Run", "/TN", APP])
    else:
        sh(["systemctl", "start", APP])
    print("Iniciado")


def stop():
    require_admin()
    if IS_WIN:
        sh(["schtasks", "/End", "/TN", APP], check=False, capture_output=True)
        # mata el ffmpeg que haya quedado colgado
        sh(["taskkill", "/F", "/IM", "ffmpeg.exe"], check=False, capture_output=True)
    else:
        sh(["systemctl", "stop", APP], check=False)
    print("Detenido")


def restart():
    stop()
    time.sleep(2)
    start()


def golive():
    require_admin()
    action_start(read_conf())
    print("Transmisión iniciada (arranca en unos segundos).")


def end():
    require_admin()
    action_end(read_conf())
    print("Transmisión finalizada. Vuelve sola en el próximo horario programado.")


def pause():
    require_admin()
    action_pause(True)
    print("En pausa: al aire se ve la placa de pausa. Para volver: yt_stream.py resume")


def resume():
    require_admin()
    action_pause(False)
    print("Reanudado.")


def status():
    action, state, detail = decide(read_conf())
    print(f"Estado: {STATE_TXT.get(state, state)}" + (f" — {detail}" if detail else ""))
    print()
    if IS_WIN:
        sh(["schtasks", "/Query", "/TN", APP, "/V", "/FO", "LIST"], check=False)
    else:
        sh(["systemctl", "status", APP, "--no-pager"], check=False)
    tail(10, follow=False)


def tail(n=30, follow=True):
    if not LOG.exists():
        print(f"Todavía no hay logs en {LOG}")
        return
    with open(LOG, encoding="utf-8", errors="replace") as f:
        for line in f.readlines()[-n:]:
            print(line, end="")
        if not follow:
            return
        try:
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass


COMMANDS = {
    "run": run, "install": install, "uninstall": uninstall,
    "start": start, "stop": stop, "restart": restart,
    "live": golive, "pause": pause, "resume": resume, "end": end, "auth": auth,
    "status": status, "logs": tail,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    try:
        COMMANDS[sys.argv[1]](*sys.argv[2:3])
    except subprocess.CalledProcessError as e:
        print(f"Falló el comando: {' '.join(map(str, e.cmd))}")
        sys.exit(1)
