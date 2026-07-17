#!/usr/bin/env python3
"""
Bose SoundTouch preset-to-radio bridge.

Manages one or more SoundTouch speakers on the LAN. For each speaker:
- listens to the local WebSocket; when a preset button is pressed, pushes the
  configured stream URL via UPnP SetAVTransportURI + Play with DIDL-Lite
  metadata so the station name and logo show up on the speaker
- looks up station name + favicon from radio-browser.info (cached for the
  session)
- publishes Home Assistant MQTT-discovery configs so each preset appears as a
  `button.bose_<id>_preset_N` entity. Pressing the entity plays the same URL.

A single config can manage multiple speakers (each with its own preset map),
matched to discovered devices either by IP (`host`) or by the friendly name
the user set on the speaker (`name`, e.g. "Wohnzimmer"). One thread per
speaker handles the WebSocket loop; one shared MQTT client dispatches HA
commands to the right speaker by `device_id`.
"""

import base64
import html
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import paho.mqtt.client as mqtt
import upnpclient
import websocket

__version__ = "1.8.10"

USER_AGENT = f"homeassistant-bose-soundtouch-bridge/{__version__}"

# SSL context that skips certificate verification — the proxy is
# just forwarding streams, we never store sensitive data.
_PROXY_SSL_CTX = ssl.create_default_context()
_PROXY_SSL_CTX.check_hostname = False
_PROXY_SSL_CTX.verify_mode = ssl.CERT_NONE

OPTIONS_PATH = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
SUPERVISOR_URL = "http://supervisor"
RADIO_BROWSER_STATIC_BASES = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://fr1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "http://de1.api.radio-browser.info",
    "http://nl1.api.radio-browser.info",
    "http://at1.api.radio-browser.info",
]
RADIO_BROWSER_BASES = RADIO_BROWSER_STATIC_BASES
_RADIO_BROWSER_BASE_CACHE = {"expires": 0.0, "bases": []}
PRESET_RE = re.compile(r'<nowSelectionUpdated>\s*<preset id="(\d+)"')
MQTT_TOPIC_RE = re.compile(r"^bose_bridge/([^/]+)/preset/(\d+)/command$")
SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"
HTTPS_PROXY_PATH = "/bose-proxy"


def get_local_ip() -> str:
    """Return the machine's primary LAN IP (non-loopback)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def rewrite_url_for_proxy(url: str, proxy_port: int | None) -> str:
    """Rewrite an HTTPS stream URL so the speaker can play it.

    The SoundTouch firmware's UPnP stack only speaks plain HTTP on port 80.
    When ``proxy_port`` is set we encode the real ``https://`` URL and
    replace it with ``http://<ha_lan_ip>:<port>/bose-proxy/<base64>`` so the
    speaker connects to HA's LAN address.

    Returns the original URL unchanged when ``proxy_port`` is ``None`` or the
    URL is already plain HTTP.
    """
    if proxy_port is None or not url.startswith("https://"):
        return url
    # Detect HA's LAN IP so the speaker can reach the proxy
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ha_ip = s.getsockname()[0]
        s.close()
    except Exception:
        ha_ip = "127.0.0.1"
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"http://{ha_ip}:{proxy_port}{HTTPS_PROXY_PATH}/{encoded}"


# ---------- HTTPS proxy ----------------------------------------------------

class _HttpsProxyHandler(urllib.request.BaseHandler):
    """Mixin: serve a proxied HTTPS URL for the speaker."""

    def http_do_proxy(self, handler, proxy_port: int):
        """Serve a proxied HTTPS URL for the speaker.

        The speaker sends a plain HTTP GET to
        http://localhost:<port>/bose-proxy/<base64_https_url>.
        We fetch the real HTTPS URL upstream and stream the audio back.
        """
        path = handler.path
        if not path.startswith(HTTPS_PROXY_PATH + "/"):
            print(f"[proxy] 404 bad path: {path}")
            handler.send_response(404)
            handler.end_headers()
            return

        b64 = path[len(HTTPS_PROXY_PATH) + 1:]
        # Pad to valid base64 length (4n + 0/2/3 — never 4n+1).
        pad = (4 - len(b64) % 4) % 4
        try:
            target_url = base64.urlsafe_b64decode(b64 + "=" * pad).decode()
        except Exception:
            print(f"[proxy] 400 bad base64: {b64[:80]}")
            handler.send_response(404)
            handler.end_headers()
            return

        print(f"[proxy] proxying {target_url}")

        # Fetch upstream — use browser-like User-Agent to avoid geo/UA blocking
        req = urllib.request.Request(target_url)
        req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/120.0.0.0 Safari/537.36")
        try:
            upstream = urllib.request.urlopen(req, timeout=10, context=_PROXY_SSL_CTX)
        except urllib.error.HTTPError as e:
            print(f"[proxy] upstream HTTP error {e.code}: {e.reason}")
            handler.send_response(e.code)
            handler.send_header("Content-Type", "text/plain")
            handler.end_headers()
            handler.wfile.write(f"upstream error {e.code}: {e.reason}".encode())
            return
        except Exception as e:
            print(f"[proxy] upstream connection error: {e}")
            handler.send_response(502)
            handler.send_header("Content-Type", "text/plain")
            handler.end_headers()
            handler.wfile.write(f"upstream error: {e}".encode())
            return

        content_type = "audio/mpeg"
        for hdr in ("Content-Type", "Content-Length"):
            val = upstream.headers.get(hdr)
            if val:
                content_type = hdr == "Content-Type" and val or content_type
        print(f"[proxy] upstream OK — Content-Type={content_type}")

        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        cl = upstream.headers.get("Content-Length")
        if cl:
            handler.send_header("Content-Length", cl)
        handler.send_header("Connection", "close")
        handler.end_headers()

        # Stream audio back with progress logging
        total_bytes = 0
        while True:
            chunk = upstream.read(65536)
            if not chunk:
                print(f"[proxy] done — {total_bytes} bytes streamed")
                break
            try:
                handler.wfile.write(chunk)
                total_bytes += len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                print(f"[proxy] client disconnected — {total_bytes} bytes streamed so far")
                break
        upstream.close()


class _ProxyHandler(BaseHTTPRequestHandler, _HttpsProxyHandler):
    """Small threading HTTP server that proxies HTTPS URLs for the speaker."""

    # Log requests via print() so they appear in HA logs
    def log_message(self, format, *args):
        print(f"[proxy] {format % args}")

    def do_GET(self):
        proxy_port = getattr(self.server, "proxy_port", 9000)
        self.http_do_proxy(self, proxy_port)


def start_https_proxy(port: int = 9000) -> ThreadingHTTPServer:
    """Start a lightweight HTTP proxy that rewrites speaker requests to HTTPS.

    Returns the server object (caller should call server.server_close()
    on shutdown).
    """
    server = ThreadingHTTPServer(("0.0.0.0", port), _ProxyHandler)
    server.proxy_port = port  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True, name="https-proxy")
    t.start()
    return server


# ---------- radio search server --------------------------------------------

RADIO_SEARCH_PATH = "/radio-search"
RADIO_SEARCH_PORT = 9002

# HTML frontend — embedded so zero extra files needed
_RADIO_SEARCH_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SoundTouch Radio Search</title>
<style>
:root{--bg:#141414;--card:#1e1e1e;--border:#333;--accent:#4fc3f7;
--text:#e0e0e0;--muted:#999;--success:#66bb6a}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);padding:16px;max-width:640px;margin:0 auto}
h1{font-size:1.3rem;margin-bottom:12px;display:flex;align-items:center;gap:8px}
h1 .icon{font-size:1.5rem}
.search-box{display:flex;gap:8px;margin-bottom:16px}
.search-box input{flex:1;padding:10px 14px;border:1px solid var(--border);
border-radius:8px;background:var(--card);color:var(--text);font-size:.95rem;
outline:none}
.search-box input:focus{border-color:var(--accent)}
.search-box button{padding:10px 20px;border:none;border-radius:8px;
background:var(--accent);color:#000;font-weight:700;cursor:pointer;
font-size:.95rem;white-space:nowrap}
.search-box button:hover{opacity:.9}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.filters select{padding:6px 10px;border:1px solid var(--border);border-radius:6px;
background:var(--card);color:var(--text);font-size:.85rem}
.filters select:focus{border-color:var(--accent)}
#results{display:flex;flex-direction:column;gap:8px}
.station-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
padding:12px;display:flex;gap:12px;align-items:center;transition:border-color .2s}
.station-card:hover{border-color:var(--accent)}
.station-card .favicon{width:48px;height:48px;border-radius:6px;
object-fit:cover;flex-shrink:0;background:#222}
.station-card .info{flex:1;min-width:0}
.station-card .name{font-weight:600;font-size:.95rem;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.station-card .meta{font-size:.8rem;color:var(--muted);margin-top:2px;
display:flex;gap:8px;flex-wrap:wrap}
.station-card .meta span{display:flex;align-items:center;gap:3px}
.station-card .actions{display:flex;gap:6px;flex-shrink:0}
.station-card .actions button{padding:6px 12px;border:1px solid var(--border);
border-radius:6px;background:transparent;color:var(--text);cursor:pointer;
font-size:.8rem;white-space:nowrap;transition:all .2s}
.station-card .actions button:hover{background:var(--accent);color:#000;
border-color:var(--accent)}
.station-card .actions button.copied{background:var(--success);color:#000;
border-color:var(--success)}
.loading{text-align:center;padding:24px;color:var(--muted)}
.error{text-align:center;padding:16px;color:#ef5350}
.empty{text-align:center;padding:32px;color:var(--muted)}
.ha-info{background:#1a237e;border-radius:8px;padding:10px 14px;margin-bottom:16px;
font-size:.85rem;color:#bbdefb;display:flex;align-items:center;gap:8px}
</style>
</head>
<body>
<h1><span class="icon">📻</span> SoundTouch Radio Search</h1>
<div class="ha-info" id="haInfo" style="display:none">
  ℹ️ Du befindest dich in der Home Assistant App. Die kopierte URL kannst du direkt als Preset eintragen.
</div>
<div class="search-box">
  <input type="text" id="query" placeholder="Station, Genre oder Schlagwort..."
         autofocus autocomplete="off">
  <button onclick="doSearch()">Suchen</button>
</div>
<div class="filters">
  <select id="country">
    <option value="">Alle Länder</option>
    <option value="DE" selected>🇩🇪 Deutschland</option>
    <option value="AT">🇦🇹 Österreich</option>
    <option value="CH">🇨🇭 Schweiz</option>
    <option value="GB">🇬🇧 Großbritannien</option>
    <option value="FR">🇫🇷 Frankreich</option>
    <option value="IT">🇮🇹 Italien</option>
  </select>
  <select id="sort">
    <option value="votes">Meist geklickt</option>
    <option value="name">Name A-Z</option>
  </select>
</div>
<div id="results">
  <div class="empty">Gib einen Suchbegriff ein und drücke Suchen.</div>
</div>

<script>
// Check if embedded in HA Ingress
if(window.top!==window.self){document.getElementById('haInfo').style.display='flex'}

const queryInput=document.getElementById('query');
const resultsDiv=document.getElementById('results');

queryInput.addEventListener('keydown',e=>{if(e.key==='Enter')doSearch()});

async function doSearch(){
  const q=queryInput.value.trim();
  if(!q){queryInput.focus();return}
  const country=document.getElementById('country').value;
  const sort=document.getElementById('sort').value;
  resultsDiv.innerHTML='<div class="loading">🔍 Suche läuft...</div>';
  const params=new URLSearchParams({q,country,sort});
  try{
    const r=await fetch(searchApiUrl(params));
    if(!r.ok){
      let msg='API error';
      try{const err=await r.json();msg=err.error||msg}catch(_e){}
      throw new Error(msg);
    }
    const data=await r.json();
    if(data.error)throw new Error(data.error);
    renderResults(data);
  }catch(e){
    resultsDiv.innerHTML='<div class="error">❌ Fehler: '+e.message+'</div>';
  }
}

function searchApiUrl(params){
  const path=window.location.pathname;
  let base=path;
  if(base.endsWith('/radio-search/')){
    base=base.slice(0,-'radio-search/'.length);
  }else if(base.endsWith('/radio-search')){
    base=base.slice(0,-'radio-search'.length);
  }else if(!base.endsWith('/')){
    base+='/';
  }
  return base+'api/search?'+params;
}

function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}

function renderResults(stations){
  if(!stations.length){resultsDiv.innerHTML='<div class="empty">Keine Ergebnisse gefunden.</div>';return}
  const html=stations.map(s=>{
    const n=esc(s.name||'Unbekannt');
    const u=esc(s.url_resolved||s.url||'');
    const f=esc(s.favicon||'');
    const c=esc(s.country||'');
    const b=s.bitrate?(s.bitrate+' kbps'):'';
    const t=(s.tags||'').split(',').filter(Boolean).slice(0,3).map(esc).join(', ');
    const cc=s.click_count||0;
    const img=f?'<img class="favicon" src="'+f+'" alt="">':'<div class="favicon" style="background:#222;display:flex;align-items:center;justify-content:center;font-size:24px;">🎵</div>';
    return `<div class="station-card">
      ${img}
      <div class="info">
        <div class="name">${n}</div>
        <div class="meta">
          ${c?'<span>🌍 '+c+'</span>':''}
          ${b?'<span>📡 '+b+'</span>':''}
          ${cc?'<span>👆 '+cc+'</span>':''}
          ${t?'<span>🏷️ '+t+'</span>':''}
        </div>
      </div>
      <div class="actions">
        <button class="copy-btn" data-url="${u}">URL kopieren</button>
      </div>
    </div>`;
  }).join('');
  resultsDiv.innerHTML=html;
}

// Event delegation — no inline onclick, no escaping hell
resultsDiv.addEventListener('click',function(e){
  const btn=e.target.closest('.copy-btn');
  if(!btn)return;
  const url=btn.getAttribute('data-url');
  btn.textContent='✓ Kopiert!';btn.classList.add('copied');
  setTimeout(()=>{btn.textContent='URL kopieren';btn.classList.remove('copied')},2000);
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(url).catch(()=>{fallbackCopy(url)});
  }else{fallbackCopy(url)}
});

function fallbackCopy(url){
  const ta=document.createElement('textarea');
  ta.value=url;ta.style.position='fixed';ta.style.left='-9999px';
  document.body.appendChild(ta);ta.select();document.execCommand('copy');
  document.body.removeChild(ta);
}
</script>
</body>
</html>
""";


class RadioSearchError(Exception):
    pass


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _radio_browser_bases() -> list[str]:
    """Return a shuffled, cached list of radio-browser API bases.

    radio-browser.info explicitly recommends discovering mirrors via
    all.api.radio-browser.info instead of pinning one hardcoded server.
    """
    now = time.monotonic()
    cached = _RADIO_BROWSER_BASE_CACHE
    if cached["bases"] and now < cached["expires"]:
        return list(cached["bases"])

    discovered: list[str] = []
    try:
        infos = socket.getaddrinfo("all.api.radio-browser.info", 443, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos})
        for ip in ips:
            try:
                host = socket.gethostbyaddr(ip)[0].rstrip(".")
            except Exception:
                continue
            if host.endswith(".api.radio-browser.info"):
                discovered.append(f"https://{host}")
                discovered.append(f"http://{host}")
    except Exception as e:
        print(f"[search] radio-browser server discovery failed: {e}")

    random.shuffle(discovered)
    bases = _dedupe(discovered + RADIO_BROWSER_STATIC_BASES)
    cached["bases"] = bases
    cached["expires"] = now + 3600
    return bases


def _fetch_radio_search(base: str, search_params: dict[str, str]) -> list[dict]:
    params = urllib.parse.urlencode(search_params)
    url = f"{base}/json/stations/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=12, context=_PROXY_SSL_CTX if base.startswith("https://") else None) as r:
        return json.load(r)


def _station_result(s: dict) -> dict:
    return {
        "name": s.get("name", ""),
        "url": s.get("url", ""),
        "url_resolved": s.get("url_resolved", ""),
        "favicon": s.get("favicon", ""),
        "tags": s.get("tags", ""),
        "country": s.get("country", ""),
        "bitrate": s.get("bitrate", 0),
        "click_count": s.get("click_count", 0),
        "codec": s.get("codec", ""),
        "stationuuid": s.get("stationuuid", ""),
    }


def _search_stations(query: str, country: str = "", sort: str = "votes") -> list[dict]:
    """Search radio stations via radio-browser.info API."""
    reverse = "false" if sort == "name" else "true"
    base_params = {"order": sort, "reverse": reverse, "hidebroken": "true", "limit": "50"}
    if country:
        base_params["countrycode"] = country.upper()

    errors: list[str] = []
    for base in _radio_browser_bases()[:8]:
        try:
            stations = _fetch_radio_search(base, {**base_params, "name": query})
            if not stations:
                stations = _fetch_radio_search(base, {**base_params, "tag": query})
            results: list[dict] = []
            seen: set[str] = set()
            for s in stations:
                url = s.get("url_resolved") or s.get("url") or ""
                if not url.startswith("http"):
                    continue
                key = s.get("stationuuid") or url
                if key in seen:
                    continue
                seen.add(key)
                results.append(_station_result(s))
            print(f"[search] {base} returned {len(results)} result(s) for {query!r}")
            return results[:50]
        except Exception as e:
            msg = f"{base}: {e}"
            errors.append(msg)
            print(f"[search] {msg}")
            continue
    raise RadioSearchError("radio-browser currently unreachable: " + " | ".join(errors[-3:]))


class _RadioSearchHandler(BaseHTTPRequestHandler):
    """HTTP handler for the radio search web UI."""

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/" or path == RADIO_SEARCH_PATH or path == RADIO_SEARCH_PATH + "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_RADIO_SEARCH_HTML.encode("utf-8"))
        elif path == "/api/search" or path.startswith(RADIO_SEARCH_PATH + "/api/search"):
            qs = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(qs)
            query = params.get("q", [""])[0]
            country = params.get("country", [""])[0]
            sort = params.get("sort", ["votes"])[0]
            if not query:
                self._json_response(400, {"error": "missing q parameter"})
                return
            try:
                stations = _search_stations(query, country, sort)
            except RadioSearchError as e:
                self._json_response(502, {"error": str(e)})
                return
            self._json_response(200, stations)
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Silences request logs to keep HA log clean
        pass


def start_radio_search_server(port: int = RADIO_SEARCH_PORT) -> ThreadingHTTPServer | None:
    """Start the radio search web UI server.

    Returns the server object or None on failure.
    """
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _RadioSearchHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="radio-search")
        t.start()
        print(f"[search] radio search listening on 0.0.0.0:{port}")
        return server
    except OSError as e:
        print(f"[search] failed to start on port {port}: {e}")
        return None


# ---------- config ---------------------------------------------------------


def _entry_has_content(e: dict) -> bool:
    return bool(
        (e.get("host") or "").strip()
        or (e.get("name") or "").strip()
        or any((e.get(f"preset_{n}_url") or "").strip() for n in range(1, 7))
    )


def _wildcard_from_toplevel(d: dict) -> dict | None:
    """Build a wildcard speaker entry from top-level `preset_N_url` fields.
    Returns None if no preset URLs are set."""
    presets = {
        f"preset_{n}_url": (d.get(f"preset_{n}_url") or "").strip()
        for n in range(1, 7)
    }
    return presets if any(presets.values()) else None


def _has_wildcard(speakers: list[dict]) -> bool:
    return any(
        not (s.get("host") or "").strip() and not (s.get("name") or "").strip()
        for s in speakers
    )


def load_options() -> dict:
    """Return {'speakers': [...], 'sync_presets_on_startup': bool,
    'https_proxy': bool, 'proxy_port': int | None}.

    Supervisor (HAOS / Supervised): options come as JSON at /data/options.json.
    Standalone Docker: `SPEAKERS_JSON` env var holds the JSON-encoded list.

    Top-level `preset_1_url` .. `preset_6_url` (Supervisor) or `PRESET_N_URL`
    env vars (standalone) are a convenience for the common case "same presets
    on every speaker": if set and no explicit wildcard already exists in
    `speakers:` / `SPEAKERS_JSON`, they are appended as a wildcard entry.

    Each speaker entry in the list has optional `host` / `name` plus
    `preset_1_url` .. `preset_6_url`. An entry with neither `host` nor `name`
    is a wildcard that fans out to every discovered speaker no explicit entry
    claimed.
    """
    if os.path.exists(OPTIONS_PATH):
        with open(OPTIONS_PATH) as f:
            raw = json.load(f)
        speakers = [s for s in (raw.get("speakers") or []) if _entry_has_content(s)]
        flat = _wildcard_from_toplevel(raw)
        if flat and not _has_wildcard(speakers):
            speakers.append(flat)
        return {
            "speakers": speakers,
            "sync_presets_on_startup": raw.get("sync_presets_on_startup", True),
            "https_proxy": raw.get("https_proxy", False),
            "proxy_port": int(raw.get("proxy_port", 9000)),
        }

    print("[cfg] /data/options.json not found — reading config from environment")
    speakers: list[dict] = []
    speakers_json = os.environ.get("SPEAKERS_JSON", "").strip()
    if speakers_json:
        try:
            parsed = json.loads(speakers_json)
            if isinstance(parsed, list):
                speakers = [s for s in parsed if isinstance(s, dict) and _entry_has_content(s)]
            else:
                print("[cfg] SPEAKERS_JSON is not a JSON list — ignoring")
        except json.JSONDecodeError as e:
            print(f"[cfg] SPEAKERS_JSON invalid: {e}")

    flat = _wildcard_from_toplevel({
        f"preset_{n}_url": os.environ.get(f"PRESET_{n}_URL", "")
        for n in range(1, 7)
    })
    if flat and not _has_wildcard(speakers):
        speakers.append(flat)

    sync = os.environ.get("SYNC_PRESETS_ON_STARTUP", "true").lower() in ("1", "true", "yes", "on")
    https_proxy = os.environ.get("HTTPS_PROXY", "false").lower() in ("1", "true", "yes", "on")
    proxy_port = int(os.environ.get("PROXY_PORT", "9000")) if https_proxy else None
    return {
        "speakers": speakers,
        "sync_presets_on_startup": sync,
        "https_proxy": https_proxy,
        "proxy_port": proxy_port,
    }


# ---------- Bose discovery -------------------------------------------------


def discover_soundtouch_all(timeout: float = 3.0) -> dict[str, str]:
    """Return {ip: upnp_description_url} for every SoundTouch / Bose UPnP
    MediaRenderer that answers SSDP within `timeout`.

    The value is the `LOCATION:` header the device itself advertises — the
    authoritative UPnP device-description URL. We do NOT reconstruct it from
    a guessed path/UUID: the description filename differs across SoundTouch
    models/firmware (some serve it under a different name than the
    `/XD/BO5EBO5E-…` path others use, which 404s). Sends one M-SEARCH and
    drains responses until the deadline (speakers all reply to one packet).
    """
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR[0]}:{SSDP_ADDR[1]}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {SSDP_TARGET}\r\n\r\n"
    ).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(msg, SSDP_ADDR)
    found: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            s.settimeout(remaining)
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                break
            if addr[0] in found:
                continue
            text = data.decode(errors="ignore")
            loc = next(
                (line.split(": ", 1)[1].strip()
                 for line in text.split("\r\n")
                 if line.lower().startswith("location:")),
                None,
            )
            if not loc:
                continue
            try:
                desc = urllib.request.urlopen(loc, timeout=3).read().decode()
            except Exception:
                continue
            if "SoundTouch" in desc or "Bose" in desc:
                found[addr[0]] = loc
    finally:
        s.close()
    return found


def fetch_speaker_info(host: str) -> tuple[str, str, str]:
    """Return (device_id, friendly_name, model) by hitting /info on port 8090."""
    with urllib.request.urlopen(f"http://{host}:8090/info", timeout=5) as r:
        info = r.read().decode()
    device_id = re.search(r'deviceID="([0-9A-F]+)"', info).group(1)
    name = re.search(r"<name>([^<]+)</name>", info)
    model = re.search(r"<type>([^<]+)</type>", info)
    return device_id, (name.group(1) if name else "SoundTouch"), (model.group(1) if model else "SoundTouch")


def get_upnp_services(host: str, device_id: str, desc_url: str | None = None):
    """Resolve the AVTransport + RenderingControl services for a speaker.

    Tries, in order: the SSDP-advertised description URL (authoritative, when
    known); a targeted SSDP lookup for host-pinned speakers that skipped
    discovery; and finally the legacy hardcoded `/XD/BO5EBO5E-…` path as a
    last resort. The hardcoded path works on most models but 404s on some
    — SSDP is the robust source.
    """
    candidates: list[str] = []
    if desc_url:
        candidates.append(desc_url)
    else:
        # Host-pinned config that skipped discovery: do a short targeted
        # SSDP sweep and pick this host's advertised description URL.
        loc = discover_soundtouch_all(timeout=2.0).get(host)
        if loc:
            candidates.append(loc)
    candidates.append(
        f"http://{host}:8091/XD/BO5EBO5E-F00D-F00D-FEED-{device_id}.xml"
    )

    last_err: Exception | None = None
    for url in candidates:
        try:
            print(f"[upnp] description: {url}")
            d = upnpclient.Device(url)
            av = next(s for s in d.services if "AVTransport" in s.service_id)
            rc = next(s for s in d.services if "RenderingControl" in s.service_id)
            return av, rc
        except Exception as e:
            last_err = e
            print(f"[upnp] {url} failed: {e}")
    raise last_err if last_err else RuntimeError("no UPnP description URL to try")


# ---------- speaker resolution --------------------------------------------


def _entry_presets(entry: dict) -> dict[int, str]:
    return {n: url for n in range(1, 7)
            if (url := (entry.get(f"preset_{n}_url") or "").strip())}


def resolve_speakers(cfg_speakers: list[dict]) -> list[dict]:
    """Match each config entry to a real speaker on the LAN.

    An entry with `host` is pinned to that IP. An entry with `name` (but no
    host) is matched against /info `<name>` via SSDP discovery
    (case-insensitive). An entry with NEITHER `host` nor `name` is a
    **wildcard / master preset**: its preset map is fanned out to every
    discovered speaker that no explicit entry has claimed. This lets the
    simplest config — one entry with just `preset_*_url` — apply the same
    presets to every SoundTouch on the LAN, while explicit entries can still
    override individual speakers.

    Only one wildcard entry is honoured; additional ones are ignored.

    Returns dicts {host, device_id, friendly, model, desc_url, presets:{n: url}}.
    `desc_url` is the SSDP-advertised UPnP description URL (None for
    host-pinned speakers that skipped discovery — resolved later).
    """
    explicit: list[dict] = []
    wildcards: list[dict] = []
    for entry in cfg_speakers:
        if (entry.get("host") or "").strip() or (entry.get("name") or "").strip():
            explicit.append(entry)
        else:
            wildcards.append(entry)
    if len(wildcards) > 1:
        print(f"[cfg] {len(wildcards)} wildcard speaker entries — only the first is used, the rest are ignored")
    wildcard = wildcards[0] if wildcards else None

    needs_discovery = wildcard is not None or any(
        not (e.get("host") or "").strip() for e in explicit
    )
    # (ip, device_id, friendly, model, desc_url)
    discovered: list[tuple[str, str, str, str, str]] = []
    if needs_discovery:
        print("[cfg] discovering SoundTouch speakers via SSDP...")
        for ip, loc in discover_soundtouch_all().items():
            try:
                device_id, friendly, model = fetch_speaker_info(ip)
                discovered.append((ip, device_id, friendly, model, loc))
                print(f"[cfg] discovered: {friendly!r} ({model}) at {ip} — id {device_id}")
            except Exception as e:
                print(f"[cfg] could not read /info from {ip}: {e}")
        if not discovered:
            print("[cfg] SSDP discovery returned no speakers")
        elif not explicit:
            # No per-speaker overrides configured — print a copy-paste-ready
            # YAML block so users can extend the config without having to
            # type or guess the speaker names. (HA's add-on schema can't be
            # populated dynamically, so this log hint is the closest we can
            # get to "auto-filling the form".)
            print("[cfg] to override presets for a specific speaker, copy one of these names into `speakers:`")
            print("[cfg]")
            for _, _, friendly, _, _ in discovered:
                print(f'[cfg]     - name: "{friendly}"')
                print('[cfg]       preset_1_url: "http://your-stream.example/stream.mp3"')
            print("[cfg]")

    resolved: list[dict] = []
    used_ids: set[str] = set()

    for entry in explicit:
        host = (entry.get("host") or "").strip()
        name = (entry.get("name") or "").strip()
        presets = _entry_presets(entry)
        if host:
            try:
                device_id, friendly, model = fetch_speaker_info(host)
            except Exception as e:
                print(f"[cfg] cannot reach configured host={host}: {e}")
                continue
            # If discovery ran for other entries, reuse this host's
            # advertised description URL; otherwise get_upnp_services does
            # a targeted SSDP lookup later.
            desc_url = next((d[4] for d in discovered if d[0] == host), None)
        else:
            match = next(
                (d for d in discovered if d[2].lower() == name.lower() and d[1] not in used_ids),
                None,
            )
            if not match:
                avail_names = [d[2] for d in discovered if d[1] not in used_ids]
                print(f"[cfg] no discovered speaker matches name={name!r}; unclaimed: {avail_names}")
                continue
            host, device_id, friendly, model, desc_url = match
        used_ids.add(device_id)
        resolved.append({
            "host": host,
            "device_id": device_id,
            "friendly": friendly,
            "model": model,
            "desc_url": desc_url,
            "presets": presets,
        })

    if wildcard is not None:
        presets = _entry_presets(wildcard)
        remaining = [d for d in discovered if d[1] not in used_ids]
        if not presets:
            print("[cfg] wildcard entry has no preset URLs — skipping")
        elif not remaining:
            print("[cfg] wildcard entry has no unclaimed speakers to apply to")
        else:
            print(f"[cfg] applying wildcard preset map to {len(remaining)} speaker(s): {[d[2] for d in remaining]}")
            for host, device_id, friendly, model, desc_url in remaining:
                used_ids.add(device_id)
                resolved.append({
                    "host": host,
                    "device_id": device_id,
                    "friendly": friendly,
                    "model": model,
                    "desc_url": desc_url,
                    "presets": dict(presets),
                })

    return resolved


# ---------- radio-browser.info ---------------------------------------------


def lookup_station(url: str) -> dict:
    """Return {'name': str, 'favicon': str} or empty dict if not found."""
    body = urllib.parse.urlencode({"url": url}).encode()
    for base in RADIO_BROWSER_BASES:
        try:
            req = urllib.request.Request(
                f"{base}/json/stations/byurl",
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=4) as r:
                stations = json.load(r)
            if stations:
                s = stations[0]
                return {"name": s.get("name", ""), "favicon": s.get("favicon", "")}
            return {}
        except Exception as e:
            print(f"[meta] {base} failed: {e}")
            continue
    return {}


def build_didl(url: str, meta: dict) -> str:
    title = html.escape(meta.get("name") or "Internet Radio")
    art = html.escape(meta.get("favicon") or "")
    art_tag = f"<upnp:albumArtURI>{art}</upnp:albumArtURI>" if art else ""
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        f"<dc:title>{title}</dc:title>"
        "<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>"
        f"{art_tag}"
        f'<res protocolInfo="http-get:*:audio/mpeg:*">{html.escape(url)}</res>'
        "</item></DIDL-Lite>"
    )


# ---------- preset sync ----------------------------------------------------


def _key(host: str, state: str, key: str):
    body = f'<key state="{state}" sender="Gabbo">{key}</key>'.encode()
    req = urllib.request.Request(
        f"http://{host}:8090/key",
        data=body,
        headers={"Content-Type": "application/xml"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass  # release_after_hold returns an XML-parse error but still saves


def _current_preset_url(host: str, n: int) -> str | None:
    try:
        with urllib.request.urlopen(f"http://{host}:8090/presets", timeout=5) as r:
            xml = r.read().decode()
    except Exception:
        return None
    m = re.search(rf'<preset id="{n}"[^>]*>(.*?)</preset>', xml, re.DOTALL)
    if not m:
        return None
    loc = re.search(r'location="([^"]+)"', m.group(1))
    return loc.group(1) if loc else None


def sync_presets(host: str, av, rc, presets: dict, tag: str = "", proxy_port: int | None = None):
    """Save each configured preset onto the speaker so physical button presses
    emit a WebSocket event the bridge can intercept. Skips slots already in
    the right state. Mutes during the operation to hide audio blips."""
    targets = {n: e["url"] for n, e in presets.items() if e.get("url")}
    # Rewrite URLs for proxy so the speaker can actually load them.
    rewritten = {n: rewrite_url_for_proxy(u, proxy_port) for n, u in targets.items()}
    needed = {n: u for n, u in rewritten.items() if _current_preset_url(host, n) != u}
    if not needed:
        print(f"[sync{tag}] all configured presets already match the device — skipping")
        return
    print(f"[sync{tag}] {len(needed)}/{len(targets)} presets need writing: {sorted(needed)}")

    saved_vol = int(rc.GetVolume(InstanceID=0, Channel="Master")["CurrentVolume"])
    rc.SetMute(InstanceID=0, Channel="Master", DesiredMute="1")
    try:
        for n, url in rewritten.items():
            if n not in needed:
                continue
            try:
                av.Stop(InstanceID=0)
            except Exception:
                pass
            time.sleep(0.4)
            # IMPORTANT: empty CurrentURIMetaData. With DIDL, the speaker marks
            # the now-playing item as isPresetable="false" and silently ignores
            # the long-press save. The bridge applies DIDL at runtime in play_preset().
            av.SetAVTransportURI(InstanceID=0, CurrentURI=url, CurrentURIMetaData="")
            av.Play(InstanceID=0, Speed="1")
            time.sleep(3.5)
            _key(host, "press", f"PRESET_{n}")
            time.sleep(0.8)
            _key(host, "release_after_hold", f"PRESET_{n}")
            time.sleep(2.0)
            stored = _current_preset_url(host, n)
            if stored == url:
                print(f"[sync{tag}]  ✓ preset {n} -> {url}")
            else:
                print(f"[sync{tag}]  ✗ preset {n} did not stick (now: {stored})")
        try:
            av.Stop(InstanceID=0)
        except Exception:
            pass
    finally:
        rc.SetMute(InstanceID=0, Channel="Master", DesiredMute="0")
        print(f"[sync{tag}] unmuted, volume {saved_vol}")


# ---------- MQTT -----------------------------------------------------------


def fetch_mqtt_creds() -> dict | None:
    """Supervisor: ask /services/mqtt. Standalone: read MQTT_* env vars."""
    if SUPERVISOR_TOKEN:
        try:
            req = urllib.request.Request(
                f"{SUPERVISOR_URL}/services/mqtt",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r).get("data")
        except Exception as e:
            print(f"[mqtt] supervisor MQTT lookup failed: {e}")
    host = os.environ.get("MQTT_HOST", "").strip()
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.environ.get("MQTT_PORT", "1883")),
        "username": os.environ.get("MQTT_USERNAME", ""),
        "password": os.environ.get("MQTT_PASSWORD", ""),
    }


def publish_discovery(client: mqtt.Client, device_id: str, friendly: str, model: str, presets: dict):
    device = {
        "identifiers": [f"bose_soundtouch_{device_id}"],
        "name": f"Bose {friendly}",
        "manufacturer": "Bose",
        "model": model,
    }
    cmd_base = f"bose_bridge/{device_id}/preset"
    for n in range(1, 7):
        meta = presets.get(n, {})
        url = meta.get("url", "")
        label = meta.get("name") or f"Preset {n}"
        unique = f"bose_{device_id}_preset_{n}"
        cfg = {
            "name": f"Preset {n}: {label}" if url else f"Preset {n}",
            "unique_id": unique,
            "object_id": unique,
            "command_topic": f"{cmd_base}/{n}/command",
            "icon": "mdi:radio",
            "device": device,
            "availability_topic": f"bose_bridge/{device_id}/status",
            "payload_available": "online",
            "payload_not_available": "offline",
        }
        topic = f"homeassistant/button/{unique}/config"
        client.publish(topic, json.dumps(cfg), qos=1, retain=True)
    print(f"[mqtt] published HA discovery for 6 buttons (device {device_id} / {friendly!r})")


# ---------- per-speaker runner --------------------------------------------


def run_speaker(speaker: dict, sync_on_startup: bool, mqtt_client, play_registry: dict, proxy_port: int | None):
    """Per-speaker worker: resolve UPnP services, optionally sync presets,
    register the play callback, publish MQTT discovery, then run the WebSocket
    loop forever with reconnect. Intended to run inside its own thread."""
    host = speaker["host"]
    device_id = speaker["device_id"]
    friendly = speaker["friendly"]
    tag = f" {friendly}"

    print(f"[{friendly}] starting (host={host}, id={device_id}, model={speaker['model']}, proxy_port={proxy_port})")

    presets: dict[int, dict] = {}
    for n, url in speaker["presets"].items():
        meta = lookup_station(url)
        presets[n] = {"url": url, **meta}
        print(f"[meta{tag}] preset {n}: {url} -> {meta or '(no metadata found)'}")

    try:
        av, rc = get_upnp_services(host, device_id, speaker.get("desc_url"))
    except Exception as e:
        print(f"[upnp{tag}] failed to load services: {e} — speaker disabled")
        return

    if sync_on_startup:
        try:
            sync_presets(host, av, rc, presets, tag=tag, proxy_port=proxy_port)
        except Exception as e:
            print(f"[sync{tag}] failed: {e}")

    def play_preset(n: int):
        entry = presets.get(n)
        if not entry:
            print(f"[play{tag}] preset {n} not configured")
            return
        raw_url = entry["url"]
        url = rewrite_url_for_proxy(raw_url, proxy_port)
        didl = build_didl(url, entry)
        print(f"[play{tag}] preset {n} -> {url} (from {raw_url})")
        try:
            try:
                av.Stop(InstanceID=0)
            except Exception:
                pass
            av.SetAVTransportURI(InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl)
            av.Play(InstanceID=0, Speed="1")
        except Exception as e:
            print(f"[play{tag}] failed: {e}")

    play_registry[device_id] = play_preset

    if mqtt_client is not None:
        publish_discovery(mqtt_client, device_id, friendly, speaker["model"], presets)
        mqtt_client.publish(f"bose_bridge/{device_id}/status", "online", qos=1, retain=True)

    def on_ws_message(_ws, msg):
        m = PRESET_RE.search(msg)
        if not m:
            return
        n = int(m.group(1))
        if n == 0:
            return
        print(f"[ws{tag}] physical preset {n} press")
        play_preset(n)

    conn: dict = {"opened_at": None, "last_error": None}

    def on_ws_open(_ws):
        conn["opened_at"] = time.monotonic()
        print(f"[ws{tag}] connected to ws://{host}:8080")

    def on_ws_error(_ws, e):
        conn["last_error"] = str(e) or e.__class__.__name__

    def on_ws_close(_ws, _code, _reason):
        pass

    # Reconnect with exponential backoff. A speaker that locks up (frozen
    # firmware, Wi-Fi drop) is unreachable for minutes — a tight 5s loop
    # just floods the log and hammers the network. Healthy long-lived
    # sessions reset the backoff so a brief blip recovers fast.
    BASE_BACKOFF = 5
    MAX_BACKOFF = 60
    HEALTHY_UPTIME = 60
    backoff = BASE_BACKOFF
    fails = 0
    while True:
        conn["opened_at"] = None
        conn["last_error"] = None
        ws = websocket.WebSocketApp(
            f"ws://{host}:8080",
            subprotocols=["gabbo"],
            on_open=on_ws_open,
            on_message=on_ws_message,
            on_error=on_ws_error,
            on_close=on_ws_close,
        )
        ws.run_forever(ping_interval=30, ping_timeout=10)

        opened_at = conn["opened_at"]
        uptime = (time.monotonic() - opened_at) if opened_at else 0.0
        if uptime >= HEALTHY_UPTIME:
            # Real session that dropped — treat as a one-off, recover fast.
            fails = 0
            backoff = BASE_BACKOFF
            print(f"[ws{tag}] dropped after {int(uptime)}s up — reconnecting in {backoff}s")
        else:
            fails += 1
            err = conn["last_error"] or "unreachable"
            # Log the first few attempts, then only every ~12th, so a frozen
            # speaker doesn't spam the log indefinitely.
            if fails <= 3 or fails % 12 == 0:
                print(f"[ws{tag}] {err} (attempt {fails}) — retrying in {backoff}s")
        time.sleep(backoff)
        if uptime < HEALTHY_UPTIME:
            backoff = min(backoff * 2, MAX_BACKOFF)


# ---------- main loop ------------------------------------------------------


def _setup_mqtt(resolved: list[dict], play_registry: dict):
    """Open a single MQTT connection shared by all speakers. The HA-command
    subscription is a wildcard; messages are dispatched to the right speaker by
    device_id extracted from the topic."""
    creds = fetch_mqtt_creds()
    if not creds:
        print("[mqtt] no MQTT credentials — HA buttons disabled")
        return None

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bose_bridge_{os.getpid()}",
    )
    if creds.get("username"):
        client.username_pw_set(creds["username"], creds.get("password", ""))

    # paho supports only one will per connection. Use the first speaker as the
    # canary — if the bridge dies mid-flight, at least that one shows offline.
    # The others get an explicit "online" on connect and rely on the speaker
    # threads to republish if they exit cleanly.
    if resolved:
        client.will_set(f"bose_bridge/{resolved[0]['device_id']}/status", "offline", qos=1, retain=True)

    def on_connect(c, _u, _f, rc, _p=None):
        print(f"[mqtt] connected (rc={rc})")
        c.subscribe("bose_bridge/+/preset/+/command")

    def on_message(_c, _u, msg):
        m = MQTT_TOPIC_RE.match(msg.topic)
        if not m:
            return
        device_id = m.group(1)
        n = int(m.group(2))
        play = play_registry.get(device_id)
        if not play:
            print(f"[mqtt] command for unknown device {device_id}")
            return
        print(f"[mqtt] device {device_id} preset {n} requested via HA")
        play(n)

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(creds["host"], int(creds.get("port", 1883)), keepalive=60)
        client.loop_start()
        return client
    except Exception as e:
        print(f"[mqtt] connect failed, continuing without HA control: {e}")
        return None


def main():
    cfg = load_options()

    # Start radio search web UI before speaker resolution so Home Assistant
    # Ingress remains available even when presets/speakers still need setup.
    radio_search_server: ThreadingHTTPServer | None = None
    try:
        radio_search_server = start_radio_search_server()
    except Exception as e:
        print(f"[search] failed to start: {e}")

    cfg_speakers = cfg["speakers"]
    if not cfg_speakers:
        print(
            "[cfg] no speakers configured. Radio search stays available; "
            "add preset URLs or speaker entries to enable playback."
        )
        while True:
            time.sleep(3600)

    resolved = resolve_speakers(cfg_speakers)
    if not resolved:
        print("[cfg] no speakers could be resolved — check host/name fields and SSDP reachability.")
        print("[cfg] radio search stays available while the add-on keeps running.")
        while True:
            time.sleep(3600)

    print(f"[main] managing {len(resolved)} speaker(s): {[s['friendly'] for s in resolved]}")

    # Start HTTPS proxy if enabled.
    proxy_server: ThreadingHTTPServer | None = None
    proxy_port: int | None = None
    if cfg.get("https_proxy"):
        proxy_port = cfg["proxy_port"]
        assert proxy_port is not None
        try:
            proxy_server = start_https_proxy(proxy_port)
            print(f"[proxy] HTTPS proxy listening on 127.0.0.1:{proxy_port}")
        except OSError as e:
            print(f"[proxy] failed to start proxy on port {proxy_port}: {e} — HTTPS URLs will not work")
            proxy_port = None

    play_registry: dict[str, Callable[[int], None]] = {}
    mqtt_client = _setup_mqtt(resolved, play_registry)

    sync_on_startup = cfg["sync_presets_on_startup"]
    threads: list[threading.Thread] = []
    for speaker in resolved:
        t = threading.Thread(
            target=run_speaker,
            args=(speaker, sync_on_startup, mqtt_client, play_registry, proxy_port),
            name=f"speaker-{speaker['friendly']}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Speaker threads loop forever (reconnecting on WS failure). Block here so
    # the process stays alive and signals propagate.
    for t in threads:
        t.join()

    # Shut down servers so the process can exit cleanly.
    if proxy_server:
        proxy_server.shutdown()
        proxy_server.server_close()
    if radio_search_server:
        radio_search_server.shutdown()
        radio_search_server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"[main] unhandled error: {e}")
        import traceback; traceback.print_exc()
        raise SystemExit(1)
