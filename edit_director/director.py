"""
Mini Studio — AI Edit Director  :9533
Full pipeline: source → STT → analysis → Edit Plan → OpenCut project → preview → export

Endpoints:
  GET  /                         AI Jobs dashboard (HTML)
  GET  /api/jobs                 List all jobs
  GET  /api/jobs/<id>            Job details + status
  GET  /api/jobs/<id>/edit-plan  Current edit plan JSON
  GET  /api/jobs/<id>/preview    Preview video file
  GET  /api/jobs/<id>/opencut-project  OpenCut project JSON for bridge injection
  GET  /media/<id>/<file>        Serve project files with CORS (for bridge page)
  POST /api/process              Start new job
  POST /api/jobs/<id>/approve    Approve → trigger exports
  POST /api/jobs/<id>/reedit     Re-edit with new instruction (no re-transcribe)
  POST /api/jobs/<id>/revert     Revert to previous edit plan version
  GET  /inject/<id>              Redirect to OpenCut bridge page
"""
import os, json, hashlib, time, uuid, subprocess, threading, shutil, glob, re
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, request, jsonify, send_file, Response, redirect

app = Flask(__name__)

@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"]  = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

# ── Config ──────────────────────────────────────────────────────────────────
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
WHISPER_URL     = os.environ.get("WHISPER_URL",    "http://127.0.0.1:8421")
TTS_URL         = os.environ.get("TTS_URL",        "http://127.0.0.1:8422")     # XTTS fallback
POCKET_TTS_URL  = os.environ.get("POCKET_TTS_URL", "http://127.0.0.1:5020")     # Pocket TTS primary (local)
VOICE_WAV_PATH  = os.environ.get("VOICE_WAV_PATH", "/opt/studio/voices/voice_clone.wav")
OMNIROUTE_URL    = os.environ.get("OMNIROUTE_URL",   "")  # e.g. http://192.168.0.xxx:20128
POLLINATIONS_URL = os.environ.get("POLLINATIONS_URL", "")  # e.g. https://text.pollinations.ai
OPENCUT_URL     = os.environ.get("OPENCUT_URL",   "http://192.168.0.78:9500")
PROJECTS_DIR    = os.environ.get("PROJECTS_DIR",  "/opt/studio/projects")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "7819702619")
DIRECTOR_HOST  = os.environ.get("DIRECTOR_HOST",  "192.168.0.78")
DIRECTOR_PORT  = int(os.environ.get("DIRECTOR_PORT", "9533"))
GROQ_MODEL     = "groq/compound-mini"
SYNC_DIR       = os.environ.get("SYNC_DIR", "/opt/studio/sync")
STYLE_PROFILE_PATH = os.path.join(SYNC_DIR, "editor_profile.json")
SETTINGS_PATH      = os.path.join(SYNC_DIR, "settings.json")
ENV_PATH           = "/opt/studio/.env"
# ── Context Engine config (overridden by settings.json at runtime) ────────────
HA_URL     = os.environ.get("HA_URL",     "http://192.168.0.164:8123")
HA_TOKEN   = os.environ.get("HA_TOKEN",   "")
HA_TRACKER = os.environ.get("HA_TRACKER", "device_tracker.sam_pixel_9")
CONTEXT_DB = os.path.join(SYNC_DIR, "context.db")

os.makedirs(PROJECTS_DIR, exist_ok=True)
os.makedirs(SYNC_DIR, exist_ok=True)

# In-memory job registry
_jobs: dict = {}
_jobs_lock = threading.Lock()
_style_lock = threading.Lock()

# ── Context Engine (Trip Context DB) ─────────────────────────────────────────
import sqlite3

def _ctx_conn():
    conn = sqlite3.connect(CONTEXT_DB, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _init_ctx_db():
    with _ctx_conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS context_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc     TEXT NOT NULL,
            ts_end_utc TEXT,
            source     TEXT NOT NULL,
            device     TEXT,
            latitude   REAL,
            longitude  REAL,
            altitude   REAL,
            accuracy   REAL,
            location_name TEXT,
            event_type TEXT,
            metadata   TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ctx_ts ON context_events(ts_utc)")
        c.commit()

_init_ctx_db()

def ctx_insert(ts_utc, source, device="", lat=None, lon=None, alt=None,
               acc=None, loc_name="", event_type="", metadata=None, ts_end=None):
    with _ctx_conn() as c:
        c.execute("""INSERT INTO context_events
          (ts_utc,ts_end_utc,source,device,latitude,longitude,altitude,accuracy,
           location_name,event_type,metadata)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
          (ts_utc, ts_end, source, device, lat, lon, alt, acc, loc_name, event_type,
           json.dumps(metadata or {})))
        c.commit()

def ctx_query(ts_start: str, ts_end: str) -> list:
    with _ctx_conn() as c:
        rows = c.execute(
            "SELECT * FROM context_events WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc",
            (ts_start, ts_end)
        ).fetchall()
    return [dict(r) for r in rows]

def ctx_stats() -> dict:
    with _ctx_conn() as c:
        total = c.execute("SELECT COUNT(*) FROM context_events").fetchone()[0]
        by_src = {r["source"]: r["n"] for r in c.execute(
            "SELECT source, COUNT(*) as n FROM context_events GROUP BY source")}
    return {"total": total, "by_source": by_src}

# ── Context: video metadata extraction ───────────────────────────────────────

def extract_video_metadata(video_path: str) -> dict:
    """Extract creation_time, GPS, camera, resolution from video file via ffprobe."""
    r = subprocess.run([
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", video_path
    ], capture_output=True, text=True, timeout=20)
    meta = {"path": video_path}
    try:
        j = json.loads(r.stdout)
        fmt  = j.get("format", {})
        tags = fmt.get("tags", {})
        # Creation time (MP4/MOV embedded)
        ct = (tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
              or tags.get("date_time_original"))
        if ct:
            meta["creation_time"] = ct.replace("Z", "+00:00")
        meta["duration"] = float(fmt.get("duration", 0))
        meta["size"]     = int(fmt.get("size", 0))
        # GPS (Apple/Android ISO 6709 location tag: "+51.5074-0.1278/")
        loc = tags.get("location") or tags.get("com.apple.quicktime.location.ISO6709")
        if loc:
            m = re.match(r'([+-]\d+\.?\d*)([+-]\d+\.?\d*)', loc)
            if m:
                meta["latitude"]  = float(m.group(1))
                meta["longitude"] = float(m.group(2))
        # Camera
        make  = tags.get("make")  or tags.get("com.apple.quicktime.make",  "")
        model = tags.get("model") or tags.get("com.apple.quicktime.model", "")
        if make or model:
            meta["camera"] = f"{make} {model}".strip()
        # Video stream
        for stream in j.get("streams", []):
            if stream.get("codec_type") == "video":
                meta["width"]  = stream.get("width", 0)
                meta["height"] = stream.get("height", 0)
                meta["fps"]    = stream.get("avg_frame_rate", "")
                meta["codec"]  = stream.get("codec_name", "")
                # Stream-level creation_time (sometimes more accurate)
                st_tags = stream.get("tags", {})
                if not meta.get("creation_time") and st_tags.get("creation_time"):
                    meta["creation_time"] = st_tags["creation_time"].replace("Z", "+00:00")
                break
    except Exception as e:
        meta["meta_error"] = str(e)
    return meta

# ── Context: HA GPS sync ──────────────────────────────────────────────────────

def sync_ha_gps(date_str: str, entity_id: str = None) -> tuple:
    """Pull device_tracker history from HA for one date. Returns (count, msg)."""
    import requests as req
    entity = entity_id or HA_TRACKER
    if not HA_TOKEN:
        return 0, "HA_TOKEN not set in .env — add it to enable GPS sync"
    try:
        start = f"{date_str}T00:00:00+00:00"
        end   = f"{date_str}T23:59:59+00:00"
        r = req.get(
            f"{HA_URL}/api/history/period/{start}",
            headers={"Authorization": f"Bearer {HA_TOKEN}",
                     "Content-Type": "application/json"},
            params={"filter_entity_id": entity, "end_time": end},
            timeout=15
        )
        r.raise_for_status()
        count = 0
        for entity_history in r.json():
            for state in entity_history:
                ts    = state.get("last_updated") or state.get("last_changed", "")
                attrs = state.get("attributes", {})
                lat   = attrs.get("latitude")
                lon   = attrs.get("longitude")
                if lat is None or lon is None:
                    continue
                meta = {}
                if attrs.get("speed")   is not None: meta["speed_ms"]   = attrs["speed"]
                if attrs.get("heading") is not None: meta["heading"]    = attrs["heading"]
                if attrs.get("altitude")is not None: meta["altitude_m"] = attrs["altitude"]
                ctx_insert(ts_utc=ts, source="ha_gps", device="pixel9",
                           lat=float(lat), lon=float(lon),
                           acc=attrs.get("gps_accuracy"),
                           loc_name=state.get("state", ""),
                           event_type="gps", metadata=meta)
                count += 1
        return count, "ok"
    except Exception as e:
        return 0, str(e)

# ── Context: weather (Open-Meteo archive, free, no key) ───────────────────────

_WEATHER_CODES = {
    0:"Clear sky", 1:"Mainly clear", 2:"Partly cloudy", 3:"Overcast",
    45:"Foggy", 48:"Icy fog", 51:"Light drizzle", 53:"Drizzle", 55:"Heavy drizzle",
    61:"Light rain", 63:"Rain", 65:"Heavy rain", 71:"Light snow", 73:"Snow",
    75:"Heavy snow", 80:"Rain showers", 81:"Showers", 82:"Heavy showers",
    95:"Thunderstorm", 96:"Thunderstorm+hail", 99:"Thunderstorm+heavy hail"
}

def fetch_weather(lat: float, lon: float, date_str: str) -> tuple:
    """Fetch hourly weather from Open-Meteo archive (free, no API key needed).
    Note: data available up to ~5 days ago; use forecast API for recent days."""
    import requests as req
    try:
        r = req.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={"latitude": lat, "longitude": lon,
                    "start_date": date_str, "end_date": date_str,
                    "hourly": "temperature_2m,weathercode,precipitation,windspeed_10m",
                    "timezone": "UTC"},
            timeout=15
        )
        r.raise_for_status()
        d = r.json()
        hourly = d.get("hourly", {})
        times  = hourly.get("time", [])
        temps  = hourly.get("temperature_2m", [])
        wcodes = hourly.get("weathercode", [])
        winds  = hourly.get("windspeed_10m", [None]*len(times))
        for i, t in enumerate(times):
            wc = int(wcodes[i]) if wcodes[i] is not None else 0
            ctx_insert(
                ts_utc=f"{t}:00+00:00", source="weather",
                lat=lat, lon=lon, event_type="weather",
                metadata={"temp_c": temps[i], "weather_code": wc,
                          "description": _WEATHER_CODES.get(wc, str(wc)),
                          "wind_kmh": winds[i]}
            )
        return len(times), "ok"
    except Exception as e:
        return 0, str(e)

# ── Context: Autel flight log (CSV) ──────────────────────────────────────────

def parse_autel_log(file_path: str) -> tuple:
    """Parse Autel Nano flight log CSV. Auto-detects column names."""
    import csv
    count = 0
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            # Auto-detect key columns
            time_col = next((h for h in headers if any(k in h.lower()
                             for k in ("time","date","ts","timestamp"))), None)
            lat_col  = next((h for h in headers if "lat" in h.lower()), None)
            lon_col  = next((h for h in headers if any(k in h.lower()
                             for k in ("lon","lng","long"))), None)
            alt_col  = next((h for h in headers if "alt" in h.lower()), None)
            if not (time_col and lat_col and lon_col):
                return 0, f"Missing required columns. Found: {headers}"
            for row in reader:
                ts  = row.get(time_col, "").strip()
                lat = row.get(lat_col)
                lon = row.get(lon_col)
                alt = row.get(alt_col)
                if not ts or not lat or not lon:
                    continue
                try:
                    lf, lo = float(lat), float(lon)
                    if abs(lf) < 0.001 and abs(lo) < 0.001:
                        continue  # skip null-island positions
                    ctx_insert(ts_utc=ts, source="autel", device="autel_nano",
                               lat=lf, lon=lo,
                               alt=float(alt) if alt else None,
                               event_type="flight",
                               metadata={k: v for k, v in row.items()
                                         if k not in (time_col, lat_col, lon_col)})
                    count += 1
                except (ValueError, TypeError):
                    pass
    except Exception as e:
        return 0, str(e)
    return count, "ok"

# ── Context: clip matching & AI block ────────────────────────────────────────

def match_clip_to_context(creation_time: str, duration: float = 60.0) -> list:
    """Return context events for a clip's time window (+/- 2 min padding)."""
    if not creation_time:
        return []
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
        ts_start = (dt - timedelta(minutes=2)).isoformat()
        ts_end   = (dt + timedelta(seconds=duration + 120)).isoformat()
        return ctx_query(ts_start, ts_end)
    except Exception:
        return []

def format_context_for_ai(ctx_events: list) -> str:
    """Format context events into a compact text block for the AI prompt."""
    if not ctx_events:
        return ""
    gps_evs  = [e for e in ctx_events if e["source"] == "ha_gps"]
    wx_evs   = [e for e in ctx_events if e["source"] == "weather"]
    fly_evs  = [e for e in ctx_events if e["source"] == "autel"]
    parts = []
    if gps_evs:
        latest = gps_evs[-1]
        loc    = latest.get("location_name") or \
                 (f"{latest['latitude']:.4f},{latest['longitude']:.4f}"
                  if latest.get("latitude") else "")
        if loc: parts.append(f"Location: {loc}")
        meta = json.loads(latest.get("metadata") or "{}")
        spd  = meta.get("speed_ms")
        if spd is not None:
            kmh = float(spd) * 3.6
            mov = "stationary" if kmh < 1 else "walking" if kmh < 8 else \
                  "cycling" if kmh < 30 else "driving"
            parts.append(f"Movement: {mov} ({kmh:.0f} km/h)")
    if wx_evs:
        meta = json.loads(wx_evs[0].get("metadata") or "{}")
        desc = meta.get("description", "")
        temp = meta.get("temp_c")
        if desc: parts.append(f"Weather: {desc}")
        if temp is not None: parts.append(f"Temp: {temp:.0f}°C")
    if fly_evs:
        alts = [e["altitude"] for e in fly_evs if e.get("altitude")]
        parts.append(f"Drone: {len(fly_evs)} points" +
                     (f", {min(alts):.0f}–{max(alts):.0f}m" if alts else ""))
    return " | ".join(parts)

# ── Settings management ───────────────────────────────────────────────────────

_DEFAULT_CAMERAS = [
    {"id": "pixel9",  "name": "Pixel 9",        "type": "phone",
     "is_gps_device": True,  "tz_offset": 0,
     "notes": "Main GPS device — tracked via HA device_tracker"},
    {"id": "feiyu",   "name": "Feiyu Pocket 2S", "type": "action_cam",
     "is_gps_device": False, "tz_offset": 0,
     "filename_hint": "FVIDEO,Video,FPV",
     "notes": "4K wearable — timestamp from file metadata or filename"},
    {"id": "autel",   "name": "Autel EVO Nano",  "type": "drone",
     "is_gps_device": False, "tz_offset": 0,
     "filename_hint": "AUTEL,Video",
     "notes": "Drone — use flight log CSV for GPS telemetry"},
]

def load_settings() -> dict:
    defaults = {
        "ha_url":               HA_URL,
        "ha_token":             HA_TOKEN,
        "ha_tracker":           HA_TRACKER,
        "cameras":              _DEFAULT_CAMERAS,
        "context_auto_sync":    True,
        "context_auto_weather": True,
        "context_auto_locate":  True,   # auto look up HA GPS on clip upload
        "timezone_offset":      0,      # local UTC offset in hours (e.g. 1 for BST)
        "pocket_tts_url":       POCKET_TTS_URL,
        "voice_wav":            VOICE_WAV_PATH,
        "opencut_url":          OPENCUT_URL,
        "director_host":        DIRECTOR_HOST,
        "telegram_chat_id":     TELEGRAM_CHAT,
    }
    try:
        if Path(SETTINGS_PATH).exists():
            saved = json.loads(Path(SETTINGS_PATH).read_text())
            for k, v in saved.items():
                defaults[k] = v
    except Exception:
        pass
    return defaults

def save_settings(data: dict) -> str:
    global HA_URL, HA_TOKEN, HA_TRACKER, POCKET_TTS_URL, VOICE_WAV_PATH
    try:
        current = load_settings()
        current.update({k: v for k, v in data.items() if v is not None})
        Path(SETTINGS_PATH).write_text(json.dumps(current, indent=2))
        # Update in-memory config
        HA_URL          = current.get("ha_url",       HA_URL)
        HA_TOKEN        = current.get("ha_token",     HA_TOKEN)
        HA_TRACKER      = current.get("ha_tracker",   HA_TRACKER)
        POCKET_TTS_URL  = current.get("pocket_tts_url", POCKET_TTS_URL)
        VOICE_WAV_PATH  = current.get("voice_wav",    VOICE_WAV_PATH)
        # Write back to .env (non-destructive — only updates keys that are there)
        if Path(ENV_PATH).exists():
            env_text = Path(ENV_PATH).read_text()
            updates = {}
            if "ha_url"         in data: updates["HA_URL"]         = data["ha_url"]
            if "ha_token"       in data and data["ha_token"]: updates["HA_TOKEN"] = data["ha_token"]
            if "ha_tracker"     in data: updates["HA_TRACKER"]     = data["ha_tracker"]
            if "pocket_tts_url" in data: updates["POCKET_TTS_URL"] = data["pocket_tts_url"]
            for key, val in updates.items():
                if re.search(f"^{key}=", env_text, re.MULTILINE):
                    env_text = re.sub(f"^{key}=.*$", f"{key}={val}", env_text, flags=re.MULTILINE)
                else:
                    env_text += f"\n{key}={val}"
            Path(ENV_PATH).write_text(env_text)
        return "ok"
    except Exception as e:
        return str(e)

# ── Context: filename timestamp parser ────────────────────────────────────────

def parse_filename_ts(filename: str, tz_offset_h: int = 0) -> str:
    """Extract ISO UTC timestamp from camera filename patterns.
    Feiyu:  FVIDEO_20260820_230114.mp4
    Generic: 2026-08-20_23-01-14.mp4 / 20260820-230114.mp4
    Returns ISO string or empty string."""
    stem = Path(filename).stem
    patterns = [
        r'(\d{4})-(\d{2})-(\d{2})[_T ](\d{2})-(\d{2})-(\d{2})',   # 2026-08-20_23-01-14
        r'(\d{4})-(\d{2})-(\d{2})[_T ](\d{2}):(\d{2}):(\d{2})',   # 2026-08-20T23:01:14
        r'(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})',         # 20260820_230114
        r'(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})',        # 2026_08_20_23_01_14
    ]
    for pat in patterns:
        m = re.search(pat, stem)
        if m:
            Y, M, D, h, mn, s = m.groups()
            try:
                from datetime import datetime, timedelta, timezone
                dt_local = datetime(int(Y), int(M), int(D), int(h), int(mn), int(s))
                dt_utc   = dt_local - timedelta(hours=tz_offset_h)
                return dt_utc.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            except Exception:
                pass
    return ""

# ── Context: Nominatim reverse geocoding ─────────────────────────────────────

def reverse_geocode(lat: float, lon: float) -> str:
    """Convert GPS coordinates to a place name (Nominatim, free, no key)."""
    import requests as req
    try:
        r = req.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 14},
            headers={"User-Agent": "MiniStudio/1.0 (local vlog builder)"},
            timeout=6
        )
        r.raise_for_status()
        j = r.json()
        addr = j.get("address", {})
        parts = []
        for field in ("suburb", "neighbourhood", "city_district", "city",
                      "town", "village", "county"):
            if addr.get(field) and addr[field] not in parts:
                parts.append(addr[field])
                if len(parts) >= 2:
                    break
        return ", ".join(parts) if parts else j.get("display_name", "")[:60]
    except Exception:
        return ""

# ── Context: instant HA location lookup for a clip timestamp ─────────────────

def ha_locate_at_time(ts_utc: str) -> dict:
    """Look up where the Pixel 9 was at a given UTC timestamp.
    Checks context DB first, then calls HA API if needed.
    Returns {"latitude", "longitude", "location_name"} or {}."""
    if not HA_TOKEN or not ts_utc:
        return {}
    try:
        from datetime import datetime, timedelta
        dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
        win_start = (dt - timedelta(minutes=2)).isoformat()
        win_end   = (dt + timedelta(minutes=2)).isoformat()

        # Check context DB cache first
        cached = ctx_query(win_start, win_end)
        gps_ev = next((e for e in cached
                       if e["source"] == "ha_gps" and e.get("latitude")), None)
        if gps_ev:
            loc = gps_ev.get("location_name", "") or \
                  reverse_geocode(gps_ev["latitude"], gps_ev["longitude"])
            if loc and loc != gps_ev.get("location_name", ""):
                gps_ev["location_name"] = loc  # enrich the cached entry
            return {"latitude": gps_ev["latitude"], "longitude": gps_ev["longitude"],
                    "location_name": loc}

        # Not cached — query HA directly
        import requests as req
        r = req.get(
            f"{HA_URL}/api/history/period/{win_start}",
            headers={"Authorization": f"Bearer {HA_TOKEN}"},
            params={"filter_entity_id": HA_TRACKER,
                    "end_time": win_end, "minimal_response": "false"},
            timeout=8
        )
        r.raise_for_status()
        for entity_hist in r.json():
            for state in entity_hist:
                attrs = state.get("attributes", {})
                lat   = attrs.get("latitude")
                lon   = attrs.get("longitude")
                if lat is None or lon is None:
                    continue
                ts    = state.get("last_updated", ts_utc)
                loc   = state.get("state", "")
                if loc in ("home", "not_home", "unknown", ""):
                    loc = reverse_geocode(lat, lon)
                # Cache in context DB for future use
                ctx_insert(ts_utc=ts, source="ha_gps", device="pixel9",
                           lat=float(lat), lon=float(lon),
                           acc=attrs.get("gps_accuracy"),
                           loc_name=loc, event_type="gps",
                           metadata={"speed_ms": attrs.get("speed"),
                                     "heading":  attrs.get("heading")})
                return {"latitude": float(lat), "longitude": float(lon),
                        "location_name": loc}
    except Exception as e:
        print(f"[context] ha_locate_at_time failed: {e}")
    return {}

# ── Helpers ──────────────────────────────────────────────────────────────────
def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def gen_id() -> str:
    return uuid.uuid4().hex[:10]

def stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def project_dir(jid: str) -> Path:
    return Path(PROJECTS_DIR) / jid

def edit_plan_dir(jid: str) -> Path:
    return project_dir(jid) / "edit-plan"

def save_job(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid, {})
    p = project_dir(jid)
    p.mkdir(parents=True, exist_ok=True)
    (p / "job.json").write_text(json.dumps(job, indent=2, default=str))

def load_all_jobs():
    for p in sorted(Path(PROJECTS_DIR).glob("*/job.json")):
        try:
            job = json.loads(p.read_text())
            jid = job["job_id"]
            with _jobs_lock:
                _jobs[jid] = job
        except Exception:
            pass

load_all_jobs()

def update_step(jid: str, step: str, status: str = "running", msg: str = ""):
    with _jobs_lock:
        job = _jobs.get(jid, {})
        job.setdefault("steps", {})[step] = {"status": status, "msg": msg, "ts": now_iso()}
        if status == "running":
            job["current_step"] = step
        _jobs[jid] = job
    save_job(jid)

def set_error(jid: str, msg: str):
    with _jobs_lock:
        _jobs[jid]["status"] = "error"
        _jobs[jid]["error"] = msg
    save_job(jid)

def send_telegram(msg: str):
    if not TELEGRAM_TOKEN:
        return
    try:
        import urllib.request, urllib.parse
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data, timeout=10
        )
    except Exception as e:
        print(f"[director] Telegram error: {e}")

# ── TTS (Pocket TTS primary, XTTS fallback) ──────────────────────────────────
def punctuate_for_tts(text: str) -> str:
    """Return text ready for Pocket TTS.

    Local Whisper (faster-whisper) already produces punctuated output natively,
    so no Groq call is needed — zero API cost.  We only do a cheap local pass
    to ensure the first letter is capitalised and the text ends with a sentence
    terminator, which Pocket TTS uses to set the final inflection.
    """
    t = text.strip()
    if not t:
        return t
    # Capitalise first character
    t = t[0].upper() + t[1:]
    # Ensure sentence ends with punctuation (Pocket TTS inflects the tail)
    if t[-1] not in ".!?":
        t += "."
    return t


def generate_tts_audio(text: str, out_path: str) -> bool:
    """Generate TTS audio: Pocket TTS → XTTS fallback.
    Text is punctuated via Groq first so Pocket TTS inflects correctly.
    Returns True on success."""
    import requests as req

    # Punctuation pass (cheap Groq call) so TTS sounds natural
    clean_text = punctuate_for_tts(text)

    # ── Pocket TTS (primary) ──────────────────────────────────────────────────
    try:
        voice_path = Path(VOICE_WAV_PATH)
        files = {"text": (None, clean_text)}
        if voice_path.exists():
            files["voice_wav"] = (voice_path.name, voice_path.open("rb"), "audio/wav")
        else:
            files["voice_url"] = (None, "alba")   # built-in fallback voice
        r = req.post(
            f"{POCKET_TTS_URL}/tts",
            files=files,
            headers={"User-Agent": "MiniStudio/1.0"},
            timeout=120
        )
        r.raise_for_status()
        if len(r.content) > 1000:   # real audio, not an error JSON
            Path(out_path).write_bytes(r.content)
            print(f"[director] Pocket TTS generated {len(r.content)//1024}KB → {Path(out_path).name}")
            return True
    except Exception as e:
        print(f"[director] Pocket TTS failed ({e}), trying XTTS...")

    # ── XTTS fallback ─────────────────────────────────────────────────────────
    try:
        r = req.post(
            f"{TTS_URL}/generate",
            json={"text": clean_text, "language": "en"},
            headers={"User-Agent": "MiniStudio/1.0"},
            timeout=120
        )
        r.raise_for_status()
        if len(r.content) > 1000:
            Path(out_path).write_bytes(r.content)
            print(f"[director] XTTS fallback generated {len(r.content)//1024}KB")
            return True
    except Exception as e:
        print(f"[director] XTTS fallback also failed: {e}")

    return False


# ── Editor Style Profile ─────────────────────────────────────────────────────
# Learns Sam's editing preferences from every job decision.
# Stored in SYNC_DIR so Syncthing keeps both machines identical.

_PROFILE_DEFAULTS = {
    "version": 1,
    "jobs_total": 0,
    "jobs_approved": 0,
    "jobs_rejected": 0,
    "jobs_reedited": 0,
    "manual_rules": [],
    "reedit_instructions": [],
    "common_instructions": [],
    "style_summary": "",
    "avg_cuts_per_job": 0.0,
    "avg_kept_ratio": 0.0,
    "edit_history": []
}

def load_style_profile() -> dict:
    with _style_lock:
        try:
            return json.loads(Path(STYLE_PROFILE_PATH).read_text())
        except Exception:
            return dict(_PROFILE_DEFAULTS)

def save_style_profile(profile: dict):
    with _style_lock:
        profile["updated_at"] = now_iso()
        Path(STYLE_PROFILE_PATH).write_text(json.dumps(profile, indent=2))

def _rebuild_style_summary(profile: dict) -> str:
    """Build a plain-English summary of Sam's editing style from accumulated history."""
    n = profile["jobs_approved"]
    if n == 0:
        return ""
    lines = []
    avg_cuts = profile.get("avg_cuts_per_job", 0)
    avg_kept = profile.get("avg_kept_ratio", 1.0)
    pct_kept = round(avg_kept * 100)
    if avg_cuts:
        lines.append(f"Typically makes {avg_cuts:.1f} cuts per video, keeping ~{pct_kept}% of source duration.")
    instrs = profile.get("common_instructions", [])
    if instrs:
        lines.append(f"Common instructions: {'; '.join(instrs[:5])}.")
    reeedits = profile.get("reedit_instructions", [])
    if reeedits:
        lines.append(f"Often re-edits with: {'; '.join(reeedits[:4])}.")
    rules = profile.get("manual_rules", [])
    if rules:
        lines.append(f"Manual rules: {' | '.join(rules)}.")
    lines.append(f"Based on {n} approved job{'s' if n != 1 else ''}.")
    return " ".join(lines)

def record_edit_decision(jid: str, action: str, plan: dict = None, instruction: str = ""):
    """Record approve / reject / reedit and update the style profile."""
    profile = load_style_profile()
    profile["jobs_total"] = profile.get("jobs_total", 0) + 1

    entry = {
        "job_id": jid,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "action": action,
        "instruction": instruction or ""
    }

    if action == "approved" and plan:
        cuts   = len(plan.get("cuts",  []))
        clips  = len(plan.get("clips", []))
        src    = plan.get("source", {}).get("duration", 0)
        out    = sum((c.get("timeline_end", 0) - c.get("timeline_start", 0))
                     for c in plan.get("clips", []))
        ratio  = round(out / src, 3) if src else 1.0
        entry.update({"cuts": cuts, "clips": clips,
                      "source_dur": round(src), "output_dur": round(out), "kept_ratio": ratio})
        profile["jobs_approved"] = profile.get("jobs_approved", 0) + 1

        # Rolling averages
        n   = profile["jobs_approved"]
        old_cuts  = profile.get("avg_cuts_per_job", 0)
        old_ratio = profile.get("avg_kept_ratio",   1.0)
        profile["avg_cuts_per_job"] = round(old_cuts + (cuts  - old_cuts)  / n, 2)
        profile["avg_kept_ratio"]   = round(old_ratio + (ratio - old_ratio) / n, 3)

        # Track instruction
        if instruction:
            instr_list = profile.setdefault("common_instructions", [])
            if instruction not in instr_list:
                instr_list.insert(0, instruction)
            profile["common_instructions"] = instr_list[:10]

    elif action == "rejected":
        profile["jobs_rejected"] = profile.get("jobs_rejected", 0) + 1

    elif action == "reedited" and instruction:
        profile["jobs_reedited"] = profile.get("jobs_reedited", 0) + 1
        ri = profile.setdefault("reedit_instructions", [])
        if instruction not in ri:
            ri.insert(0, instruction)
        profile["reedit_instructions"] = ri[:10]

    # Keep last 50 history entries
    history = profile.setdefault("edit_history", [])
    history.insert(0, entry)
    profile["edit_history"] = history[:50]

    profile["style_summary"] = _rebuild_style_summary(profile)
    save_style_profile(profile)
    print(f"[director] Style profile updated: {action} for {jid[:8]}")

def build_style_context() -> str:
    """Return a prompt-ready string describing Sam's editing style. Empty if no history yet."""
    profile = load_style_profile()
    if profile.get("jobs_approved", 0) == 0 and not profile.get("manual_rules"):
        return ""
    parts = []
    summary = profile.get("style_summary", "")
    if summary:
        parts.append(f"Editor style (learned from {profile['jobs_approved']} approved jobs): {summary}")
    rules = profile.get("manual_rules", [])
    if rules:
        parts.append("Editor's own rules (always follow these):\n" +
                     "\n".join(f"  - {r}" for r in rules))
    return "\n\n".join(parts)


# ── Whisper STT ──────────────────────────────────────────────────────────────
def run_whisper_groq(video_path: str) -> dict:
    """Transcribe via Groq Whisper API (whisper-large-v3-turbo).
    Returns transcript dict compatible with local Whisper format."""
    if not GROQ_API_KEY:
        raise ValueError("No GROQ_API_KEY")
    import requests as req
    with open(video_path, "rb") as f:
        r = req.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "User-Agent": "MiniStudio/1.0"
            },
            files={"file": (Path(video_path).name, f, "application/octet-stream")},
            data={
                "model": "whisper-large-v3-turbo",
                "response_format": "verbose_json",
                "timestamp_granularities": "segment",
                "language": "en",
            },
            timeout=180
        )
    r.raise_for_status()
    resp = r.json()
    # Normalise to match local Whisper service format
    segments = []
    for seg in resp.get("segments", []):
        segments.append({
            "id":    seg.get("id", 0),
            "start": seg.get("start", 0),
            "end":   seg.get("end", 0),
            "text":  seg.get("text", "").strip(),
        })
    return {
        "text":     resp.get("text", "").strip(),
        "language": resp.get("language", "en"),
        "duration": resp.get("duration", 0) or (segments[-1]["end"] if segments else 0),
        "segments": segments,
        "source":   "groq-whisper-large-v3-turbo",
    }


def run_whisper_local(video_path: str) -> dict:
    """Transcribe via local Whisper service at WHISPER_URL (fallback)."""
    import requests as req
    with open(video_path, "rb") as f:
        r = req.post(
            f"{WHISPER_URL}/transcribe",
            files={"file": (Path(video_path).name, f, "video/mp4")},
            headers={"User-Agent": "MiniStudio/1.0"},
            timeout=300,
        )
    r.raise_for_status()
    result = r.json()
    result.setdefault("source", "local-whisper")
    return result


def run_whisper(video_path: str) -> dict:
    """Cascade: local Whisper first (free) → Groq Whisper only if local fails.
    Groq is saved for punctuation/TTS pass, not spent on the main transcript."""
    try:
        result = run_whisper_local(video_path)
        print(f"[director] Transcribed locally: {len(result.get('segments',[]))} segs, "
              f"{result.get('duration',0):.1f}s")
        return result
    except Exception as e:
        print(f"[director] Local Whisper failed ({e}), falling back to Groq...")
    return run_whisper_groq(video_path)

# ── Video Analysis ────────────────────────────────────────────────────────────
def _measure_rms_db(video_path: str) -> float:
    """Return mean RMS loudness in dBFS using ffmpeg volumedetect."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path, "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60
        )
        m = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", r.stderr)
        return float(m.group(1)) if m else -30.0
    except Exception:
        return -30.0


def analyze_video(video_path: str, duration: float) -> dict:
    """FFprobe-based silence detection with adaptive noise threshold.

    Measures actual audio loudness first, then sets silence threshold
    relative to the mean: loud/shouted audio gets a higher threshold
    so silences are still detected correctly.
    """
    analysis = {"duration": duration, "silences": [], "audio_levels": [], "error": None}
    try:
        # Measure audio level to set adaptive threshold
        mean_db = _measure_rms_db(video_path)
        # Silence threshold = mean - 15 dB (i.e. 15 dB below average speech)
        # Clamp between -45 dB (very quiet) and -18 dB (very loud/shouting)
        noise_threshold = max(-45.0, min(-18.0, mean_db - 15.0))
        analysis["mean_rms_db"] = round(mean_db, 1)
        analysis["silence_threshold_db"] = round(noise_threshold, 1)
        print(f"[director] Audio analysis: mean={mean_db:.1f}dB → silence threshold={noise_threshold:.1f}dB")

        cmd = [
            "ffmpeg", "-i", video_path, "-af",
            f"silencedetect=noise={noise_threshold:.1f}dB:d=0.5",
            "-f", "null", "-"
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = r.stderr

        starts, ends = [], []
        for line in out.splitlines():
            if "silence_start" in line:
                m = re.search(r"silence_start: ([\d.]+)", line)
                if m: starts.append(float(m.group(1)))
            elif "silence_end" in line:
                m = re.search(r"silence_end: ([\d.]+)", line)
                if m: ends.append(float(m.group(1)))

        silences = []
        for s, e in zip(starts, ends):
            dur = e - s
            if dur >= 0.5:
                silences.append({"start": round(s, 3), "end": round(e, 3), "duration": round(dur, 3)})
        analysis["silences"] = silences
        analysis["silence_count"] = len(silences)

        # Duration via ffprobe
        r2 = subprocess.run([
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", video_path
        ], capture_output=True, text=True, timeout=30)
        try:
            analysis["duration"] = float(r2.stdout.strip())
        except Exception:
            pass
    except Exception as e:
        analysis["error"] = str(e)
    return analysis

# ── LLM cascade (OmniRoute → Pollinations → Groq) ────────────────────────────

class DeferredError(Exception):
    """Raised when all free LLMs are unavailable and video is not urgent."""

def call_llm(messages: list, priority: str = "normal") -> tuple:
    """Try LLMs in cost order. Returns (content_str, provider_name).

    Cascade:
      1. OmniRoute  — your own router, zero cost, always first
      2. Pollinations — free tier, second
      3. Groq — only if priority=='rush' AND both above failed
      4. DeferredError — if not rush and all free options exhausted
    """
    import requests as req
    errors = []

    # ── 1. OmniRoute ─────────────────────────────────────────────────────────
    if OMNIROUTE_URL:
        try:
            r = req.post(
                f"{OMNIROUTE_URL.rstrip('/')}/v1/chat/completions",
                headers={"Content-Type": "application/json",
                         "User-Agent": "MiniStudio/1.0"},
                json={"model": "auto", "messages": messages,
                      "response_format": {"type": "json_object"},
                      "max_tokens": 8000, "stream": False},
                timeout=120
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content and "{" in content:
                print("[director] LLM: OmniRoute OK")
                return content, "omniroute"
            errors.append("OmniRoute: empty response")
        except Exception as e:
            errors.append(f"OmniRoute: {e}")
            print(f"[director] OmniRoute failed: {e}")

    # ── 2. Pollinations ──────────────────────────────────────────────────────
    if POLLINATIONS_URL:
        try:
            r = req.post(
                f"{POLLINATIONS_URL.rstrip('/')}/v1/chat/completions",
                headers={"Content-Type": "application/json",
                         "User-Agent": "MiniStudio/1.0"},
                json={"model": "openai-large", "messages": messages,
                      "jsonMode": True, "max_tokens": 8000},
                timeout=120
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content and "{" in content:
                print("[director] LLM: Pollinations OK")
                return content, "pollinations"
            errors.append("Pollinations: empty response")
        except Exception as e:
            errors.append(f"Pollinations: {e}")
            print(f"[director] Pollinations failed: {e}")

    # ── 3. Groq — only if rush ────────────────────────────────────────────────
    if priority == "rush":
        if not GROQ_API_KEY:
            raise ValueError(f"Rush job but no GROQ_API_KEY. Prior errors: {errors}")
        print(f"[director] LLM: free options exhausted, using Groq (rush job)")
        r = req.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json",
                     "User-Agent": "MiniStudio/1.0"},
            json={"model": GROQ_MODEL, "messages": messages,
                  "response_format": {"type": "json_object"},
                  "temperature": 0.1, "max_tokens": 8000, "stream": False},
            timeout=180
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content, "groq"

    # ── 4. Not rush + no free LLM → defer ────────────────────────────────────
    raise DeferredError(f"No free LLM available (not rush). Errors: {errors}")


# ── LLM Edit Plan generation ──────────────────────────────────────────────────
EDIT_PLAN_SCHEMA = {
    "description": "AI Edit Plan — all decisions the editor will apply",
    "type": "object",
    "required": ["version", "project_id", "source", "project", "cuts", "clips", "tts", "audio_tracks", "exports", "review"],
    "properties": {
        "version": {"type": "string", "enum": ["1.0"]},
        "project_id": {"type": "string"},
        "channel": {"type": "string"},
        "source": {
            "type": "object",
            "required": ["file", "duration", "sha256"],
            "properties": {"file": {"type": "string"}, "duration": {"type": "number"}, "sha256": {"type": "string"}}
        },
        "project": {
            "type": "object",
            "properties": {
                "target_duration": {"type": "number"},
                "aspect_ratio": {"type": "string"},
                "resolution": {"type": "string"},
                "fps": {"type": "number"}
            }
        },
        "cuts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "source_start", "source_end", "action", "reason", "confidence"],
                "properties": {
                    "id": {"type": "string"},
                    "source_start": {"type": "number"},
                    "source_end": {"type": "number"},
                    "action": {"type": "string", "enum": ["remove"]},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "needs_review": {"type": "boolean"}
                }
            }
        },
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "source_start", "source_end", "timeline_start", "timeline_end"],
                "properties": {
                    "id": {"type": "string"},
                    "source_start": {"type": "number"},
                    "source_end": {"type": "number"},
                    "timeline_start": {"type": "number"},
                    "timeline_end": {"type": "number"},
                    "action": {"type": "string", "enum": ["keep"]}
                }
            }
        },
        "tts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text", "timeline_start"],
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "timeline_start": {"type": "number"},
                    "expected_duration": {"type": "number"},
                    "generated_file": {"type": ["string", "null"]}
                }
            }
        },
        "audio_tracks": {"type": "array"},
        "exports": {"type": "array"},
        "review": {
            "type": "object",
            "properties": {
                "ai_confidence": {"type": "number"},
                "needs_review_count": {"type": "integer"},
                "needs_review_ids": {"type": "array"}
            }
        }
    }
}

def generate_edit_plan(jid: str, job: dict, transcript: dict, analysis: dict) -> dict:
    """Call Groq LLM to produce a valid Edit Plan JSON."""
    import requests as req

    source_path = job["source_path"]
    source_dur  = analysis.get("duration", transcript.get("duration", 60))
    sha = job["source_sha256"]
    channel_id = job.get("channel_id", "")
    instruction = job.get("instruction", "Edit this video to remove silences and fillers. Keep the best moments.")

    # Build transcript summary (cap at 4000 chars)
    transcript_text = transcript.get("text", "")[:3000]
    segments_preview = json.dumps(transcript.get("segments", [])[:30], indent=None)[:1500]
    silence_summary = json.dumps(analysis.get("silences", [])[:20], indent=None)[:800]

    # Build channel profile context
    channels_path = "/opt/studio/channels.json"
    channel_ctx = ""
    if channel_id and os.path.exists(channels_path):
        try:
            channels = json.loads(Path(channels_path).read_text())
            ch = channels.get(channel_id, {})
            if ch:
                channel_ctx = f"\nChannel: {ch.get('name', '')} | Style: {ch.get('style', '')} | Platform: {ch.get('platform', '')}"
        except Exception:
            pass

    system_prompt = """You are an AI video editor. Given a transcript, silence analysis, and editing instruction, produce a JSON Edit Plan.

RULES:
- cuts[] = segments to REMOVE from source (silences, fillers, boring parts)
- clips[] = segments to KEEP, derived from removing the cuts. Timeline_start must be sequential with no gaps.
- Every cut and clip MUST have a unique stable ID (cut_001, cut_002... / clip_001, clip_002...)
- Every cut MUST have a confidence value between 0 and 1
- Cuts with confidence < 0.7 MUST have needs_review: true
- clips[] timeline positions must be contiguous (no overlap, no gap)
- The sum of clip durations must equal source duration minus sum of cut durations
- tts[] entries are optional AI voiceover segments. Leave empty [] if not needed.
- audio_tracks must include: original, my_voice, ai_voice, music, sfx
- exports must include: export_ai (ai_voice ON, my_voice OFF), export_myvoice (ai_voice OFF, my_voice ON), export_multi (both as streams)
- Output ONLY valid JSON matching the schema. No explanation text.
"""

    # Inject Sam's learned editing style into the prompt
    style_ctx = build_style_context()
    if style_ctx:
        system_prompt += f"\n\n## Editor Preferences (learned — always respect these):\n{style_ctx}\n"

    user_prompt = f"""Source video: {Path(source_path).name}
Duration: {source_dur:.1f} seconds
SHA256: {sha[:16]}...{channel_ctx}

Instruction: {instruction}

Transcript:
{transcript_text}

Transcript segments (first 30):
{segments_preview}

Detected silences (first 20):
{silence_summary}

Generate the Edit Plan JSON. Remove all detected silences longer than 1.0 second. Remove obvious filler words if identifiable from transcript. Keep the rest.

Output the complete Edit Plan JSON now:"""

    priority = job.get("priority", "normal")
    raw_content, provider = call_llm([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ], priority=priority)
    print(f"[director] LLM raw response from {provider} ({len(raw_content)} chars): {raw_content[:100]}...")

    # Parse and fill in required fields
    try:
        plan = json.loads(raw_content)
    except json.JSONDecodeError as e:
        # Try to extract JSON from response
        m = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if m:
            plan = json.loads(m.group(0))
        else:
            raise ValueError(f"LLM did not return valid JSON: {str(e)}: {raw_content[:200]}")
    if not isinstance(plan, dict):
        raise ValueError(f"LLM returned non-dict: {type(plan).__name__}: {repr(raw_content)[:100]}")
    plan["version"] = "1.0"
    plan["project_id"] = jid
    plan["source"] = {
        "file": source_path,
        "duration": source_dur,
        "sha256": sha
    }

    # ── Normalize audio_tracks (LLM may return strings or wrong schema) ──
    raw_at = plan.get("audio_tracks", [])
    canonical_tracks = [
        {"id": "original", "type": "original_audio", "enabled": True},
        {"id": "my_voice",  "type": "voice",          "enabled": True},
        {"id": "ai_voice",  "type": "voice",          "enabled": True},
        {"id": "music",     "type": "music",          "enabled": True},
        {"id": "sfx",       "type": "sfx",            "enabled": True}
    ]
    if (not raw_at or not isinstance(raw_at, list)
            or not isinstance(raw_at[0], dict) or "id" not in raw_at[0]):
        plan["audio_tracks"] = canonical_tracks

    # ── Normalize exports ──
    raw_ex = plan.get("exports", [])
    canonical_exports = [
        {"id": "export_ai",      "output": f"exports/{jid}_AI.mp4",
         "audio_tracks": {"original": True, "my_voice": False, "ai_voice": True, "music": True, "sfx": True}},
        {"id": "export_myvoice", "output": f"exports/{jid}_MyVoice.mp4",
         "audio_tracks": {"original": True, "my_voice": True, "ai_voice": False, "music": True, "sfx": True}},
        {"id": "export_multi",   "output": f"exports/{jid}_MultiAudio.mp4",
         "audio_streams": [
             {"label": "AI Voice", "tracks": ["original", "ai_voice", "music", "sfx"]},
             {"label": "My Voice", "tracks": ["original", "my_voice", "music", "sfx"]}
         ]}
    ]
    if (not raw_ex or not isinstance(raw_ex, list)
            or not isinstance(raw_ex[0], dict) or "id" not in raw_ex[0]):
        plan["exports"] = canonical_exports

    # ── Normalize cuts — add missing action/reason fields ──
    for i, c in enumerate(plan.get("cuts", [])):
        if "action" not in c:
            c["action"] = "remove"
        if "reason" not in c:
            c["reason"] = "silence_or_filler"
        if "id" not in c:
            c["id"] = f"cut_{i+1:03d}"
        if "confidence" not in c:
            c["confidence"] = 0.8

    # ── Normalize clips — add missing action/timeline fields ──
    for i, c in enumerate(plan.get("clips", [])):
        if "action" not in c:
            c["action"] = "keep"
        if "id" not in c:
            c["id"] = f"clip_{i+1:03d}"

    # ── Always rebuild timeline positions from source ranges ──
    plan = auto_fix_plan(plan)

    # ── Derive cut source positions from gaps between clips ──
    # Groq compound-mini often omits source_start/source_end on cuts.
    # Cuts are the removed segments between kept clips (or before first / after last).
    clips_by_src = sorted(plan.get("clips", []), key=lambda x: x.get("source_start", 0))
    src_dur = plan.get("source", {}).get("duration", source_dur)
    cuts = plan.get("cuts", [])
    for i, cut in enumerate(cuts):
        if cut.get("source_start") is None or cut.get("source_end") is None:
            if i < len(clips_by_src) - 1:
                cut["source_start"] = round(clips_by_src[i].get("source_end", 0), 3)
                cut["source_end"]   = round(clips_by_src[i + 1].get("source_start", 0), 3)
            elif clips_by_src:
                # Trailing cut after last clip — runs to end of source
                cut["source_start"] = round(clips_by_src[-1].get("source_end", 0), 3)
                cut["source_end"]   = round(src_dur, 3)
    # Drop zero-duration cuts (e.g. trailing cut when last clip ends at source end)
    plan["cuts"] = [c for c in cuts
                    if c.get("source_start") is None
                    or c.get("source_end") is None
                    or c.get("source_end", 0) > c.get("source_start", 0)]

    if not plan.get("tts"):
        plan["tts"] = []

    # ── Normalize review ──
    if not plan.get("review") or "ai_confidence" not in plan.get("review", {}):
        needs = [c["id"] for c in plan.get("cuts", []) if c.get("needs_review")]
        plan["review"] = {
            "ai_confidence": round(
                sum(c.get("confidence", 0.8) for c in plan.get("cuts", [])) / max(len(plan.get("cuts", [])), 1), 3
            ),
            "needs_review_count": len(needs),
            "needs_review_ids": needs
        }

    return plan


# ── Edit Plan Validator ────────────────────────────────────────────────────────
def validate_edit_plan(plan: dict, source_path: str) -> list:
    """Return list of error strings. Empty = valid."""
    errors = []

    # Schema checks
    for f in ("version", "project_id", "source", "cuts", "clips", "audio_tracks", "exports"):
        if f not in plan:
            errors.append(f"Missing required field: {f}")

    cuts = plan.get("cuts", [])
    clips = plan.get("clips", [])
    source_dur = plan.get("source", {}).get("duration", 0)

    # Unique IDs
    cut_ids = [c.get("id") for c in cuts]
    clip_ids = [c.get("id") for c in clips]
    if len(set(cut_ids)) != len(cut_ids):
        errors.append("Duplicate cut IDs")
    if len(set(clip_ids)) != len(clip_ids):
        errors.append("Duplicate clip IDs")

    # All cuts have confidence
    for c in cuts:
        if "confidence" not in c:
            errors.append(f"Cut {c.get('id')} missing confidence")
        if c.get("source_start", 0) >= c.get("source_end", 0):
            errors.append(f"Cut {c.get('id')} has start >= end")

    # Timeline continuity check
    if clips:
        sorted_clips = sorted(clips, key=lambda x: x.get("timeline_start", 0))
        expected_start = 0.0
        for clip in sorted_clips:
            ts = clip.get("timeline_start", 0)
            te = clip.get("timeline_end", 0)
            if abs(ts - expected_start) > 0.1:
                errors.append(f"Timeline gap at clip {clip.get('id')}: expected start {expected_start:.3f}, got {ts:.3f}")
            if ts >= te:
                errors.append(f"Clip {clip.get('id')} has start >= end")
            expected_start = te

    # Source file existence
    if not os.path.exists(source_path):
        errors.append(f"Source file not found: {source_path}")

    # TTS text must not be empty
    for t in plan.get("tts", []):
        if not t.get("text", "").strip():
            errors.append(f"TTS entry {t.get('id')} has empty text")

    return errors

def auto_fix_plan(plan: dict) -> dict:
    """Fix common timeline issues automatically."""
    clips = sorted(plan.get("clips", []), key=lambda x: x.get("source_start", 0))
    if not clips:
        return plan

    # Rebuild timeline_start/end from sorted clips
    cursor = 0.0
    for clip in clips:
        dur = clip.get("source_end", 0) - clip.get("source_start", 0)
        if dur <= 0:
            dur = 0.001
        clip["timeline_start"] = round(cursor, 3)
        clip["timeline_end"] = round(cursor + dur, 3)
        cursor += dur

    plan["clips"] = clips
    return plan


# ── OpenCut Project Builder ────────────────────────────────────────────────────
def build_opencut_project(jid: str, plan: dict) -> dict:
    """Convert Edit Plan to a valid OpenCut TProject JSON."""
    project_uuid = stable_uuid(jid)
    now = datetime.now(timezone.utc).isoformat()

    clips = sorted(plan.get("clips", []), key=lambda x: x.get("timeline_start", 0))

    # Calculate total timeline duration
    total_dur = max((c.get("timeline_end", 0) for c in clips), default=0)

    # Generate stable mediaId for source video
    source_media_id = stable_uuid(f"media-{jid}-source")

    # Build video elements from clips
    video_elements = []
    for clip in clips:
        clip_dur = clip["timeline_end"] - clip["timeline_start"]
        trim_start = clip["source_start"]
        trim_end = 0  # trimEnd is from end of source, trimStart is from beginning
        video_elements.append({
            "id": stable_uuid(f"ve-{jid}-{clip['id']}"),
            "name": clip["id"],
            "type": "video",
            "mediaId": source_media_id,
            "duration": round(clip_dur, 3),
            "startTime": round(clip["timeline_start"], 3),
            "trimStart": round(trim_start, 3),
            "trimEnd": 0,
            "sourceDuration": plan["source"]["duration"],
            "transform": {
                "x": 0, "y": 0, "width": 1, "height": 1,
                "rotation": 0, "scaleX": 1, "scaleY": 1,
                "originX": 0.5, "originY": 0.5
            },
            "opacity": 1.0,
            "muted": False
        })

    # Audio tracks
    tracks = [
        {
            "id": stable_uuid(f"vt-{jid}-main"),
            "name": "Video",
            "type": "video",
            "isMain": True,
            "muted": False,
            "hidden": False,
            "volume": 1.0,
            "elements": video_elements
        },
        {
            "id": stable_uuid(f"at-{jid}-original"),
            "name": "Original Audio",
            "type": "audio",
            "muted": False,
            "volume": 1.0,
            "elements": []
        },
        {
            "id": stable_uuid(f"at-{jid}-my_voice"),
            "name": "My Voice",
            "type": "audio",
            "muted": False,
            "volume": 1.0,
            "elements": []
        },
        {
            "id": stable_uuid(f"at-{jid}-ai_voice"),
            "name": "AI Voice",
            "type": "audio",
            "muted": False,
            "volume": 1.0,
            "elements": []
        },
        {
            "id": stable_uuid(f"at-{jid}-music"),
            "name": "Music",
            "type": "audio",
            "muted": True,
            "volume": 0.4,
            "elements": []
        },
        {
            "id": stable_uuid(f"at-{jid}-sfx"),
            "name": "SFX",
            "type": "audio",
            "muted": True,
            "volume": 0.7,
            "elements": []
        }
    ]

    # TTS audio elements → AI Voice track
    tts_entries = plan.get("tts", [])
    ai_voice_track = next(t for t in tracks if t["name"] == "AI Voice")
    for tts in tts_entries:
        if tts.get("generated_file") and os.path.exists(tts["generated_file"]):
            tts_media_id = stable_uuid(f"media-{jid}-{tts['id']}")
            dur = tts.get("expected_duration", 3.0)
            ai_voice_track["elements"].append({
                "id": stable_uuid(f"ae-{jid}-{tts['id']}"),
                "name": tts["id"],
                "type": "audio",
                "sourceType": "library",
                "sourceUrl": f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}/media/{jid}/tts/{tts['id']}.wav",
                "volume": 1.0,
                "duration": dur,
                "startTime": tts.get("timeline_start", 0),
                "trimStart": 0,
                "trimEnd": 0
            })

    # Add review markers for needs_review clips/cuts
    needs_review_ids = plan.get("review", {}).get("needs_review_ids", [])
    markers = []
    for cut in plan.get("cuts", []):
        if cut.get("id") in needs_review_ids or cut.get("needs_review"):
            # Find corresponding clip position
            for clip in clips:
                if abs(clip.get("source_start", 0) - cut.get("source_start", -999)) < 0.5:
                    markers.append({
                        "id": stable_uuid(f"mk-{jid}-{cut['id']}"),
                        "time": clip["timeline_start"],
                        "note": f"⚠ Review: {cut.get('reason', 'AI flagged')}",
                        "color": "yellow",
                        "createdAt": int(time.time() * 1000)
                    })
                    break

    scene = {
        "id": stable_uuid(f"scene-{jid}"),
        "name": "Main",
        "isMain": True,
        "tracks": tracks,
        "bookmarks": [],
        "markers": markers,
        "createdAt": now,
        "updatedAt": now
    }

    project = {
        "metadata": {
            "id": project_uuid,
            "name": plan.get("channel", "AI Edit") + f" — {Path(plan['source']['file']).stem}",
            "duration": round(total_dur, 3),
            "createdAt": now,
            "updatedAt": now
        },
        "scenes": [scene],
        "currentSceneId": scene["id"],
        "settings": {
            "fps": plan.get("project", {}).get("fps", 30),
            "canvasSize": {"width": 1920, "height": 1080},
            "background": {"type": "color", "color": "#000000"}
        },
        "version": 1,
        "_aiMeta": {
            "job_id": jid,
            "source_sha256": plan["source"]["sha256"],
            "edit_plan_version": plan.get("_plan_version", "v001"),
            "source_media_id": source_media_id,
            "generated_at": now
        }
    }

    return project


# ── Preview Render ─────────────────────────────────────────────────────────────
def render_preview(jid: str, plan: dict, output_path: str) -> bool:
    """FFmpeg: apply cuts, produce low-res preview MP4."""
    source = plan["source"]["file"]
    clips = sorted(plan.get("clips", []), key=lambda x: x.get("timeline_start", 0))
    if not clips:
        return False

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Build FFmpeg filter_complex for clip concatenation
    inputs = []
    filter_parts = []
    seg_labels = []

    for i, clip in enumerate(clips):
        inputs += ["-ss", str(clip["source_start"]), "-to", str(clip["source_end"]), "-i", source]
        filter_parts.append(f"[{i}:v]scale=640:-2,fps=15[v{i}];[{i}:a][v{i}]")
        seg_labels.append(f"[v{i}][{i}:a]")

    n = len(clips)
    concat_filter = "".join(f"[{i}:v]scale=640:-2,fps=15[v{i}];" for i in range(n))
    concat_filter += "".join(f"[v{i}][{i}:a]" for i in range(n))
    concat_filter += f"concat=n={n}:v=1:a=1[outv][outa]"

    cmd = []
    for clip in clips:
        cmd += ["-ss", str(clip["source_start"]), "-to", str(clip["source_end"]), "-i", source]

    cmd += [
        "-filter_complex", concat_filter,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "28",
        "-c:a", "aac", "-b:a", "96k",
        "-y", output_path
    ]

    cmd = ["ffmpeg"] + cmd

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"[director] Preview render failed: {result.stderr[-500:]}")
            return False
        return os.path.exists(output_path)
    except Exception as e:
        print(f"[director] Preview render exception: {e}")
        return False


# ── Three-Output Export ────────────────────────────────────────────────────────
def render_exports(jid: str, plan: dict):
    """Produce AI.mp4, MyVoice.mp4, MultiAudio.mp4 from Edit Plan."""
    source = plan["source"]["file"]
    clips = sorted(plan.get("clips", []), key=lambda x: x.get("timeline_start", 0))
    if not clips:
        return

    out_dir = project_dir(jid) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Build base concat command
    base_inputs = []
    for clip in clips:
        base_inputs += ["-ss", str(clip["source_start"]), "-to", str(clip["source_end"]), "-i", source]
    n = len(clips)

    # Single-audio concat filter (for AI.mp4 and MyVoice.mp4)
    concat_single = "".join(f"[{i}:v]fps=30,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2[v{i}];" for i in range(n))
    concat_single += "".join(f"[v{i}][{i}:a]" for i in range(n))
    concat_single += f"concat=n={n}:v=1:a=1[outv][outa]"

    # Multi-audio concat filter (splits audio into two streams)
    concat_multi = concat_single + ";[outa]asplit[outa1][outa2]"

    single_exports = {
        f"{jid}_AI.mp4":      ["-filter_complex", concat_single, "-map", "[outv]", "-map", "[outa]"],
        f"{jid}_MyVoice.mp4": ["-filter_complex", concat_single, "-map", "[outv]", "-map", "[outa]"],
    }
    for filename, fc_and_maps in single_exports.items():
        out_path = str(out_dir / filename)
        cmd = ["ffmpeg"] + base_inputs + fc_and_maps + [
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-y", out_path
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                print(f"[director] Export done: {filename}")
            else:
                print(f"[director] Export failed: {filename}: {r.stderr[-200:]}")
        except Exception as e:
            print(f"[director] Export exception: {e}")

    # Multi-audio with two audio streams
    multi_path = str(out_dir / f"{jid}_MultiAudio.mp4")
    cmd_multi = ["ffmpeg"] + base_inputs + [
        "-filter_complex", concat_multi,
        "-map", "[outv]", "-map", "[outa1]", "-map", "[outa2]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a:0", "aac", "-b:a:0", "128k",
        "-c:a:1", "aac", "-b:a:1", "128k",
        "-metadata:s:a:0", "title=AI Voice",
        "-metadata:s:a:1", "title=My Voice",
        "-y", multi_path
    ]
    try:
        r = subprocess.run(cmd_multi, capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            print(f"[director] Export done: {jid}_MultiAudio.mp4")
        else:
            print(f"[director] Multi-audio export failed: {r.stderr[-200:]}")
    except Exception as e:
        print(f"[director] Multi-audio export exception: {e}")


# ── Save Edit Plan Version ─────────────────────────────────────────────────────
def save_plan_version(jid: str, plan: dict) -> str:
    """Save edit plan as next version (v001, v002...). Returns version string."""
    ep_dir = edit_plan_dir(jid)
    ep_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(ep_dir.glob("edit-plan-v*.json"))
    next_n = len(existing) + 1
    version = f"v{next_n:03d}"
    plan["_plan_version"] = version

    plan_path = ep_dir / f"edit-plan-{version}.json"
    plan_path.write_text(json.dumps(plan, indent=2))

    # Update current symlink
    current_link = ep_dir / "current.json"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(plan_path.name)

    return version


def load_current_plan(jid: str) -> dict | None:
    current = edit_plan_dir(jid) / "current.json"
    if current.exists():
        return json.loads(current.read_text())
    return None


def get_plan_version(jid: str, version: str) -> dict | None:
    p = edit_plan_dir(jid) / f"edit-plan-{version}.json"
    if p.exists():
        return json.loads(p.read_text())
    return None


def list_plan_versions(jid: str) -> list:
    ep_dir = edit_plan_dir(jid)
    return sorted(p.name for p in ep_dir.glob("edit-plan-v*.json"))


# ── Main Pipeline ──────────────────────────────────────────────────────────────
def run_pipeline(jid: str):
    with _jobs_lock:
        job = dict(_jobs[jid])

    source_path = job["source_path"]
    pdir = project_dir(jid)
    pdir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: SHA256
        update_step(jid, "sha256", "running", "Hashing source file...")
        sha = sha256_file(source_path)
        with _jobs_lock:
            _jobs[jid]["source_sha256"] = sha
            _jobs[jid]["source_hash_verified_at"] = now_iso()
        (pdir / "source_hash.txt").write_text(sha)
        (pdir / "source_path.txt").write_text(source_path)
        update_step(jid, "sha256", "done", sha[:16])

        # Check cache (if we've already processed this source)
        transcript_path = pdir / "transcript.json"
        analysis_path   = pdir / "analysis.json"

        # Step 2: Whisper STT
        update_step(jid, "transcript", "running", "Running Whisper STT...")
        if transcript_path.exists():
            transcript = json.loads(transcript_path.read_text())
            update_step(jid, "transcript", "done", f"Cached — {len(transcript.get('segments', []))} segments")
        else:
            try:
                transcript = run_whisper(source_path)
                transcript_path.write_text(json.dumps(transcript, indent=2))
                update_step(jid, "transcript", "done",
                            f"{len(transcript.get('segments', []))} segments, {transcript.get('duration', 0):.1f}s")
            except Exception as e:
                # Fallback: use empty transcript if Whisper fails
                print(f"[director] Whisper failed, using empty transcript: {e}")
                transcript = {"text": "", "segments": [], "language": "en", "duration": 0}
                # Try to get duration via ffprobe
                try:
                    r = subprocess.run([
                        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", source_path
                    ], capture_output=True, text=True, timeout=30)
                    transcript["duration"] = float(r.stdout.strip())
                except Exception:
                    pass
                transcript_path.write_text(json.dumps(transcript, indent=2))
                update_step(jid, "transcript", "done", "Empty (Whisper unavailable)")

        # Step 3: Video analysis
        update_step(jid, "analysis", "running", "Analyzing video...")
        if analysis_path.exists():
            analysis = json.loads(analysis_path.read_text())
            update_step(jid, "analysis", "done", f"Cached — {len(analysis.get('silences', []))} silences")
        else:
            analysis = analyze_video(source_path, transcript.get("duration", 0))
            analysis_path.write_text(json.dumps(analysis, indent=2))
            update_step(jid, "analysis", "done",
                        f"{len(analysis.get('silences', []))} silences detected")

        # Update job with source duration
        src_dur = analysis.get("duration", transcript.get("duration", 0))
        with _jobs_lock:
            _jobs[jid]["source_duration"] = src_dur

        # Step 4: Generate Edit Plan
        update_step(jid, "edit_plan", "running", "AI generating Edit Plan...")
        with _jobs_lock:
            job_current = dict(_jobs[jid])

        plan = generate_edit_plan(jid, job_current, transcript, analysis)
        version = save_plan_version(jid, plan)

        with _jobs_lock:
            _jobs[jid]["edit_plan_version"] = version
            _jobs[jid]["edit_plan_cuts"] = len(plan.get("cuts", []))
            _jobs[jid]["edit_plan_clips"] = len(plan.get("clips", []))
        update_step(jid, "edit_plan", "done",
                    f"{version}: {len(plan.get('cuts', []))} cuts, {len(plan.get('clips', []))} clips")

        # Step 5: Validate
        update_step(jid, "validate", "running", "Validating Edit Plan...")
        errors = validate_edit_plan(plan, source_path)
        if errors:
            print(f"[director] Validation errors (auto-fixing): {errors}")
            plan = auto_fix_plan(plan)
            errors2 = validate_edit_plan(plan, source_path)
            if errors2:
                update_step(jid, "validate", "done",
                            f"Fixed {len(errors)} errors; {len(errors2)} remaining: {errors2[0]}")
            else:
                # Re-save fixed plan
                save_plan_version(jid, plan)
                update_step(jid, "validate", "done", f"Auto-fixed {len(errors)} issues")
        else:
            update_step(jid, "validate", "done", "All checks passed")

        with _jobs_lock:
            _jobs[jid]["validation_errors"] = errors
            _jobs[jid]["validation_passed"] = len(errors) == 0

        # Step 6: Build OpenCut project
        update_step(jid, "opencut", "running", "Building OpenCut project...")
        oc_project = build_opencut_project(jid, plan)
        oc_path = pdir / "opencut-project.json"
        oc_path.write_text(json.dumps(oc_project, indent=2))
        project_uuid = oc_project["metadata"]["id"]
        with _jobs_lock:
            _jobs[jid]["opencut_project_uuid"] = project_uuid
        update_step(jid, "opencut", "done", f"Project UUID: {project_uuid[:8]}...")

        # Step 7: Preview render
        update_step(jid, "preview", "running", "Rendering preview...")
        preview_path = str(pdir / "preview.mp4")
        ok = render_preview(jid, plan, preview_path)
        if ok:
            update_step(jid, "preview", "done", "preview.mp4 ready")
        else:
            update_step(jid, "preview", "warn", "Preview render failed (continue anyway)")
        with _jobs_lock:
            _jobs[jid]["preview_path"] = preview_path if ok else None

        # Step 8: TTS — punctuate transcript text → Pocket TTS → audio files
        tts_entries = plan.get("tts", [])
        if tts_entries:
            update_step(jid, "tts", "running",
                        f"Generating {len(tts_entries)} segments via Pocket TTS...")
            tts_dir = pdir / "tts"
            tts_dir.mkdir(exist_ok=True)
            ok_tts, failed_tts = [], []
            for tts in tts_entries:
                tts_file = tts_dir / f"{tts['id']}.wav"
                success = generate_tts_audio(tts.get("text", ""), str(tts_file))
                if success:
                    tts["generated_file"] = str(tts_file)
                    ok_tts.append(tts["id"])
                else:
                    failed_tts.append(tts["id"])

            msg = f"{len(ok_tts)} segments via Pocket TTS"
            if failed_tts:
                msg += f" · {len(failed_tts)} failed"
                with _jobs_lock:
                    _jobs[jid]["tts_failed"] = failed_tts
            update_step(jid, "tts", "done" if not failed_tts else "warn", msg)

            # Rebuild OpenCut project with TTS audio tracks
            oc_project = build_opencut_project(jid, plan)
            oc_path.write_text(json.dumps(oc_project, indent=2))
        else:
            update_step(jid, "tts", "done", "No TTS in plan (skipped)")

        # Done — awaiting review
        with _jobs_lock:
            _jobs[jid]["status"] = "awaiting_review"
            _jobs[jid]["completed_at"] = now_iso()
            _jobs[jid]["needs_review_count"] = plan.get("review", {}).get("needs_review_count", 0)
            _jobs[jid]["opencut_url"] = f"{OPENCUT_URL}/ai-bridge.html?job={jid}"

        save_job(jid)

        # Verify source hash unchanged
        sha_after = sha256_file(source_path)
        hash_ok = sha_after == sha
        with _jobs_lock:
            _jobs[jid]["source_hash_final"] = sha_after
            _jobs[jid]["source_hash_intact"] = hash_ok

        # Telegram notification
        clips_total = len(plan.get("clips", []))
        cuts_total = len(plan.get("cuts", []))
        review_count = plan.get("review", {}).get("needs_review_count", 0)
        confidence = plan.get("review", {}).get("ai_confidence", 0)

        src_dur_s = int(src_dur)
        out_dur = sum(c.get("timeline_end", 0) - c.get("timeline_start", 0) for c in plan.get("clips", []))
        out_dur_s = int(out_dur)

        ch_name = ""
        if job.get("channel_id"):
            try:
                channels = json.loads(Path("/opt/studio/channels.json").read_text())
                ch_name = channels.get(job["channel_id"], {}).get("name", "")
            except Exception:
                pass

        bridge_url = f"http://{DIRECTOR_HOST}:9500/ai-bridge.html?job={jid}"
        msg = (
            f"🎬 <b>AI Edit Ready" + (f" — {ch_name}" if ch_name else "") + f" ({int(confidence*100)}%)</b>\n"
            f"⏱ {src_dur_s//60}:{src_dur_s%60:02d} → {out_dur_s//60}:{out_dur_s%60:02d}"
            + (f" (saved {(src_dur_s-out_dur_s)//60}:{(src_dur_s-out_dur_s)%60:02d})" if src_dur_s > out_dur_s else "") + "\n"
            f"✂️ {cuts_total} cuts"
            + (f" · ⚠️ {review_count} need review" if review_count else "") + "\n\n"
            f"🔗 <a href=\"{bridge_url}\">Open in OpenCut</a>\n"
            f"<code>{bridge_url}</code>"
        )
        send_telegram(msg)

    except DeferredError as de:
        from datetime import timedelta
        retry_at = (datetime.now() + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0).isoformat()
        with _jobs_lock:
            _jobs[jid]["status"]       = "deferred"
            _jobs[jid]["retry_at"]     = retry_at
            _jobs[jid]["defer_reason"] = str(de)
        save_job(jid)
        fname = Path(job.get("source_path", "")).name
        send_telegram(
            f"⏰ <b>Job deferred</b> — {fname}\n"
            f"No free LLM available (not marked rush).\n"
            f"Will retry tomorrow at 09:00."
        )

    except Exception as e:
        import traceback
        err = repr(e)
        tb = traceback.format_exc()
        print(f"[director] Pipeline error for {jid}: {tb}")
        set_error(jid, err)
        send_telegram(f"❌ AI Edit FAILED: {jid[:8]}\n{err[:200]}")


# ── Demo page ─────────────────────────────────────────────────────────────────

DEMO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Edit Demo — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh;padding:24px}
h1{font-size:1.6rem;font-weight:700;margin-bottom:6px}
.sub{color:#888;font-size:.9rem;margin-bottom:32px}
.section{background:#161618;border:1px solid #2a2a2e;border-radius:12px;padding:24px;margin-bottom:20px}
.section-title{font-size:.75rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:#666;margin-bottom:16px}
/* Upload */
#drop-zone{border:2px dashed #333;border-radius:10px;padding:48px 24px;text-align:center;cursor:pointer;transition:all .2s}
#drop-zone:hover,#drop-zone.drag{border-color:#7c3aed;background:#1a1040}
#drop-zone .icon{font-size:2.5rem;margin-bottom:12px}
#drop-zone .label{font-size:1rem;color:#aaa}
#drop-zone .hint{font-size:.8rem;color:#555;margin-top:6px}
#file-input{display:none}
#upload-bar{display:none;margin-top:16px}
.bar-bg{background:#222;border-radius:4px;height:6px;overflow:hidden}
.bar-fill{background:#7c3aed;height:100%;width:0;transition:width .2s;border-radius:4px}
.file-info{display:none;background:#1a1040;border:1px solid #3a1e7a;border-radius:8px;padding:12px 16px;margin-top:12px;font-size:.85rem;color:#b39ddb}
/* Instruction */
#section-config{display:none}
textarea#instr{width:100%;background:#111;border:1px solid #333;color:#e0e0e0;border-radius:8px;padding:12px;font-size:.9rem;resize:vertical;min-height:80px;font-family:inherit}
textarea#instr:focus{outline:none;border-color:#7c3aed}
select#chan{width:100%;background:#111;border:1px solid #333;color:#e0e0e0;border-radius:8px;padding:10px 12px;font-size:.9rem;margin-top:12px}
.btn{display:inline-flex;align-items:center;gap:8px;background:#7c3aed;color:#fff;border:none;border-radius:8px;padding:12px 24px;font-size:.95rem;font-weight:600;cursor:pointer;transition:background .2s;margin-top:16px;width:100%;justify-content:center}
.btn:hover{background:#6d28d9}.btn:disabled{background:#444;cursor:default}
.btn-sm{padding:8px 16px;font-size:.8rem;width:auto;margin-top:0}
.btn-green{background:#16a34a}.btn-green:hover{background:#15803d}
.btn-blue{background:#1d4ed8}.btn-blue:hover{background:#1e40af}
.btn-outline{background:transparent;border:1px solid #7c3aed;color:#b39ddb}.btn-outline:hover{background:#1a1040}
/* Progress */
#section-progress{display:none}
.step-row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #1e1e22}
.step-row:last-child{border-bottom:none}
.step-icon{font-size:1.1rem;width:24px;text-align:center;flex-shrink:0}
.step-name{font-weight:600;font-size:.9rem;width:120px;flex-shrink:0}
.step-status{font-size:.8rem;color:#888;flex:1}
.step-status.ok{color:#4ade80}.step-status.err{color:#f87171}.step-status.run{color:#fbbf24}
.job-id{font-size:.75rem;color:#555;margin-bottom:16px;font-family:monospace}
#elapsed{color:#888;font-size:.8rem;margin-top:12px}
/* Results */
#section-results{display:none}
.result-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:600px){.result-grid{grid-template-columns:1fr}}
.stat-card{background:#111;border:1px solid #2a2a2e;border-radius:8px;padding:16px;text-align:center}
.stat-val{font-size:1.6rem;font-weight:700;color:#a78bfa}
.stat-label{font-size:.75rem;color:#666;margin-top:4px}
video#preview{width:100%;border-radius:8px;margin-bottom:16px;background:#000;max-height:400px}
.opencut-btn{display:flex;align-items:center;justify-content:center;gap:10px;background:linear-gradient(135deg,#7c3aed,#2563eb);color:#fff;border:none;border-radius:10px;padding:16px 24px;font-size:1rem;font-weight:700;cursor:pointer;text-decoration:none;width:100%;margin-bottom:16px}
.opencut-btn:hover{opacity:.9}
.exports-row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.exp-btn{flex:1;min-width:120px;background:#1a1040;border:1px solid #3a1e7a;color:#b39ddb;border-radius:8px;padding:10px;text-align:center;cursor:pointer;text-decoration:none;font-size:.8rem;transition:background .2s}
.exp-btn:hover{background:#231060}
.cuts-list{max-height:200px;overflow-y:auto;font-size:.8rem;font-family:monospace;background:#0a0a0c;border-radius:6px;padding:10px}
.cut-row{padding:3px 0;border-bottom:1px solid #1a1a1e;color:#888}
.cut-row span{color:#a78bfa}
/* TTS Tester */
#section-tts{border-color:#1e3a2e}
.tts-row{display:flex;gap:10px;align-items:flex-start}
textarea#tts-text{flex:1;background:#111;border:1px solid #333;color:#e0e0e0;border-radius:8px;padding:10px;font-size:.85rem;resize:vertical;min-height:60px;font-family:inherit}
audio#tts-audio{width:100%;margin-top:12px;display:none}
#tts-status{font-size:.8rem;color:#888;margin-top:8px}
.spin{display:inline-block;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>
<h1>🎬 AI Edit Demo</h1>
<p class="sub">Upload a video — watch the AI edit it — open the result in OpenCut</p>

<!-- Upload -->
<div class="section">
  <div class="section-title">Step 1 — Upload Video</div>
  <div id="drop-zone" onclick="document.getElementById('file-input').click()">
    <div class="icon">📹</div>
    <div class="label">Drop your video here or click to browse</div>
    <div class="hint">MP4 · MOV · MKV · AVI · WebM</div>
  </div>
  <input type="file" id="file-input" accept="video/*">
  <div id="upload-bar"><div class="bar-bg"><div class="bar-fill" id="bar"></div></div></div>
  <div class="file-info" id="file-info"></div>
</div>

<!-- Configure -->
<div class="section" id="section-config">
  <div class="section-title">Step 2 — Editing Instruction</div>
  <textarea id="instr">Remove all silences longer than 1 second. Remove filler words. Keep the most interesting content.</textarea>
  <select id="chan"><option value="">No channel (default)</option></select>
  <label style="display:flex;align-items:center;gap:8px;margin:8px 0;font-size:.85rem;color:#aaa;cursor:pointer">
    <input type="checkbox" id="rush-chk" style="width:16px;height:16px;accent-color:#a855f7">
    <span>Rush — use Groq if free LLMs unavailable (costs quota)</span>
  </label>
  <button class="btn" id="submit-btn" onclick="submitJob()">🚀 Start AI Edit</button>
</div>

<!-- Progress -->
<div class="section" id="section-progress">
  <div class="section-title">Step 3 — Pipeline</div>
  <div class="job-id" id="job-id-label"></div>
  <div id="steps-list">
    <div class="step-row" id="sr-sha256"><span class="step-icon">🔒</span><span class="step-name">Hash</span><span class="step-status" id="ss-sha256">waiting...</span></div>
    <div class="step-row" id="sr-transcript"><span class="step-icon">🎙️</span><span class="step-name">Transcribe</span><span class="step-status" id="ss-transcript">waiting...</span></div>
    <div class="step-row" id="sr-analysis"><span class="step-icon">📊</span><span class="step-name">Silence Analysis</span><span class="step-status" id="ss-analysis">waiting...</span></div>
    <div class="step-row" id="sr-edit_plan"><span class="step-icon">🤖</span><span class="step-name">AI Edit Plan</span><span class="step-status" id="ss-edit_plan">waiting...</span></div>
    <div class="step-row" id="sr-validate"><span class="step-icon">✅</span><span class="step-name">Validate</span><span class="step-status" id="ss-validate">waiting...</span></div>
    <div class="step-row" id="sr-opencut"><span class="step-icon">🎞️</span><span class="step-name">OpenCut Project</span><span class="step-status" id="ss-opencut">waiting...</span></div>
    <div class="step-row" id="sr-preview"><span class="step-icon">▶️</span><span class="step-name">Render Preview</span><span class="step-status" id="ss-preview">waiting...</span></div>
    <div class="step-row" id="sr-tts"><span class="step-icon">🔊</span><span class="step-name">Voice (TTS)</span><span class="step-status" id="ss-tts">waiting...</span></div>
    <div class="step-row" id="sr-export"><span class="step-icon">📦</span><span class="step-name">3 Exports</span><span class="step-status" id="ss-export">waiting...</span></div>
  </div>
  <div id="elapsed"></div>
</div>

<!-- Results -->
<div class="section" id="section-results">
  <div class="section-title">Step 4 — Results</div>
  <div class="result-grid" id="stats-grid"></div>
  <video id="preview" controls></video>
  <a id="opencut-link" class="opencut-btn" href="#" target="_blank">
    ✂️ Open in OpenCut Editor
  </a>
  <div class="exports-row" id="exports-row"></div>
  <div class="section-title" style="margin-top:16px">Cuts Made</div>
  <div class="cuts-list" id="cuts-list"></div>
</div>

<!-- TTS Tester -->
<div class="section" id="section-tts">
  <div class="section-title">TTS — AI Voice Tester</div>
  <p style="font-size:.85rem;color:#888;margin-bottom:12px">Type any text and hear what the AI voice sounds like (XTTS-v2 voice clone)</p>
  <div class="tts-row">
    <textarea id="tts-text">Hello! This is the AI voice that will be used in your edited videos. The voice is cloned from your recordings using XTTS-v2.</textarea>
    <button class="btn btn-green btn-sm" onclick="testTTS()" id="tts-btn">🔊 Generate</button>
  </div>
  <div id="tts-status"></div>
  <audio id="tts-audio" controls></audio>
</div>

<script>
const API = window.location.origin;
let uploadedPath = null, jobId = null, pollTimer = null, startTime = null;

// ── Drag & Drop ──────────────────────────────────────────────────────────────
const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
dz.addEventListener('drop', e => { e.preventDefault(); dz.classList.remove('drag'); handleFile(e.dataTransfer.files[0]); });
document.getElementById('file-input').addEventListener('change', e => handleFile(e.target.files[0]));

function handleFile(file) {
  if (!file) return;
  const bar = document.getElementById('bar');
  const upBar = document.getElementById('upload-bar');
  const info = document.getElementById('file-info');
  upBar.style.display = 'block';
  info.style.display = 'none';
  const fd = new FormData();
  fd.append('file', file);
  const xhr = new XMLHttpRequest();
  xhr.upload.onprogress = e => { if (e.lengthComputable) bar.style.width = (e.loaded/e.total*100)+'%'; };
  xhr.onload = () => {
    const r = JSON.parse(xhr.responseText);
    if (r.path) {
      uploadedPath = r.path;
      bar.style.width = '100%';
      info.textContent = `✓ ${file.name} (${(file.size/1024/1024).toFixed(1)} MB) uploaded`;
      info.style.display = 'block';
      document.getElementById('section-config').style.display = 'block';
      document.getElementById('section-config').scrollIntoView({behavior:'smooth'});
    } else {
      info.textContent = '❌ Upload failed: ' + (r.error||'unknown');
      info.style.display = 'block';
    }
  };
  xhr.onerror = () => { info.textContent='❌ Network error during upload'; info.style.display='block'; };
  xhr.open('POST', API+'/upload');
  xhr.send(fd);
  dz.querySelector('.label').textContent = `Uploading ${file.name}...`;
}

// ── Load channels ────────────────────────────────────────────────────────────
fetch(API+'/api/channels').then(r=>r.json()).then(chs=>{
  const sel = document.getElementById('chan');
  (chs||[]).forEach(ch => {
    const o = document.createElement('option');
    o.value = ch.id; o.textContent = ch.name;
    sel.appendChild(o);
  });
}).catch(()=>{});

// ── Submit ───────────────────────────────────────────────────────────────────
async function submitJob() {
  if (!uploadedPath) return;
  document.getElementById('submit-btn').disabled = true;
  document.getElementById('submit-btn').textContent = '⏳ Submitting...';
  const instr = document.getElementById('instr').value.trim();
  const chanId = document.getElementById('chan').value;
  const isRush = document.getElementById('rush-chk').checked;
  const body = { video_path: uploadedPath, instruction: instr, project_name: 'demo', priority: isRush ? 'rush' : 'normal' };
  if (chanId) body.channel_id = chanId;
  const r = await fetch(API+'/api/process', { method:'POST', headers:{'Content-Type':'application/json','User-Agent':'MiniStudio/1.0'}, body:JSON.stringify(body) });
  const j = await r.json();
  if (j.job_id) {
    jobId = j.job_id;
    startTime = Date.now();
    document.getElementById('job-id-label').textContent = 'Job: '+jobId;
    document.getElementById('section-progress').style.display = 'block';
    document.getElementById('section-progress').scrollIntoView({behavior:'smooth'});
    pollTimer = setInterval(pollJob, 3000);
    pollJob();
  } else {
    alert('Error: '+(j.error||JSON.stringify(j)));
    document.getElementById('submit-btn').disabled = false;
    document.getElementById('submit-btn').textContent = '🚀 Start AI Edit';
  }
}

// ── Poll ─────────────────────────────────────────────────────────────────────
const STEPS = ['sha256','transcript','analysis','edit_plan','validate','opencut','preview','tts','export'];
async function pollJob() {
  if (!jobId) return;
  const r = await fetch(API+'/api/jobs/'+jobId);
  const j = await r.json();
  const steps = j.steps||{};
  STEPS.forEach(s => {
    const el = document.getElementById('ss-'+s);
    if (!el) return;
    const st = steps[s];
    if (!st) return;
    el.className = 'step-status';
    if (st.status==='done') { el.className+=' ok'; el.textContent = '✓ '+st.msg; }
    else if (st.status==='error') { el.className+=' err'; el.textContent = '✗ '+st.msg; }
    else if (st.status==='running') { el.className+=' run'; el.textContent = '⟳ '+st.msg; }
  });
  const secs = Math.floor((Date.now()-startTime)/1000);
  document.getElementById('elapsed').textContent = `Elapsed: ${Math.floor(secs/60)}m ${secs%60}s`;
  if (j.status==='awaiting_review') {
    clearInterval(pollTimer);
    showResults(j);
  } else if (j.status==='deferred') {
    clearInterval(pollTimer);
    document.getElementById('elapsed').textContent = `⏰ Deferred — no free LLM available. Retry at: ${j.retry_at||'09:00 tomorrow'}`;
  } else if (j.status==='error') {
    clearInterval(pollTimer);
  }
}

// ── Results ──────────────────────────────────────────────────────────────────
async function showResults(job) {
  const srcS = Math.round(job.source_duration||0);
  let plan = {};
  try { plan = await (await fetch(API+'/api/jobs/'+jobId+'/edit-plan')).json(); } catch(e){}
  const clips = plan.clips||[];
  const cuts = plan.cuts||[];
  const outS = Math.round(clips.reduce((a,c)=>(c.timeline_end||0)-c.timeline_start+a, 0)||0);
  const savedS = srcS - outS;
  const conf = Math.round((plan.review?.ai_confidence||0)*100);

  document.getElementById('stats-grid').innerHTML = `
    <div class="stat-card"><div class="stat-val">${fmt(srcS)} → ${fmt(outS)}</div><div class="stat-label">Duration (saved ${fmt(savedS)})</div></div>
    <div class="stat-card"><div class="stat-val">${cuts.length}</div><div class="stat-label">Cuts Made</div></div>
    <div class="stat-card"><div class="stat-val">${conf}%</div><div class="stat-label">AI Confidence</div></div>
    <div class="stat-card"><div class="stat-val">${clips.length}</div><div class="stat-label">Clips Kept</div></div>`;

  const vid = document.getElementById('preview');
  vid.src = API+'/media/'+jobId+'/preview.mp4';

  const bridge = API.replace(':9533','').replace('9533','9500')+':9500/ai-bridge.html?job='+jobId;
  document.getElementById('opencut-link').href = bridge;

  const exRow = document.getElementById('exports-row');
  exRow.innerHTML = '';
  [['AI Voice','_AI.mp4','🤖'],['My Voice','_MyVoice.mp4','🎤'],['Multi-Audio','_MultiAudio.mp4','🎵']].forEach(([label,suffix,icon])=>{
    const a = document.createElement('a');
    a.className='exp-btn'; a.href=API+'/media/'+jobId+'/exports/'+jobId+suffix; a.download=jobId+suffix;
    a.innerHTML=`${icon}<br><strong>${label}</strong><br><small>Download</small>`;
    exRow.appendChild(a);
  });

  const cutsList = document.getElementById('cuts-list');
  cutsList.innerHTML = cuts.length ? cuts.map(c=>{
    const dur = c.source_end&&c.source_start ? (c.source_end-c.source_start).toFixed(1) : '?';
    const ss = c.source_start!=null ? fmt(Math.round(c.source_start)) : '?:??';
    const se = c.source_end!=null ? fmt(Math.round(c.source_end)) : '?:??';
    return `<div class="cut-row"><span>${c.id}</span>  ${ss} → ${se}  (${dur}s)  ${c.reason||''}</div>`;
  }).join('') : '<div style="color:#555">No cuts in plan</div>';

  document.getElementById('section-results').style.display = 'block';
  document.getElementById('section-results').scrollIntoView({behavior:'smooth'});
}

function fmt(s){ return Math.floor(s/60)+':'+(s%60).toString().padStart(2,'0'); }

// ── TTS Tester ───────────────────────────────────────────────────────────────
async function testTTS() {
  const text = document.getElementById('tts-text').value.trim();
  if (!text) return;
  const btn = document.getElementById('tts-btn');
  const status = document.getElementById('tts-status');
  const audio = document.getElementById('tts-audio');
  btn.disabled = true; btn.textContent = '⏳';
  status.innerHTML = '<span class="spin">⟳</span> Generating voice with XTTS-v2...';
  try {
    const r = await fetch(API+'/tts-preview', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text}) });
    if (!r.ok) throw new Error(await r.text());
    const blob = await r.blob();
    audio.src = URL.createObjectURL(blob);
    audio.style.display = 'block';
    audio.play();
    status.textContent = '✓ Voice generated — press play above';
  } catch(e) {
    status.textContent = '❌ '+e.message;
  }
  btn.disabled = false; btn.textContent = '🔊 Generate';
}
</script>
</body>
</html>"""


# ── Flask Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return dashboard_html()

@app.route("/demo")
def demo_page():
    return Response(DEMO_HTML, mimetype="text/html")

@app.route("/api/status")
def api_status():
    with _jobs_lock:
        jobs = list(_jobs.values())
    return jsonify({
        "status": "ok",
        "jobs_total": len(jobs),
        "jobs_running": sum(1 for j in jobs if j.get("status") == "running"),
        "jobs_ready": sum(1 for j in jobs if j.get("status") == "awaiting_review"),
        "version": "1.0"
    })

@app.route("/api/jobs")
def api_jobs():
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify(jobs)

@app.route("/api/jobs/<jid>")
def api_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@app.route("/api/jobs/<jid>/edit-plan")
def api_edit_plan(jid):
    plan = load_current_plan(jid)
    if not plan:
        return jsonify({"error": "no edit plan"}), 404
    return jsonify(plan)

@app.route("/api/jobs/<jid>/opencut-project")
def api_opencut_project(jid):
    p = project_dir(jid) / "opencut-project.json"
    if not p.exists():
        return jsonify({"error": "no opencut project"}), 404
    data = json.loads(p.read_text())
    resp = jsonify(data)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/api/jobs/<jid>/preview")
def api_preview(jid):
    with _jobs_lock:
        job = _jobs.get(jid, {})
    preview = job.get("preview_path")
    if not preview or not os.path.exists(preview):
        return jsonify({"error": "preview not ready"}), 404
    return send_file(preview, mimetype="video/mp4")

@app.route("/api/jobs/<jid>/versions")
def api_versions(jid):
    return jsonify(list_plan_versions(jid))

@app.route("/media/<jid>/<path:filename>")
def serve_media(jid, filename):
    """Serve project files with CORS for bridge page."""
    p = project_dir(jid) / filename
    if not p.exists():
        # Try source video
        with _jobs_lock:
            job = _jobs.get(jid, {})
        src = job.get("source_path", "")
        if filename in ("source.mp4", "source.mkv", "source.mov") and os.path.exists(src):
            resp = send_file(src)
            resp.headers["Access-Control-Allow-Origin"] = "*"
            return resp
        return jsonify({"error": "not found"}), 404
    resp = send_file(str(p))
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

@app.route("/api/process", methods=["POST"])
def api_process():
    data = request.get_json(force=True)
    video_path = data.get("video_path", "")
    if not video_path or not os.path.exists(video_path):
        return jsonify({"error": f"video_path not found: {video_path}"}), 400

    jid = gen_id()
    instruction = data.get("instruction", "Edit this video. Remove silences. Keep best content.")

    # Detect priority from explicit param or instruction keywords
    priority = data.get("priority", "")
    if not priority:
        inst_lower = instruction.lower()
        if any(w in inst_lower for w in ("rush", "urgent", "today", "asap", "need it now")):
            priority = "rush"
        elif any(w in inst_lower for w in ("no rush", "tomorrow", "later", "whenever")):
            priority = "defer"
        else:
            priority = "normal"

    job = {
        "job_id": jid,
        "status": "running",
        "created_at": now_iso(),
        "source_path": video_path,
        "source_name": Path(video_path).name,
        "channel_id": data.get("channel_id", ""),
        "instruction": instruction,
        "priority": priority,
        "project_name": data.get("project_name", Path(video_path).stem),
        "steps": {}
    }

    with _jobs_lock:
        _jobs[jid] = job

    t = threading.Thread(target=run_pipeline, args=(jid,), daemon=True)
    t.start()

    return jsonify({"job_id": jid, "status": "running"})

@app.route("/api/jobs/<jid>/approve", methods=["POST"])
def api_approve(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404

    plan = load_current_plan(jid)
    if not plan:
        return jsonify({"error": "no edit plan"}), 400

    # Check TTS is complete (no pending)
    tts_failed = job.get("tts_failed", [])
    if tts_failed:
        return jsonify({"error": f"TTS failed for: {tts_failed}. Retry before approving."}), 400

    with _jobs_lock:
        _jobs[jid]["status"] = "exporting"
    save_job(jid)

    def do_export():
        try:
            render_exports(jid, plan)
            with _jobs_lock:
                _jobs[jid]["status"] = "exported"
                _jobs[jid]["exported_at"] = now_iso()
            save_job(jid)
            send_telegram(f"✅ Export complete: job {jid[:8]}\n3 files ready.")
        except Exception as e:
            with _jobs_lock:
                _jobs[jid]["status"] = "export_error"
                _jobs[jid]["export_error"] = str(e)
            save_job(jid)

    # Record approval in style profile (learn from this decision)
    with _jobs_lock:
        instr = _jobs.get(jid, {}).get("instruction", "")
    threading.Thread(target=record_edit_decision,
                     args=(jid, "approved", plan, instr), daemon=True).start()

    threading.Thread(target=do_export, daemon=True).start()
    return jsonify({"status": "exporting"})

@app.route("/api/jobs/<jid>/reedit", methods=["POST"])
def api_reedit(jid):
    """Re-edit: patch the plan using LLM, DO NOT re-transcribe or re-analyze."""
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True)
    instruction = data.get("instruction", "")
    if not instruction:
        return jsonify({"error": "instruction required"}), 400

    current_plan = load_current_plan(jid)
    if not current_plan:
        return jsonify({"error": "no current plan to re-edit"}), 400

    # Read cached transcript and analysis (DO NOT re-run them)
    pdir = project_dir(jid)
    transcript_path = pdir / "transcript.json"
    analysis_path   = pdir / "analysis.json"

    if not transcript_path.exists():
        return jsonify({"error": "transcript not cached — run full process first"}), 400

    transcript = json.loads(transcript_path.read_text())
    analysis   = json.loads(analysis_path.read_text()) if analysis_path.exists() else {"silences": [], "duration": 0}

    prev_version = current_plan.get("_plan_version", "v001")

    def do_reedit():
        with _jobs_lock:
            _jobs[jid]["status"] = "running"
            _jobs[jid]["reedit_instruction"] = instruction
        update_step(jid, "reedit", "running", f"Re-editing: {instruction[:60]}")

        try:
            # Snapshot current plan is already saved — just generate new one
            with _jobs_lock:
                job_snapshot = dict(_jobs[jid])
            job_snapshot["instruction"] = instruction

            new_plan = generate_edit_plan(jid, job_snapshot, transcript, analysis)
            version = save_plan_version(jid, new_plan)

            with _jobs_lock:
                _jobs[jid]["edit_plan_version"] = version
            update_step(jid, "reedit", "done", f"New version: {version} (was {prev_version})")

            # Learn from the re-edit instruction
            record_edit_decision(jid, "reedited", instruction=instruction)

            # Rebuild OpenCut project
            oc_project = build_opencut_project(jid, new_plan)
            (pdir / "opencut-project.json").write_text(json.dumps(oc_project, indent=2))

            # Render new preview
            preview_path = str(pdir / f"preview_{version}.mp4")
            ok = render_preview(jid, new_plan, preview_path)
            if ok:
                with _jobs_lock:
                    _jobs[jid]["preview_path"] = preview_path

            with _jobs_lock:
                _jobs[jid]["status"] = "awaiting_review"
            save_job(jid)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[director] Reedit error for {jid}: {tb}")
            update_step(jid, "reedit", "error", repr(e))
            with _jobs_lock:
                _jobs[jid]["status"] = "error"
            save_job(jid)

    threading.Thread(target=do_reedit, daemon=True).start()
    return jsonify({"status": "running", "prev_version": prev_version})

@app.route("/api/jobs/<jid>/revert", methods=["POST"])
def api_revert(jid):
    """Revert to previous plan version. Does NOT delete the newer version."""
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404

    data = request.get_json(force=True) or {}
    ep_dir = edit_plan_dir(jid)
    versions = sorted(ep_dir.glob("edit-plan-v*.json"))
    if len(versions) < 2:
        return jsonify({"error": "no previous version to revert to"}), 400

    target_version = data.get("version")
    if target_version:
        target_path = ep_dir / f"edit-plan-{target_version}.json"
    else:
        # Get current version, revert to one before
        current_link = ep_dir / "current.json"
        current_target = current_link.resolve().name if current_link.exists() else versions[-1].name
        current_idx = next((i for i, v in enumerate(versions) if v.name == current_target), len(versions) - 1)
        prev_idx = max(0, current_idx - 1)
        target_path = versions[prev_idx]
        target_version = target_path.stem.replace("edit-plan-", "")

    if not target_path.exists():
        return jsonify({"error": f"version {target_version} not found"}), 404

    # Update current symlink
    current_link = ep_dir / "current.json"
    if current_link.exists() or current_link.is_symlink():
        current_link.unlink()
    current_link.symlink_to(target_path.name)

    reverted_plan = json.loads(target_path.read_text())
    reverted_plan = auto_fix_plan(reverted_plan)

    # Rebuild OpenCut project from reverted plan
    pdir = project_dir(jid)
    oc_project = build_opencut_project(jid, reverted_plan)
    (pdir / "opencut-project.json").write_text(json.dumps(oc_project, indent=2))

    # Render preview for reverted plan
    preview_path = str(pdir / f"preview_{target_version}.mp4")
    ok = render_preview(jid, reverted_plan, preview_path)
    if ok:
        with _jobs_lock:
            _jobs[jid]["preview_path"] = preview_path

    with _jobs_lock:
        _jobs[jid]["edit_plan_version"] = target_version
        _jobs[jid]["status"] = "awaiting_review"
    save_job(jid)

    return jsonify({"reverted_to": target_version, "versions_preserved": [v.name for v in versions]})

@app.route("/inject/<jid>")
def inject_redirect(jid):
    """Redirect to OpenCut bridge page."""
    return redirect(f"{OPENCUT_URL}/ai-bridge.html?job={jid}")

@app.route("/upload", methods=["POST"])
def upload_video():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "empty filename"}), 400
    upload_dir = Path("/opt/studio/media/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe = f"{gen_id()}_{Path(f.filename).name}"
    dest = upload_dir / safe
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": f.filename, "size": dest.stat().st_size})

@app.route("/api/channels")
def api_channels():
    try:
        channels = json.loads(Path("/opt/studio/channels.json").read_text())
        return jsonify([{"id": cid, "name": ch.get("name", cid)} for cid, ch in channels.items()])
    except Exception:
        return jsonify([])

@app.route("/tts-preview", methods=["POST"])
def tts_preview():
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "Hello, this is the AI voice.").strip()[:500]
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        ok = generate_tts_audio(text, tmp)
        if ok and Path(tmp).exists():
            audio = Path(tmp).read_bytes()
            Path(tmp).unlink(missing_ok=True)
            return Response(audio, mimetype="audio/wav")
        raise ValueError("TTS returned no audio")
    except Exception as e:
        Path(tmp).unlink(missing_ok=True)
        return jsonify({"error": str(e)}), 500

# ── Dashboard HTML ─────────────────────────────────────────────────────────────
def dashboard_html() -> str:
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda x: x.get("created_at", ""), reverse=True)

    def status_badge(s):
        colors = {
            "running": "orange", "awaiting_review": "green",
            "exported": "blue", "error": "red", "exporting": "purple"
        }
        c = colors.get(s, "gray")
        return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px">{s}</span>'

    def step_dot(step_data):
        if not step_data:
            return '<span style="color:#888">○</span>'
        s = step_data.get("status", "")
        if s == "done": return '<span style="color:green">●</span>'
        if s == "running": return '<span style="color:orange">◉</span>'
        if s == "error": return '<span style="color:red">✗</span>'
        if s == "warn": return '<span style="color:yellow">⚠</span>'
        return '<span style="color:#888">○</span>'

    rows = ""
    for job in jobs:
        jid = job["job_id"]
        steps = job.get("steps", {})
        step_parts = []
        for k, v in steps.items():
            tip = str(k) + ": " + str(v.get("msg", ""))
            step_parts.append(f'<abbr title="{tip}">{step_dot(v)}</abbr>')
        step_icons = " ".join(step_parts)
        rows += f"""
        <tr>
          <td><code>{jid[:8]}</code></td>
          <td>{job.get('source_name', '')[:30]}</td>
          <td>{status_badge(job.get('status', '?'))}</td>
          <td style="font-size:11px">{step_icons}</td>
          <td>
            <a href="/api/jobs/{jid}" target="_blank">JSON</a> |
            <a href="/api/jobs/{jid}/edit-plan" target="_blank">Plan</a> |
            {'<a href="/api/jobs/'+jid+'/preview" target="_blank">Preview</a> | ' if job.get('preview_path') else ''}
            <a href="{OPENCUT_URL}/ai-bridge.html?job={jid}" target="_blank">Open in OpenCut</a>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Edit Director — Jobs</title>
<style>
  body {{ font-family: monospace; background: #111; color: #eee; margin: 0; padding: 20px; }}
  h1 {{ color: #7af; margin: 0 0 8px 0; }}
  .sub {{ color: #888; font-size: 12px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #222; padding: 8px; text-align: left; color: #7af; }}
  td {{ padding: 8px; border-bottom: 1px solid #222; vertical-align: middle; }}
  tr:hover {{ background: #1a1a1a; }}
  .new-job {{ background: #1a2a1a; padding: 16px; border-radius: 6px; margin-bottom: 20px; }}
  .new-job input, .new-job textarea, .new-job select {{
    background: #222; color: #eee; border: 1px solid #444; padding: 6px; margin: 4px 0;
    border-radius: 4px; width: 100%; box-sizing: border-box;
  }}
  .btn {{ background: #4a7; color: #fff; border: none; padding: 8px 16px; border-radius: 4px;
          cursor: pointer; font-size: 14px; }}
  .btn:hover {{ background: #5b8; }}
  #msg {{ margin-top: 8px; color: #7af; }}
</style>
</head>
<body>
<h1>🎬 AI Edit Director</h1>
<div class="sub">Pipeline: SHA256 → STT → Analysis → Edit Plan → Validate → OpenCut → Preview | Port {DIRECTOR_PORT} &nbsp;·&nbsp; <a href="/demo" style="color:#a78bfa;text-decoration:none">🚀 Upload Demo</a> &nbsp;·&nbsp; <a href="/vlog" style="color:#4ade80;text-decoration:none">🎥 Vlog Builder</a> &nbsp;·&nbsp; <a href="/shorts" style="color:#a78bfa;text-decoration:none">✂️ Shorts Builder</a> &nbsp;·&nbsp; <a href="/my-style" style="color:#a78bfa;text-decoration:none">🎨 My Style</a></div>

<div class="new-job">
  <b>Start New Job</b><br>
  <input id="vpath" placeholder="Video path (e.g. /opt/studio/projects/test_source.mp4)" value="/opt/studio/projects/test_source.mp4">
  <textarea id="instr" rows="2" placeholder="Instruction">Edit this video. Remove silences longer than 1 second. Keep the most interesting content. Make it engaging.</textarea>
  <input id="pname" placeholder="Project name (optional)">
  <button class="btn" onclick="startJob()">▶ Start Pipeline</button>
  <div id="msg"></div>
</div>

<table>
  <thead><tr><th>Job ID</th><th>Source</th><th>Status</th><th>Steps</th><th>Actions</th></tr></thead>
  <tbody id="jobs">{rows}</tbody>
</table>

<script>
async function startJob() {{
  const vpath = document.getElementById('vpath').value.trim();
  const instr = document.getElementById('instr').value.trim();
  const pname = document.getElementById('pname').value.trim();
  if (!vpath) return alert('Video path required');
  document.getElementById('msg').textContent = 'Starting...';
  const r = await fetch('/api/process', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{video_path: vpath, instruction: instr, project_name: pname}})
  }});
  const d = await r.json();
  if (d.job_id) {{
    document.getElementById('msg').textContent = '✓ Job started: ' + d.job_id;
    setTimeout(() => location.reload(), 2000);
  }} else {{
    document.getElementById('msg').textContent = 'Error: ' + JSON.stringify(d);
  }}
}}
setTimeout(() => location.reload(), 15000);
</script>
</body>
</html>"""

# ── Reject endpoint ──────────────────────────────────────────────────────────
@app.route("/api/jobs/<jid>/reject", methods=["POST"])
def api_reject(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True) or {}
    reason = data.get("reason", "")
    with _jobs_lock:
        _jobs[jid]["status"] = "rejected"
        _jobs[jid]["reject_reason"] = reason
        _jobs[jid]["rejected_at"] = now_iso()
    save_job(jid)
    record_edit_decision(jid, "rejected", instruction=reason)
    return jsonify({"status": "rejected"})


# ── Style Profile API + UI ────────────────────────────────────────────────────
@app.route("/api/style", methods=["GET"])
def api_style_get():
    return jsonify(load_style_profile())

@app.route("/api/style/rules", methods=["POST"])
def api_style_rules():
    """Add or remove a manual rule."""
    data = request.get_json(force=True) or {}
    action = data.get("action", "add")  # "add" or "remove"
    rule   = (data.get("rule") or "").strip()
    if not rule:
        return jsonify({"error": "rule required"}), 400
    profile = load_style_profile()
    rules = profile.setdefault("manual_rules", [])
    if action == "add" and rule not in rules:
        rules.insert(0, rule)
    elif action == "remove" and rule in rules:
        rules.remove(rule)
    profile["manual_rules"] = rules[:20]
    profile["style_summary"] = _rebuild_style_summary(profile)
    save_style_profile(profile)
    return jsonify({"rules": profile["manual_rules"]})

@app.route("/api/style/reset", methods=["POST"])
def api_style_reset():
    save_style_profile(dict(_PROFILE_DEFAULTS))
    return jsonify({"status": "reset"})

@app.route("/my-style")
def my_style_page():
    return Response(MY_STYLE_HTML, mimetype="text/html")

MY_STYLE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>My Editing Style — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;padding:24px;max-width:900px;margin:0 auto}
h1{font-size:1.5rem;font-weight:700;margin-bottom:4px}
.sub{color:#888;font-size:.85rem;margin-bottom:28px}
h2{font-size:1rem;font-weight:600;color:#a78bfa;margin:24px 0 10px}
.card{background:#1a1a1f;border:1px solid #2a2a35;border-radius:10px;padding:18px;margin-bottom:16px}
.stat-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px;margin-bottom:0}
.stat{background:#111118;border-radius:8px;padding:14px;text-align:center}
.stat-val{font-size:1.6rem;font-weight:700;color:#a78bfa}
.stat-label{font-size:.75rem;color:#888;margin-top:4px}
.rule-list{list-style:none;padding:0}
.rule-list li{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #222}
.rule-list li:last-child{border-bottom:none}
.rule-text{flex:1;font-size:.9rem}
.del-btn{background:#3a1020;border:none;color:#f87171;border-radius:5px;padding:3px 10px;cursor:pointer;font-size:.8rem}
.del-btn:hover{background:#5a1a30}
.add-row{display:flex;gap:8px;margin-top:12px}
.add-row input{flex:1;background:#111118;border:1px solid #333;border-radius:6px;padding:8px 12px;color:#e0e0e0;font-size:.9rem}
.btn{background:#6d28d9;color:#fff;border:none;border-radius:6px;padding:8px 16px;cursor:pointer;font-size:.9rem;font-weight:600}
.btn:hover{background:#7c3aed}
.btn-sm{padding:5px 12px;font-size:.8rem}
.hist-table{width:100%;border-collapse:collapse;font-size:.82rem}
.hist-table th{text-align:left;padding:6px 8px;color:#888;border-bottom:1px solid #2a2a35;font-weight:500}
.hist-table td{padding:6px 8px;border-bottom:1px solid #1a1a25;vertical-align:top}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600}
.badge-approved{background:#14532d;color:#86efac}
.badge-rejected{background:#450a0a;color:#fca5a5}
.badge-reedited{background:#1e1b4b;color:#a5b4fc}
.summary-box{background:#111118;border-left:3px solid #6d28d9;padding:14px;border-radius:0 8px 8px 0;font-size:.9rem;line-height:1.6;color:#ccc}
.empty{color:#555;font-size:.85rem;font-style:italic;padding:12px 0}
a{color:#a78bfa;text-decoration:none}
a:hover{text-decoration:underline}
</style>
</head>
<body>
<h1>My Editing Style</h1>
<p class="sub">The AI learns how you edit. Every approval, rejection, and re-edit teaches it your preferences. Syncs between both machines.</p>

<!-- Stats -->
<h2>Stats</h2>
<div class="card">
  <div class="stat-row" id="stats-row">
    <div class="stat"><div class="stat-val" id="s-total">…</div><div class="stat-label">Jobs Total</div></div>
    <div class="stat"><div class="stat-val" id="s-approved">…</div><div class="stat-label">Approved</div></div>
    <div class="stat"><div class="stat-val" id="s-rejected">…</div><div class="stat-label">Rejected</div></div>
    <div class="stat"><div class="stat-val" id="s-reedited">…</div><div class="stat-label">Re-edited</div></div>
    <div class="stat"><div class="stat-val" id="s-cuts">…</div><div class="stat-label">Avg Cuts/Job</div></div>
    <div class="stat"><div class="stat-val" id="s-kept">…</div><div class="stat-label">Avg % Kept</div></div>
  </div>
</div>

<!-- What AI has learned -->
<h2>What the AI Has Learned About You</h2>
<div class="card">
  <div class="summary-box" id="style-summary"><span class="empty">No edits yet — the AI will learn your style as you approve and reject jobs.</span></div>
</div>

<!-- Your Rules -->
<h2>Your Rules <span style="color:#555;font-weight:400;font-size:.8rem">(always applied, even before any history builds up)</span></h2>
<div class="card">
  <ul class="rule-list" id="rules-list"></ul>
  <div class="add-row">
    <input id="new-rule" placeholder='e.g. "Never cut mid-sentence" or "Always keep reactions"' onkeydown="if(event.key==='Enter')addRule()">
    <button class="btn btn-sm" onclick="addRule()">+ Add Rule</button>
  </div>
</div>

<!-- Common re-edit instructions -->
<h2>Your Common Re-edit Instructions</h2>
<div class="card" id="reeedits-card">
  <div class="empty">Re-edit a job to start building this list.</div>
</div>

<!-- Edit history -->
<h2>Edit History</h2>
<div class="card">
  <table class="hist-table">
    <thead><tr><th>Date</th><th>Job</th><th>Action</th><th>Instruction</th><th>Cuts</th><th>Kept</th></tr></thead>
    <tbody id="hist-body"></tbody>
  </table>
</div>

<p style="margin-top:20px;font-size:.8rem;color:#444">
  <a href="/">← Dashboard</a> &nbsp;·&nbsp;
  <a href="/demo">Demo Upload</a> &nbsp;·&nbsp;
  <button onclick="if(confirm('Reset all learned style data?')) resetStyle()" style="background:none;border:none;color:#555;cursor:pointer;font-size:.8rem">Reset style data</button>
</p>

<script>
let profile = {};

async function load() {
  const r = await fetch('/api/style');
  profile = await r.json();
  render();
}

function render() {
  document.getElementById('s-total').textContent    = profile.jobs_total || 0;
  document.getElementById('s-approved').textContent = profile.jobs_approved || 0;
  document.getElementById('s-rejected').textContent = profile.jobs_rejected || 0;
  document.getElementById('s-reedited').textContent = profile.jobs_reedited || 0;
  document.getElementById('s-cuts').textContent     = (profile.avg_cuts_per_job||0).toFixed(1);
  document.getElementById('s-kept').textContent     = Math.round((profile.avg_kept_ratio||1)*100)+'%';

  const sum = profile.style_summary || '';
  document.getElementById('style-summary').innerHTML = sum
    ? sum : '<span class="empty">No edits yet — approve some jobs to build your style profile.</span>';

  const rules = profile.manual_rules || [];
  const ul = document.getElementById('rules-list');
  if (rules.length === 0) {
    ul.innerHTML = '<li><span class="empty">No rules yet — add one above.</span></li>';
  } else {
    ul.innerHTML = rules.map(r => `
      <li>
        <span class="rule-text">${esc(r)}</span>
        <button class="del-btn" onclick="removeRule(${JSON.stringify(r)})">✕ Remove</button>
      </li>`).join('');
  }

  const ri = profile.reedit_instructions || [];
  const rc = document.getElementById('reeedits-card');
  rc.innerHTML = ri.length
    ? ri.map(i=>`<div style="padding:5px 0;border-bottom:1px solid #222;font-size:.88rem;color:#bbb">"${esc(i)}"</div>`).join('')
    : '<div class="empty">Re-edit a job to start building this list.</div>';

  const hist = profile.edit_history || [];
  const tb = document.getElementById('hist-body');
  if (hist.length === 0) {
    tb.innerHTML = '<tr><td colspan="6" class="empty" style="padding:12px">No history yet.</td></tr>';
  } else {
    tb.innerHTML = hist.map(h => {
      const badge = `<span class="badge badge-${h.action}">${h.action}</span>`;
      const kept = h.kept_ratio != null ? Math.round(h.kept_ratio*100)+'%' : '—';
      const cuts = h.cuts != null ? h.cuts : '—';
      return `<tr>
        <td>${h.date||'—'}</td>
        <td><code>${(h.job_id||'').slice(0,8)}</code></td>
        <td>${badge}</td>
        <td style="color:#aaa;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.instruction||'—')}</td>
        <td>${cuts}</td>
        <td>${kept}</td>
      </tr>`;
    }).join('');
  }
}

async function addRule() {
  const inp = document.getElementById('new-rule');
  const rule = inp.value.trim();
  if (!rule) return;
  await fetch('/api/style/rules', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'add', rule})
  });
  inp.value = '';
  load();
}

async function removeRule(rule) {
  await fetch('/api/style/rules', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action:'remove', rule})
  });
  load();
}

async function resetStyle() {
  await fetch('/api/style/reset', {method:'POST'});
  load();
}

function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


# ── Shorts Builder (Magisto-style auto-highlight reel) ───────────────────────
# Scores every segment by excitement, LLM picks best moments,
# assembles to music, outputs 9:16 reel + 1:1 square.

_shorts_jobs: dict = {}
_shorts_lock = threading.Lock()

SHORTS_STYLES = {
    "energetic": {
        "label": "Energetic",
        "desc": "Fast cuts, high energy, best for gaming/action highlights",
        "min_clip": 1.5, "max_clip": 4.0, "transition": "cut",
        "icon": "⚡"
    },
    "cinematic": {
        "label": "Cinematic",
        "desc": "Slower, flowing cuts — travel, lifestyle, vlogs",
        "min_clip": 3.0, "max_clip": 8.0, "transition": "fade",
        "icon": "🎬"
    },
    "fun": {
        "label": "Fun",
        "desc": "Mixed pacing, keeps reactions and funny moments",
        "min_clip": 2.0, "max_clip": 5.0, "transition": "cut",
        "icon": "😄"
    },
    "story": {
        "label": "Story",
        "desc": "Narrative flow — keeps speech, builds to a point",
        "min_clip": 4.0, "max_clip": 10.0, "transition": "fade",
        "icon": "📖"
    }
}

def _score_segments(video_path: str, seg_secs: float = 2.0) -> list:
    """Score each N-second segment by audio energy + scene change count."""
    import subprocess as sp

    # Get total duration
    dur_r = sp.run(
        ["ffprobe", "-v","quiet","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=30
    )
    try:
        total = float(dur_r.stdout.strip())
    except Exception:
        return []

    # Scene change timestamps
    sc_r = sp.run(
        ["ffprobe", "-v","quiet","-show_frames","-select_streams","v",
         "-show_entries","frame=pkt_pts_time,pict_type",
         "-of","csv", video_path],
        capture_output=True, text=True, timeout=120
    )
    # Count scene changes per segment (any non-P/B frame = scene boundary approx)
    sc_times = []
    for line in sc_r.stdout.splitlines():
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                t = float(parts[1])
                if "I" in parts[-1]:  # I-frame = scene cut
                    sc_times.append(t)
            except Exception:
                pass

    # Audio loudness per segment via ffmpeg
    loudness_r = sp.run(
        ["ffmpeg", "-i", video_path,
         "-af", f"asetnsamples={int(44100*seg_secs)},astats=metadata=1:reset=1",
         "-f","null","-"],
        capture_output=True, text=True, timeout=120
    )
    # Parse RMS per block
    rms_by_seg: dict = {}
    current_t = 0.0
    for line in loudness_r.stderr.splitlines():
        if "lavfi.astats.Overall.RMS_level" in line:
            try:
                val = float(line.split("=")[-1].strip())
                seg_idx = int(current_t / seg_secs)
                rms_by_seg[seg_idx] = max(rms_by_seg.get(seg_idx, -100), val)
            except Exception:
                pass
        if "pts_time" in line.lower():
            try:
                current_t = float(line.split(":")[-1].strip())
            except Exception:
                pass

    segments = []
    n_segs = max(1, int(total / seg_secs))
    for i in range(n_segs):
        t_start = i * seg_secs
        t_end   = min(t_start + seg_secs, total)
        # Count scene changes in this segment
        sc_count = sum(1 for t in sc_times if t_start <= t < t_end)
        # Audio RMS (higher = louder = more exciting)
        rms = rms_by_seg.get(i, -60.0)
        rms_norm = max(0.0, min(1.0, (rms + 60) / 40))  # -60dBFS..−20dBFS → 0..1
        sc_norm  = min(1.0, sc_count / 3.0)
        score = 0.6 * rms_norm + 0.4 * sc_norm
        segments.append({
            "idx": i, "start": round(t_start, 3), "end": round(t_end, 3),
            "rms": round(rms, 1), "scene_changes": sc_count, "score": round(score, 3)
        })

    return segments


def _detect_beats(music_path: str, approx_bpm: int = 120) -> list:
    """Return a list of beat timestamps derived from the music file.
    Uses ffmpeg audio spectrum — no librosa needed."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v","quiet","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1", music_path],
            capture_output=True, text=True, timeout=15
        )
        total = float(r.stdout.strip())
    except Exception:
        total = 60.0

    beat_interval = 60.0 / approx_bpm
    beats = []
    t = 0.0
    while t < total:
        beats.append(round(t, 3))
        t += beat_interval
    return beats


def _llm_pick_moments(transcript: dict, scores: list, target_secs: int,
                       style: str, instruction: str, priority: str) -> list:
    """Ask LLM to pick the best clip start/end times from the video.
    Falls back to top-scoring segments if LLM unavailable."""
    if not transcript.get("text","").strip():
        # No transcript — just use top-scored segments
        top = sorted(scores, key=lambda x: x["score"], reverse=True)
        needed = max(3, int(target_secs / 3))
        selected = sorted(top[:needed], key=lambda x: x["start"])
        return [{"start": s["start"], "end": s["end"], "reason": "high energy"} for s in selected]

    seg_text = transcript.get("text","")[:3000]
    top_segs = sorted(scores, key=lambda x: x["score"], reverse=True)[:20]
    segs_summary = "\n".join(
        f"t={s['start']:.1f}-{s['end']:.1f}s score={s['score']:.2f} rms={s['rms']}dB sc={s['scene_changes']}"
        for s in top_segs
    )

    style_info = SHORTS_STYLES.get(style, SHORTS_STYLES["energetic"])
    messages = [
        {"role": "system", "content":
         f"You are a video editor making a {target_secs}-second social media short in '{style}' style: {style_info['desc']}. "
         f"Clip length range: {style_info['min_clip']}-{style_info['max_clip']}s. "
         "Return ONLY JSON: {\"clips\":[{\"start\":float,\"end\":float,\"reason\":string},...]}. "
         "Clips must not overlap. Total duration must be close to the target."},
        {"role": "user", "content":
         f"Target: {target_secs}s short. Instruction: {instruction or 'pick the best moments'}.\n\n"
         f"Transcript (excerpt):\n{seg_text}\n\n"
         f"Top excitement segments:\n{segs_summary}\n\n"
         f"Pick clips that total ~{target_secs}s. Output JSON only."}
    ]

    try:
        raw, provider = call_llm(messages, priority=priority)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            clips = data.get("clips", [])
            if clips and isinstance(clips[0], dict) and "start" in clips[0]:
                print(f"[shorts] LLM ({provider}) picked {len(clips)} clips")
                return clips
    except Exception as e:
        print(f"[shorts] LLM pick failed ({e}), falling back to top scores")

    # Fallback: top scoring segments
    top = sorted(scores, key=lambda x: x["score"], reverse=True)
    needed = max(3, int(target_secs / 3))
    selected = sorted(top[:needed], key=lambda x: x["start"])
    return [{"start": s["start"], "end": s["end"], "reason": "high energy"} for s in selected]


def _render_short(video_path: str, music_path: str, clips: list,
                   out_path: str, aspect: str, fade: bool = False) -> bool:
    """Assemble clips + music into a short video with ffmpeg."""
    if not clips:
        return False

    tmp_dir = Path(out_path).parent / "tmp_clips"
    tmp_dir.mkdir(exist_ok=True)
    clip_files = []

    # Extract each clip
    for i, clip in enumerate(clips):
        start = clip["start"]
        dur   = max(0.5, clip["end"] - clip["start"])
        cf    = str(tmp_dir / f"clip_{i:03d}.mp4")
        args  = ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur),
                  "-i", video_path, "-c:v","libx264","-preset","fast",
                  "-c:a","aac","-ar","44100","-ac","2", cf]
        r = subprocess.run(args, capture_output=True, timeout=60)
        if r.returncode == 0:
            clip_files.append(cf)

    if not clip_files:
        return False

    # Build concat list
    concat_list = str(tmp_dir / "concat.txt")
    Path(concat_list).write_text("\n".join(f"file '{f}'" for f in clip_files))

    # Concat all clips
    concat_out = str(tmp_dir / "concat.mp4")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f","concat","-safe","0","-i", concat_list,
         "-c","copy", concat_out],
        capture_output=True, timeout=120
    )
    if r.returncode != 0:
        return False

    # Crop to aspect ratio
    if aspect == "9:16":
        crop_filter = "crop=ih*9/16:ih,scale=1080:1920:force_original_aspect_ratio=disable"
    elif aspect == "1:1":
        crop_filter = "crop=ih:ih,scale=1080:1080:force_original_aspect_ratio=disable"
    else:
        crop_filter = "scale=1920:1080"

    if fade:
        # Add fade between clips — approximate via overlay fade filter
        vfilter = f"{crop_filter},fade=t=in:st=0:d=0.3"
    else:
        vfilter = crop_filter

    # Mix with music if provided
    if music_path and Path(music_path).exists():
        total_dur = sum(max(0, c["end"] - c["start"]) for c in clips)
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-i", concat_out, "-i", music_path,
             "-vf", vfilter,
             "-filter_complex",
             f"[0:a]volume=0.2[va];[1:a]volume=1.0,atrim=0:{total_dur}[ma];[va][ma]amix=inputs=2:duration=first[a]",
             "-map","0:v","-map","[a]",
             "-c:v","libx264","-preset","fast","-crf","23",
             "-c:a","aac","-shortest",
             out_path],
            capture_output=True, timeout=300
        )
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", concat_out,
             "-vf", vfilter,
             "-c:v","libx264","-preset","fast","-crf","23",
             "-c:a","aac",
             out_path],
            capture_output=True, timeout=300
        )

    # Cleanup tmp
    try:
        shutil.rmtree(str(tmp_dir))
    except Exception:
        pass

    return r.returncode == 0 and Path(out_path).exists()


def run_shorts_pipeline(sjid: str):
    """Full Shorts Builder pipeline."""
    with _shorts_lock:
        job = dict(_shorts_jobs[sjid])

    video_path  = job["source_path"]
    target_secs = job.get("duration", 30)
    style       = job.get("style", "energetic")
    music_path  = job.get("music_path", "")
    instruction = job.get("instruction", "")
    priority    = job.get("priority", "normal")
    pdir        = Path(PROJECTS_DIR) / "shorts" / sjid
    pdir.mkdir(parents=True, exist_ok=True)

    def upd(step, status, msg=""):
        with _shorts_lock:
            _shorts_jobs[sjid].setdefault("steps", {})[step] = {
                "status": status, "msg": msg, "ts": now_iso()
            }
        print(f"[shorts] {step}: {status} — {msg}")

    try:
        # 1. Transcribe (cached)
        upd("transcript", "running", "Transcribing...")
        tp = pdir / "transcript.json"
        if tp.exists():
            transcript = json.loads(tp.read_text())
        else:
            try:
                transcript = run_whisper(video_path)
                tp.write_text(json.dumps(transcript, indent=2))
            except Exception:
                transcript = {"text": "", "segments": [], "duration": 0}
        upd("transcript", "done", f"{len(transcript.get('segments',[]))} segments")

        # 2. Score excitement
        upd("score", "running", "Scoring excitement...")
        scores = _score_segments(video_path)
        (pdir / "scores.json").write_text(json.dumps(scores, indent=2))
        avg_score = round(sum(s["score"] for s in scores) / max(len(scores),1), 3)
        upd("score", "done", f"{len(scores)} segments, avg={avg_score}")

        # 3. Pick best moments with LLM
        upd("pick", "running", "Picking best moments...")
        style_cfg = SHORTS_STYLES.get(style, SHORTS_STYLES["energetic"])
        clips = _llm_pick_moments(transcript, scores, target_secs, style, instruction, priority)

        # Clamp clip durations to style range
        final_clips = []
        for c in clips:
            dur = c["end"] - c["start"]
            if dur < 0.5:
                continue
            if dur < style_cfg["min_clip"]:
                c["end"] = c["start"] + style_cfg["min_clip"]
            if dur > style_cfg["max_clip"]:
                c["end"] = c["start"] + style_cfg["max_clip"]
            final_clips.append(c)

        total_dur = sum(c["end"] - c["start"] for c in final_clips)
        (pdir / "clips.json").write_text(json.dumps(final_clips, indent=2))
        upd("pick", "done", f"{len(final_clips)} clips, ~{total_dur:.0f}s total")

        # 4. Render 9:16 reel
        upd("render_reel", "running", "Rendering 9:16 reel...")
        reel_path   = str(pdir / f"{sjid}_Reel.mp4")
        fade = (style_cfg["transition"] == "fade")
        ok_reel = _render_short(video_path, music_path, final_clips,
                                 reel_path, "9:16", fade=fade)
        upd("render_reel", "done" if ok_reel else "error",
            "Reel.mp4 ready" if ok_reel else "render failed")

        # 5. Render 1:1 square
        upd("render_square", "running", "Rendering 1:1 square...")
        sq_path = str(pdir / f"{sjid}_Square.mp4")
        ok_sq = _render_short(video_path, music_path, final_clips,
                               sq_path, "1:1", fade=fade)
        upd("render_square", "done" if ok_sq else "error",
            "Square.mp4 ready" if ok_sq else "render failed")

        with _shorts_lock:
            _shorts_jobs[sjid]["status"]     = "done"
            _shorts_jobs[sjid]["done_at"]    = now_iso()
            _shorts_jobs[sjid]["reel_path"]  = reel_path if ok_reel else None
            _shorts_jobs[sjid]["sq_path"]    = sq_path   if ok_sq   else None
            _shorts_jobs[sjid]["clips"]      = final_clips
            _shorts_jobs[sjid]["total_dur"]  = round(total_dur, 1)

        fname = Path(video_path).name
        send_telegram(
            f"🎬 <b>Short ready</b> — {fname}\n"
            f"Style: {style_cfg['icon']} {style_cfg['label']} · {target_secs}s target\n"
            f"{len(final_clips)} clips · ~{total_dur:.0f}s\n"
            f"🔗 http://{DIRECTOR_HOST}:{DIRECTOR_PORT}/shorts"
        )

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[shorts] Error for {sjid}: {tb}")
        with _shorts_lock:
            _shorts_jobs[sjid]["status"] = "error"
            _shorts_jobs[sjid]["error"]  = str(e)
        send_telegram(f"❌ Shorts FAILED: {sjid[:8]}\n{str(e)[:150]}")


@app.route("/api/shorts", methods=["POST"])
def api_shorts_start():
    data = request.get_json(force=True) or {}
    video_path = data.get("video_path","")
    if not video_path or not Path(video_path).exists():
        return jsonify({"error": f"video_path not found: {video_path}"}), 400

    sjid = gen_id()
    job = {
        "job_id":       sjid,
        "status":       "running",
        "created_at":   now_iso(),
        "source_path":  video_path,
        "source_name":  Path(video_path).name,
        "duration":     int(data.get("duration", 30)),
        "style":        data.get("style", "energetic"),
        "music_path":   data.get("music_path", ""),
        "instruction":  data.get("instruction", ""),
        "priority":     data.get("priority", "normal"),
        "channel_id":   data.get("channel_id", ""),
        "channel_name": data.get("channel_name", ""),
        "info":         data.get("info", ""),
        "steps":        {}
    }
    with _shorts_lock:
        _shorts_jobs[sjid] = job

    threading.Thread(target=run_shorts_pipeline, args=(sjid,), daemon=True).start()
    return jsonify({"job_id": sjid, "status": "running"})

@app.route("/api/shorts/<sjid>")
def api_shorts_status(sjid):
    with _shorts_lock:
        job = _shorts_jobs.get(sjid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@app.route("/api/shorts/<sjid>/media/<fname>")
def api_shorts_media(sjid, fname):
    fpath = Path(PROJECTS_DIR) / "shorts" / sjid / fname
    if not fpath.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(str(fpath))

@app.route("/shorts/upload", methods=["POST"])
def shorts_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    upload_dir = Path("/opt/studio/media/shorts-uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe = f"{gen_id()}_{Path(f.filename).name}"
    dest = upload_dir / safe
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": f.filename})

@app.route("/shorts/music-upload", methods=["POST"])
def shorts_music_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    music_dir = Path("/opt/studio/music")
    music_dir.mkdir(parents=True, exist_ok=True)
    safe = f"{Path(f.filename).stem}_{gen_id()[:6]}{Path(f.filename).suffix}"
    dest = music_dir / safe
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": f.filename})

@app.route("/api/shorts/styles")
def api_shorts_styles():
    return jsonify(SHORTS_STYLES)

@app.route("/shorts")
def shorts_page():
    html = SHORTS_HTML.replace("__DIRECTOR_URL__",
                                f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}")
    return Response(html, mimetype="text/html")


SHORTS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shorts Builder — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0d;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.hero{background:linear-gradient(135deg,#0f0520 0%,#1a0535 50%,#0d1a35 100%);padding:40px 24px 32px;text-align:center}
.hero h1{font-size:2.2rem;font-weight:800;background:linear-gradient(135deg,#a78bfa,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
.hero p{color:#888;font-size:1rem;max-width:500px;margin:0 auto}
.container{max-width:820px;margin:0 auto;padding:32px 24px}
.section{margin-bottom:28px}
.section-title{font-size:.8rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#6d28d9;margin-bottom:12px}
.card{background:#13131a;border:1px solid #1e1e2e;border-radius:12px;padding:20px}
.drop-zone{border:2px dashed #2a2a3e;border-radius:10px;padding:40px;text-align:center;cursor:pointer;transition:all .2s}
.drop-zone:hover,.drop-zone.drag{border-color:#7c3aed;background:#12082a}
.drop-zone .icon{font-size:2.5rem;margin-bottom:10px}
.drop-zone .label{color:#888;font-size:.9rem}
.drop-zone .sub{color:#555;font-size:.8rem;margin-top:4px}
.file-ok{color:#a78bfa;font-size:.9rem;margin-top:10px;font-weight:600}
.styles-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.style-card{background:#0e0e18;border:2px solid #1e1e2e;border-radius:10px;padding:16px;cursor:pointer;transition:all .15s;text-align:center}
.style-card:hover{border-color:#4c1d95;background:#130e22}
.style-card.selected{border-color:#7c3aed;background:#160d2a}
.style-icon{font-size:1.8rem;margin-bottom:6px}
.style-name{font-weight:700;font-size:.95rem;margin-bottom:4px}
.style-desc{color:#777;font-size:.75rem;line-height:1.4}
.dur-row{display:flex;gap:10px;flex-wrap:wrap}
.dur-btn{flex:1;min-width:70px;background:#0e0e18;border:2px solid #1e1e2e;border-radius:8px;padding:10px;text-align:center;cursor:pointer;font-size:.9rem;font-weight:600;transition:all .15s;color:#e0e0e0}
.dur-btn:hover{border-color:#4c1d95}
.dur-btn.selected{border-color:#7c3aed;background:#130e22;color:#a78bfa}
.music-row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.music-label{color:#888;font-size:.85rem;flex:1}
.btn{background:#6d28d9;color:#fff;border:none;border-radius:8px;padding:10px 20px;cursor:pointer;font-size:.9rem;font-weight:700;transition:background .15s}
.btn:hover{background:#7c3aed}
.btn:disabled{background:#2a1a4a;color:#555;cursor:not-allowed}
.btn-outline{background:transparent;border:1px solid #333;color:#aaa}
.btn-outline:hover{border-color:#7c3aed;color:#e0e0e0;background:transparent}
.go-btn{width:100%;padding:14px;font-size:1.1rem;margin-top:8px;border-radius:10px}
.progress-section{display:none}
.step-row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid #1a1a2a}
.step-row:last-child{border-bottom:none}
.step-icon{width:28px;text-align:center;font-size:1.1rem}
.step-name{flex:1;font-size:.9rem}
.step-status{font-size:.82rem;color:#888}
.step-status.ok{color:#34d399}
.step-status.run{color:#fbbf24}
.step-status.err{color:#f87171}
.results-section{display:none}
.result-vids{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
@media(max-width:600px){.result-vids{grid-template-columns:1fr}}
.result-card{background:#0e0e18;border:1px solid #1e1e2e;border-radius:10px;overflow:hidden}
.result-card video{width:100%;aspect-ratio:9/16;object-fit:cover;background:#000}
.result-card.square video{aspect-ratio:1/1}
.result-info{padding:12px}
.result-label{font-weight:700;font-size:.9rem;margin-bottom:8px}
.dl-btn{display:block;text-align:center;background:#1a0535;color:#a78bfa;border-radius:6px;padding:7px;font-size:.85rem;text-decoration:none;font-weight:600}
.dl-btn:hover{background:#2a0a55}
.elapsed{color:#555;font-size:.8rem;margin-top:8px;text-align:center}
.tag{display:inline-block;background:#1a0535;color:#a78bfa;border-radius:4px;padding:2px 8px;font-size:.75rem;font-weight:600;margin-right:4px}
input[type=file]{display:none}
</style>
</head>
<body>
<div class="hero">
  <h1>Shorts Builder</h1>
  <p>Drop a video — AI finds the best moments, cuts to the beat, exports a 9:16 reel and 1:1 square ready to upload.</p>
</div>

<div class="container">

  <!-- Step 1: Video -->
  <div class="section">
    <div class="section-title">1 — Your Video</div>
    <div class="card">
      <div class="drop-zone" id="drop-zone" onclick="document.getElementById('vid-input').click()"
           ondragover="event.preventDefault();this.classList.add('drag')"
           ondragleave="this.classList.remove('drag')"
           ondrop="handleDrop(event)">
        <div class="icon">🎬</div>
        <div class="label">Drop your video here or click to browse</div>
        <div class="sub">MP4, MOV, AVI, MKV — any length</div>
      </div>
      <input type="file" id="vid-input" accept="video/*" onchange="handleFile(this.files[0])">
      <div class="file-ok" id="vid-ok" style="display:none"></div>
    </div>
  </div>

  <!-- Step 2: Style -->
  <div class="section">
    <div class="section-title">2 — Style</div>
    <div class="styles-grid" id="styles-grid"></div>
  </div>

  <!-- Step 3: Duration -->
  <div class="section">
    <div class="section-title">3 — Target Duration</div>
    <div class="card">
      <div class="dur-row" id="dur-row">
        <div class="dur-btn selected" data-secs="15" onclick="setDur(this)">15s<br><span style="font-size:.7rem;color:#888">TikTok</span></div>
        <div class="dur-btn selected" data-secs="30" onclick="setDur(this)">30s<br><span style="font-size:.7rem;color:#888">Reel</span></div>
        <div class="dur-btn" data-secs="60" onclick="setDur(this)">60s<br><span style="font-size:.7rem;color:#888">YouTube</span></div>
        <div class="dur-btn" data-secs="90" onclick="setDur(this)">90s<br><span style="font-size:.7rem;color:#888">Extended</span></div>
      </div>
    </div>
  </div>

  <!-- Step 4: Music -->
  <div class="section">
    <div class="section-title">4 — Music (optional)</div>
    <div class="card">
      <div class="music-row">
        <span class="music-label" id="music-label">No music — upload a track to mix in (beat-synced cuts)</span>
        <button class="btn btn-outline" onclick="document.getElementById('music-input').click()">🎵 Upload Music</button>
        <button class="btn btn-outline" id="music-clear" onclick="clearMusic()" style="display:none">✕</button>
      </div>
      <input type="file" id="music-input" accept="audio/*" onchange="handleMusic(this.files[0])">
    </div>
  </div>

  <!-- Optional instruction -->
  <div class="section">
    <div class="section-title">5 — Instruction (optional)</div>
    <div class="card">
      <input id="instr" type="text" placeholder='e.g. "Focus on the funny reactions" or "Keep the best kills"'
             style="width:100%;background:#0e0e18;border:1px solid #1e1e2e;border-radius:7px;padding:10px 14px;color:#e0e0e0;font-size:.9rem">
    </div>
  </div>

  <!-- Channel & Info -->
  <div class="section">
    <div class="section-title">6 — Channel &amp; Info (optional)</div>
    <div class="card" style="display:grid;gap:10px">
      <select id="short-chan" style="background:#0e0e18;border:1px solid #1e1e2e;border-radius:7px;padding:10px 14px;color:#e0e0e0;font-size:.9rem">
        <option value="">No channel selected</option>
      </select>
      <input id="short-info" type="text" placeholder="Extra info (e.g. Gaming highlights — keep best kills)"
             style="width:100%;background:#0e0e18;border:1px solid #1e1e2e;border-radius:7px;padding:10px 14px;color:#e0e0e0;font-size:.9rem">
    </div>
  </div>

  <!-- Go -->
  <button class="btn go-btn" id="go-btn" onclick="startShort()" disabled>
    🚀 Build My Short
  </button>

  <!-- Progress -->
  <div class="progress-section" id="progress-section" style="margin-top:28px">
    <div class="section-title">Building...</div>
    <div class="card" id="steps-card">
      <div class="step-row"><div class="step-icon">🎙️</div><div class="step-name">Transcribe</div><div class="step-status" id="ps-transcript">waiting</div></div>
      <div class="step-row"><div class="step-icon">📊</div><div class="step-name">Score Excitement</div><div class="step-status" id="ps-score">waiting</div></div>
      <div class="step-row"><div class="step-icon">🤖</div><div class="step-name">AI Picks Best Moments</div><div class="step-status" id="ps-pick">waiting</div></div>
      <div class="step-row"><div class="step-icon">📱</div><div class="step-name">Render 9:16 Reel</div><div class="step-status" id="ps-render_reel">waiting</div></div>
      <div class="step-row"><div class="step-icon">⬜</div><div class="step-name">Render 1:1 Square</div><div class="step-status" id="ps-render_square">waiting</div></div>
    </div>
    <div class="elapsed" id="elapsed"></div>
  </div>

  <!-- Results -->
  <div class="results-section" id="results-section" style="margin-top:28px">
    <div class="section-title">Your Shorts</div>
    <div class="result-vids">
      <div class="result-card" id="reel-card">
        <video id="reel-vid" controls playsinline></video>
        <div class="result-info">
          <div class="result-label">📱 9:16 Reel <span class="tag">TikTok</span><span class="tag">Instagram</span></div>
          <a class="dl-btn" id="reel-dl" href="#" download>⬇ Download Reel</a>
        </div>
      </div>
      <div class="result-card square" id="sq-card">
        <video id="sq-vid" controls playsinline></video>
        <div class="result-info">
          <div class="result-label">⬜ 1:1 Square <span class="tag">Feed</span><span class="tag">Facebook</span></div>
          <a class="dl-btn" id="sq-dl" href="#" download>⬇ Download Square</a>
        </div>
      </div>
    </div>
    <div style="text-align:center">
      <button class="btn btn-outline" onclick="location.reload()">+ Make Another</button>
    </div>
  </div>

</div><!-- /container -->

<script>
const API = '__DIRECTOR_URL__';
let uploadedPath = '', musicPath = '', selectedStyle = 'energetic', selectedDur = 30;
let jobId = '', pollTimer = null, startTime = 0;

fetch(API+'/api/channels').then(r=>r.json()).then(chs=>{
  const sel = document.getElementById('short-chan');
  chs.forEach(c=>{
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.name;
    sel.appendChild(o);
  });
}).catch(()=>{});

// Load styles
fetch(API+'/api/shorts/styles').then(r=>r.json()).then(styles => {
  const grid = document.getElementById('styles-grid');
  grid.innerHTML = Object.entries(styles).map(([key,s])=>`
    <div class="style-card${key==='energetic'?' selected':''}" data-key="${key}" onclick="selectStyle(this,'${key}')">
      <div class="style-icon">${s.icon}</div>
      <div class="style-name">${s.label}</div>
      <div class="style-desc">${s.desc}</div>
    </div>`).join('');
});

// Set default duration
document.querySelectorAll('.dur-btn').forEach(b => b.classList.remove('selected'));
document.querySelector('[data-secs="30"]').classList.add('selected');

function selectStyle(el, key) {
  document.querySelectorAll('.style-card').forEach(c=>c.classList.remove('selected'));
  el.classList.add('selected');
  selectedStyle = key;
}

function setDur(el) {
  document.querySelectorAll('.dur-btn').forEach(b=>b.classList.remove('selected'));
  el.classList.add('selected');
  selectedDur = parseInt(el.dataset.secs);
}

function handleDrop(e) {
  e.preventDefault();
  document.getElementById('drop-zone').classList.remove('drag');
  const file = e.dataTransfer.files[0];
  if (file) handleFile(file);
}

async function handleFile(file) {
  if (!file) return;
  const dz = document.getElementById('drop-zone');
  dz.innerHTML = '<div class="icon">⏳</div><div class="label">Uploading...</div>';
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(API+'/shorts/upload', {method:'POST', body:fd});
  const j = await r.json();
  if (j.path) {
    uploadedPath = j.path;
    dz.innerHTML = `<div class="icon">✅</div><div class="label" style="color:#a78bfa">${file.name}</div><div class="sub">${(file.size/1024/1024).toFixed(1)} MB</div>`;
    document.getElementById('go-btn').disabled = false;
  } else {
    dz.innerHTML = `<div class="icon">❌</div><div class="label">Upload failed</div>`;
  }
}

async function handleMusic(file) {
  if (!file) return;
  document.getElementById('music-label').textContent = 'Uploading music...';
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(API+'/shorts/music-upload', {method:'POST', body:fd});
  const j = await r.json();
  if (j.path) {
    musicPath = j.path;
    document.getElementById('music-label').textContent = '🎵 ' + file.name;
    document.getElementById('music-clear').style.display = '';
  }
}

function clearMusic() {
  musicPath = '';
  document.getElementById('music-label').textContent = 'No music — upload a track to mix in';
  document.getElementById('music-clear').style.display = 'none';
}

async function startShort() {
  if (!uploadedPath) return;
  document.getElementById('go-btn').disabled = true;
  document.getElementById('go-btn').textContent = '⏳ Building...';
  document.getElementById('progress-section').style.display = '';
  document.getElementById('progress-section').scrollIntoView({behavior:'smooth'});

  const chanSel = document.getElementById('short-chan');
  const chanId = chanSel.value;
  const chanName = chanId ? chanSel.options[chanSel.selectedIndex].text : '';
  const body = {
    video_path: uploadedPath,
    duration: selectedDur,
    style: selectedStyle,
    music_path: musicPath,
    instruction: document.getElementById('instr').value.trim(),
    channel_id: chanId, channel_name: chanName,
    info: document.getElementById('short-info').value.trim()
  };

  const r = await fetch(API+'/api/shorts', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if (j.job_id) {
    jobId = j.job_id;
    startTime = Date.now();
    pollTimer = setInterval(poll, 3000);
    poll();
  }
}

const STEPS = ['transcript','score','pick','render_reel','render_square'];
async function poll() {
  if (!jobId) return;
  const r = await fetch(API+'/api/shorts/'+jobId);
  const j = await r.json();
  const steps = j.steps || {};
  STEPS.forEach(s => {
    const el = document.getElementById('ps-'+s);
    if (!el) return;
    const st = steps[s];
    if (!st) return;
    el.className = 'step-status';
    if (st.status==='done')    { el.className+=' ok';  el.textContent='✓ '+st.msg; }
    else if (st.status==='error'){ el.className+=' err'; el.textContent='✗ '+(st.msg||'failed'); }
    else if (st.status==='running'){ el.className+=' run'; el.textContent='⟳ '+st.msg; }
    else el.textContent = st.msg || st.status;
  });
  const secs = Math.floor((Date.now()-startTime)/1000);
  document.getElementById('elapsed').textContent = `${Math.floor(secs/60)}m ${secs%60}s elapsed`;

  if (j.status === 'done') {
    clearInterval(pollTimer);
    showResults(j);
  } else if (j.status === 'error') {
    clearInterval(pollTimer);
    document.getElementById('elapsed').textContent = '❌ Error: ' + (j.error||'unknown');
  }
}

function showResults(job) {
  document.getElementById('results-section').style.display = '';
  document.getElementById('results-section').scrollIntoView({behavior:'smooth'});

  const base = API+'/api/shorts/'+job.job_id+'/media/';
  const reelFile = job.job_id+'_Reel.mp4';
  const sqFile   = job.job_id+'_Square.mp4';

  if (job.reel_path) {
    document.getElementById('reel-vid').src = base+reelFile;
    document.getElementById('reel-dl').href = base+reelFile;
    document.getElementById('reel-dl').download = reelFile;
  }
  if (job.sq_path) {
    document.getElementById('sq-vid').src = base+sqFile;
    document.getElementById('sq-dl').href = base+sqFile;
    document.getElementById('sq-dl').download = sqFile;
  }
}
</script>
</body>
</html>"""


# ── Vlog Builder — multi-clip assembler with intro/outro ─────────────────────
_vlog_jobs: dict = {}
_vlog_lock = threading.Lock()

def _make_thumbnail(video_path: str, out_path: str, t: float = 1.0) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
         "-vframes","1", "-q:v","3", "-vf","scale=320:-1", out_path],
        capture_output=True, timeout=15
    )
    return r.returncode == 0 and Path(out_path).exists()

def _get_duration(path: str) -> float:
    r = subprocess.run(
        ["ffprobe","-v","quiet","-show_entries","format=duration",
         "-of","default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=15
    )
    try:
        return float(r.stdout.strip())
    except Exception:
        return 0.0

def _remove_silences_from_clip(src: str, dst: str, threshold_db: float = -35.0) -> bool:
    """Cut silences from a single clip, output to dst."""
    # Get silence timestamps
    r = subprocess.run(
        ["ffmpeg", "-i", src,
         "-af", f"silencedetect=noise={threshold_db}dB:d=0.8",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=120
    )
    lines = r.stderr.splitlines()
    dur = _get_duration(src)

    # Parse silence start/end
    silences = []
    current_start = None
    for line in lines:
        if "silence_start" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip())
            except Exception:
                pass
        if "silence_end" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[1].split("|")[0].strip())
                if end - current_start > 0.5:
                    silences.append((current_start, end))
                current_start = None
            except Exception:
                pass

    if not silences:
        # No silences to remove — just copy
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
            capture_output=True, timeout=120
        )
        return r2.returncode == 0

    # Build keep segments
    keep = []
    t = 0.0
    for ss, se in sorted(silences):
        if ss > t + 0.1:
            keep.append((t, ss))
        t = se
    if t < dur - 0.1:
        keep.append((t, dur))

    if not keep:
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
            capture_output=True, timeout=120
        )
        return r2.returncode == 0

    # Extract each keep segment then concat
    tmp = Path(dst).parent / f"_vtmp_{Path(dst).stem}"
    tmp.mkdir(exist_ok=True)
    segs = []
    for i, (ks, ke) in enumerate(keep):
        seg = str(tmp / f"seg_{i:04d}.mp4")
        r2 = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(ks), "-t", str(ke - ks),
             "-i", src, "-c:v","libx264","-preset","fast",
             "-c:a","aac","-ar","44100","-ac","2", seg],
            capture_output=True, timeout=60
        )
        if r2.returncode == 0:
            segs.append(seg)

    if not segs:
        subprocess.run(["ffmpeg","-y","-i",src,"-c","copy",dst], capture_output=True, timeout=60)
        return True

    concat_txt = str(tmp / "concat.txt")
    Path(concat_txt).write_text("\n".join(f"file '{s}'" for s in segs))
    r3 = subprocess.run(
        ["ffmpeg", "-y", "-f","concat","-safe","0","-i", concat_txt,
         "-c","copy", dst],
        capture_output=True, timeout=300
    )
    try:
        shutil.rmtree(str(tmp))
    except Exception:
        pass
    return r3.returncode == 0 and Path(dst).exists()


def _build_tts_narration(segments, pdir):
    """Build a TTS narration WAV aligned to assembled segment durations.
    segments: list of {"description": str, "norm_dur": float}
    Returns path to combined narration WAV, or None if no descriptions."""
    audio_parts = []
    has_any = False
    for i, seg in enumerate(segments):
        dur = max(seg.get("norm_dur", 0.0), 0.5)
        desc = seg.get("description", "").strip()
        if desc:
            tts_out = str(pdir / f"tts_{i:02d}.wav")
            ok = generate_tts_audio(desc, tts_out)
            if ok:
                tts_dur = _get_duration(tts_out)
                if tts_dur < dur - 0.3:
                    padded = str(pdir / f"tts_{i:02d}_p.wav")
                    r = subprocess.run([
                        "ffmpeg", "-y", "-i", tts_out,
                        "-af", f"apad=whole_dur={dur:.3f}",
                        "-ar", "44100", "-ac", "2", padded
                    ], capture_output=True, timeout=30)
                    audio_parts.append(padded if r.returncode == 0 and Path(padded).exists() else tts_out)
                else:
                    audio_parts.append(tts_out)
                has_any = True
                print(f"[vlog] TTS seg {i}: '{desc[:40]}' → {tts_dur:.1f}s")
            else:
                sil = str(pdir / f"tts_sil_{i:02d}.wav")
                subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", sil],
                    capture_output=True, timeout=15)
                audio_parts.append(sil)
        else:
            sil = str(pdir / f"tts_sil_{i:02d}.wav")
            subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                f"anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", sil],
                capture_output=True, timeout=15)
            audio_parts.append(sil)

    if not has_any:
        return None
    valid = [p for p in audio_parts if Path(p).exists()]
    if not valid:
        return None
    concat_txt = str(pdir / "tts_concat.txt")
    Path(concat_txt).write_text("\n".join(f"file '{p}'" for p in valid))
    narration = str(pdir / "tts_narration.wav")
    r = subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
        "-ar", "44100", "-ac", "2", narration
    ], capture_output=True, timeout=180)
    return narration if r.returncode == 0 and Path(narration).exists() else None


def run_vlog_pipeline(vjid: str):
    with _vlog_lock:
        job = dict(_vlog_jobs[vjid])

    pdir = Path(PROJECTS_DIR) / "vlogs" / vjid
    pdir.mkdir(parents=True, exist_ok=True)
    clips     = job.get("clips", [])
    intro     = job.get("intro_path", "")
    outro     = job.get("outro_path", "")
    title     = job.get("title", "My Vlog")
    rm_sil    = job.get("remove_silences", True)

    def upd(step, status, msg=""):
        with _vlog_lock:
            _vlog_jobs[vjid].setdefault("steps", {})[step] = {
                "status": status, "msg": msg, "ts": now_iso()
            }
        print(f"[vlog] {step}: {status} — {msg}")

    try:
        # assembled_segs: {path, description} — used for TTS alignment
        assembled_paths = []
        assembled_segs  = []  # {description, norm_dur} filled after re-encode

        # Prepend intro (no description)
        if intro and Path(intro).exists():
            assembled_paths.append((intro, ""))
            upd("intro", "done", Path(intro).name)

        # Process each clip — enrich descriptions with context if available
        use_context = job.get("use_context", False)
        for i, clip in enumerate(clips):
            src = clip.get("path","")
            if not src or not Path(src).exists():
                upd(f"clip_{i+1}", "error", f"file not found: {src}")
                continue

            # Pull context for this clip if GPS sync was done
            description = clip.get("description", "").strip()
            if use_context and clip.get("creation_time"):
                ctx_evs = match_clip_to_context(clip["creation_time"], clip.get("duration", 60))
                ctx_str = format_context_for_ai(ctx_evs)
                if ctx_str:
                    if description:
                        description = f"{description} [{ctx_str}]"
                    else:
                        description = ctx_str
                    print(f"[vlog] clip_{i+1} context: {ctx_str}")

            upd(f"clip_{i+1}", "running",
                f"{clip.get('filename','')} — {description[:40] or '(no desc)'}")

            if rm_sil:
                cleaned = str(pdir / f"clip_{i+1:02d}_cleaned.mp4")
                ok = _remove_silences_from_clip(src, cleaned)
                out_clip = cleaned if ok else src
            else:
                out_clip = src

            assembled_paths.append((out_clip, description))
            dur = _get_duration(out_clip)
            upd(f"clip_{i+1}", "done",
                f"{clip.get('description','clip')[:30]} — {dur:.0f}s")

        # Append outro (no description)
        if outro and Path(outro).exists():
            assembled_paths.append((outro, ""))
            upd("outro", "done", Path(outro).name)

        if not assembled_paths:
            raise ValueError("No valid clips to assemble")

        # Re-encode each segment to common 1080p spec then concat
        upd("assemble", "running", f"Stitching {len(assembled_paths)} parts...")
        norm_dir = pdir / "norm"
        norm_dir.mkdir(exist_ok=True)
        norm_files = []
        for i, (f, desc) in enumerate(assembled_paths):
            nf = str(norm_dir / f"part_{i:03d}.mp4")
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", f,
                 "-vf","scale=1920:1080:force_original_aspect_ratio=decrease,"
                        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                 "-c:v","libx264","-preset","fast","-crf","22",
                 "-r","30","-c:a","aac","-ar","44100","-ac","2", nf],
                capture_output=True, timeout=300
            )
            if r.returncode == 0:
                norm_files.append(nf)
                assembled_segs.append({"description": desc, "norm_dur": _get_duration(nf)})

        concat_txt = str(pdir / "concat.txt")
        Path(concat_txt).write_text("\n".join(f"file '{f}'" for f in norm_files))
        my_voice_file = str(pdir / f"{vjid}_MyVoice.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-f","concat","-safe","0","-i", concat_txt,
             "-c","copy", my_voice_file],
            capture_output=True, timeout=600
        )
        if r.returncode != 0:
            raise RuntimeError("ffmpeg concat failed: " + r.stderr[-300:])

        try:
            shutil.rmtree(str(norm_dir))
        except Exception:
            pass

        total_dur = _get_duration(my_voice_file)
        clip_count = len(clips)
        upd("assemble", "done", f"{clip_count} clips → {total_dur:.0f}s")

        # Build TTS narration track
        ai_voice_file = None
        both_tracks_file = None
        upd("tts_narration", "running", "Generating AI voice narration...")
        narration_wav = _build_tts_narration(assembled_segs, pdir)
        if narration_wav:
            upd("tts_narration", "running", "Rendering AI Voice export...")
            ai_f = str(pdir / f"{vjid}_AIVoice.mp4")
            r = subprocess.run([
                "ffmpeg", "-y", "-i", my_voice_file, "-i", narration_wav,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-shortest",
                ai_f
            ], capture_output=True, timeout=300)
            if r.returncode == 0 and Path(ai_f).exists():
                ai_voice_file = ai_f
            bt_f = str(pdir / f"{vjid}_BothTracks.mp4")
            r = subprocess.run([
                "ffmpeg", "-y", "-i", my_voice_file, "-i", narration_wav,
                "-map", "0:v", "-map", "0:a", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-ar", "44100", "-shortest",
                bt_f
            ], capture_output=True, timeout=300)
            if r.returncode == 0 and Path(bt_f).exists():
                both_tracks_file = bt_f
            upd("tts_narration", "done",
                f"AI Voice {'✓' if ai_voice_file else '✗'} · Both Tracks {'✓' if both_tracks_file else '✗'}")
        else:
            upd("tts_narration", "done", "No clip descriptions — skipped")

        with _vlog_lock:
            _vlog_jobs[vjid]["status"]           = "done"
            _vlog_jobs[vjid]["done_at"]          = now_iso()
            _vlog_jobs[vjid]["out_file"]         = my_voice_file   # backwards compat
            _vlog_jobs[vjid]["my_voice_file"]    = my_voice_file
            _vlog_jobs[vjid]["ai_voice_file"]    = ai_voice_file
            _vlog_jobs[vjid]["both_tracks_file"] = both_tracks_file
            _vlog_jobs[vjid]["total_dur"]        = round(total_dur)
            _vlog_jobs[vjid]["clip_count"]       = clip_count

        exports = "My Voice"
        if ai_voice_file:   exports += " · AI Voice"
        if both_tracks_file: exports += " · Both Tracks"
        send_telegram(
            f"🎥 <b>Vlog ready</b> — {title}\n"
            f"{clip_count} clips · {total_dur//60:.0f}m {total_dur%60:.0f}s\n"
            f"Exports: {exports}\n"
            f"🔗 http://{DIRECTOR_HOST}:{DIRECTOR_PORT}/vlog"
        )

    except Exception as e:
        import traceback
        print(f"[vlog] Error {vjid}: {traceback.format_exc()}")
        with _vlog_lock:
            _vlog_jobs[vjid]["status"] = "error"
            _vlog_jobs[vjid]["error"]  = str(e)
        send_telegram(f"❌ Vlog FAILED: {vjid[:8]}\n{str(e)[:150]}")


# ── Vlog API routes ───────────────────────────────────────────────────────────
@app.route("/vlog/upload", methods=["POST"])
def vlog_upload():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    uid = gen_id()
    udir = Path("/opt/studio/media/vlog-uploads") / uid
    udir.mkdir(parents=True, exist_ok=True)
    safe = Path(f.filename).name
    dest = udir / safe
    f.save(str(dest))
    dur = _get_duration(str(dest))
    # Extract video metadata (creation_time, GPS, camera)
    vmeta = extract_video_metadata(str(dest))
    settings = load_settings()
    tz_off   = int(settings.get("timezone_offset", 0))

    # If no creation_time in metadata, try parsing from filename
    ct = vmeta.get("creation_time", "")
    if not ct:
        ct = parse_filename_ts(safe, tz_off)
        if ct:
            vmeta["creation_time"] = ct
            vmeta["ts_source"] = "filename"

    # Auto-locate: look up HA GPS at clip timestamp
    location_name = ""
    lat = vmeta.get("latitude")
    lon = vmeta.get("longitude")
    if ct and settings.get("context_auto_locate", True):
        loc_data = ha_locate_at_time(ct)
        if loc_data:
            lat = loc_data.get("latitude", lat)
            lon = loc_data.get("longitude", lon)
            location_name = loc_data.get("location_name", "")
    # Fallback: reverse geocode embedded GPS
    if not location_name and lat and lon:
        location_name = reverse_geocode(lat, lon)

    # Generate thumbnail
    thumb_path = str(udir / "thumb.jpg")
    _make_thumbnail(str(dest), thumb_path)
    thumb_url = f"/vlog/thumb/{uid}" if Path(thumb_path).exists() else ""

    return jsonify({
        "id":            uid,
        "path":          str(dest),
        "filename":      safe,
        "duration":      round(dur),
        "thumb_url":     thumb_url,
        "creation_time": vmeta.get("creation_time", ""),
        "ts_source":     vmeta.get("ts_source", "metadata"),
        "camera":        vmeta.get("camera", ""),
        "width":         vmeta.get("width", 0),
        "height":        vmeta.get("height", 0),
        "latitude":      lat,
        "longitude":     lon,
        "location_name": location_name,
    })

@app.route("/vlog/thumb/<uid>")
def vlog_thumb(uid):
    p = Path("/opt/studio/media/vlog-uploads") / uid / "thumb.jpg"
    if not p.exists():
        return "", 404
    return send_file(str(p), mimetype="image/jpeg")

@app.route("/api/vlog", methods=["POST"])
def api_vlog_start():
    data = request.get_json(force=True) or {}
    clips = data.get("clips", [])
    if not clips:
        return jsonify({"error": "clips required"}), 400
    vjid = gen_id()
    job = {
        "job_id":           vjid,
        "status":           "running",
        "created_at":       now_iso(),
        "title":            data.get("title", "My Vlog"),
        "clips":            clips,
        "intro_path":       data.get("intro_path", ""),
        "outro_path":       data.get("outro_path", ""),
        "remove_silences":  data.get("remove_silences", True),
        "channel_id":       data.get("channel_id", ""),
        "channel_name":     data.get("channel_name", ""),
        "info":             data.get("info", ""),
        "use_context":      data.get("use_context", False),
        "steps":            {}
    }
    with _vlog_lock:
        _vlog_jobs[vjid] = job
    threading.Thread(target=run_vlog_pipeline, args=(vjid,), daemon=True).start()
    return jsonify({"job_id": vjid, "status": "running"})

@app.route("/api/vlog/<vjid>")
def api_vlog_status(vjid):
    with _vlog_lock:
        job = _vlog_jobs.get(vjid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)

@app.route("/api/vlog/<vjid>/download")
def api_vlog_download(vjid):
    with _vlog_lock:
        job = _vlog_jobs.get(vjid)
    if not job:
        return jsonify({"error": "not found"}), 404
    fpath = job.get("my_voice_file") or job.get("out_file")
    if not fpath:
        return jsonify({"error": "not ready"}), 404
    p = Path(fpath)
    if not p.exists():
        return jsonify({"error": "file missing"}), 404
    return send_file(str(p), as_attachment=True, download_name=p.name)

@app.route("/api/vlog/<vjid>/download/<etype>")
def api_vlog_download_type(vjid, etype):
    with _vlog_lock:
        job = _vlog_jobs.get(vjid)
    if not job:
        return jsonify({"error": "not found"}), 404
    key_map = {
        "my-voice":    "my_voice_file",
        "ai-voice":    "ai_voice_file",
        "both-tracks": "both_tracks_file",
    }
    key = key_map.get(etype)
    if not key:
        return jsonify({"error": "unknown export type"}), 400
    fpath = job.get(key)
    if not fpath:
        return jsonify({"error": "export not available"}), 404
    p = Path(fpath)
    if not p.exists():
        return jsonify({"error": "file missing"}), 404
    return send_file(str(p), as_attachment=True, download_name=p.name)

@app.route("/api/vlog/<vjid>/opencut")
def api_vlog_opencut(vjid):
    """Inject vlog into OpenCut (same bridge as Edit Director)."""
    with _vlog_lock:
        job = _vlog_jobs.get(vjid)
    if not job or not job.get("out_file"):
        return jsonify({"error": "not ready"}), 404
    # Build a minimal OpenCut project pointing at the vlog file
    proj = {
        "metadata": {"id": vjid, "name": job.get("title","Vlog"), "createdAt": now_iso()},
        "media": [{"id":"vlog","src": f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}/api/vlog/{vjid}/download",
                   "type":"video","name":Path(job["out_file"]).name}],
        "timeline":{"tracks":[{"id":"v1","type":"video",
                    "clips":[{"id":"c1","mediaId":"vlog","startTime":0,
                              "endTime":job.get("total_dur",0),"mediaStartTime":0}]}]}
    }
    return jsonify(proj)

@app.route("/vlog")
def vlog_page():
    html = VLOG_HTML.replace("__DIRECTOR_URL__",
                              f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}")
    return Response(html, mimetype="text/html")


VLOG_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vlog Builder — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.hero{background:linear-gradient(135deg,#050e1a 0%,#0a1f0a 50%,#050e1a 100%);padding:36px 24px 28px;text-align:center;border-bottom:1px solid #0e2010}
.hero h1{font-size:2rem;font-weight:800;background:linear-gradient(135deg,#4ade80,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px}
.hero p{color:#666;font-size:.9rem;max-width:480px;margin:0 auto}
.container{max-width:860px;margin:0 auto;padding:28px 20px}
.section-title{font-size:.75rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#22c55e;margin-bottom:10px}
.card{background:#0d1117;border:1px solid #161f1a;border-radius:12px;padding:18px;margin-bottom:18px}
/* Clip list */
.clip-list{display:flex;flex-direction:column;gap:10px}
.clip-item{background:#0a0f0c;border:1px solid #1a2a1e;border-radius:10px;display:flex;align-items:flex-start;gap:12px;padding:12px;position:relative;cursor:grab}
.clip-item.drag-over{border-color:#22c55e;background:#0b170d}
.clip-thumb{width:80px;height:50px;object-fit:cover;border-radius:6px;background:#111;flex-shrink:0}
.clip-thumb-ph{width:80px;height:50px;background:#111;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#333;font-size:1.3rem;flex-shrink:0}
.clip-body{flex:1;min-width:0}
.clip-top{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.clip-num{background:#0e2a15;color:#22c55e;border-radius:4px;padding:2px 7px;font-size:.75rem;font-weight:700;flex-shrink:0}
.clip-name{font-size:.85rem;color:#aaa;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.clip-dur{font-size:.75rem;color:#555;flex-shrink:0}
.clip-desc{width:100%;background:#060c09;border:1px solid #1a2a1e;border-radius:6px;padding:7px 10px;color:#ccc;font-size:.85rem;resize:none}
.clip-desc:focus{outline:none;border-color:#22c55e}
.clip-del{position:absolute;top:8px;right:8px;background:none;border:none;color:#333;cursor:pointer;font-size:1rem;padding:2px 5px}
.clip-del:hover{color:#f87171}
.drag-handle{color:#333;cursor:grab;padding:0 4px;font-size:1rem;align-self:center}
/* Add clip zone */
.add-zone{border:2px dashed #1a2a1e;border-radius:10px;padding:28px;text-align:center;cursor:pointer;transition:all .2s}
.add-zone:hover,.add-zone.drag{border-color:#22c55e;background:#0a170c}
.add-zone .icon{font-size:2rem;margin-bottom:8px;color:#22c55e}
.add-zone .label{color:#666;font-size:.9rem}
/* Intro/Outro */
.io-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:580px){.io-row{grid-template-columns:1fr}}
.io-box{background:#0a0f0c;border:2px dashed #1a2a1e;border-radius:10px;padding:18px;text-align:center;cursor:pointer;transition:all .2s}
.io-box:hover{border-color:#22c55e}
.io-box .io-icon{font-size:1.8rem;margin-bottom:6px}
.io-box .io-label{font-size:.85rem;color:#666}
.io-box .io-ok{font-size:.82rem;color:#22c55e;margin-top:4px;font-weight:600}
/* Options */
.opts-row{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.toggle{display:flex;align-items:center;gap:8px;cursor:pointer;font-size:.85rem;color:#aaa}
.toggle input{width:16px;height:16px;accent-color:#22c55e}
/* Build button */
.build-btn{width:100%;padding:14px;font-size:1.05rem;font-weight:700;background:linear-gradient(135deg,#166534,#15803d);color:#fff;border:none;border-radius:10px;cursor:pointer;transition:opacity .15s;margin-top:4px}
.build-btn:hover:not(:disabled){opacity:.9}
.build-btn:disabled{opacity:.4;cursor:not-allowed}
/* Progress */
.step-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #0e1810}
.step-row:last-child{border-bottom:none}
.step-icon{width:24px;text-align:center}
.step-name{flex:1;font-size:.88rem}
.step-st{font-size:.8rem;color:#555}
.step-st.ok{color:#22c55e}.step-st.run{color:#fbbf24}.step-st.err{color:#f87171}
/* Results */
.result-box{background:#050d07;border:1px solid #0e2a15;border-radius:12px;padding:22px;text-align:center}
.result-dur{font-size:2rem;font-weight:800;color:#22c55e;margin-bottom:4px}
.result-sub{color:#666;font-size:.85rem;margin-bottom:18px}
.result-btns{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.rbtn{display:inline-flex;align-items:center;gap:6px;padding:10px 20px;border-radius:8px;font-weight:700;font-size:.9rem;cursor:pointer;text-decoration:none;border:none}
.rbtn-dl{background:#166534;color:#fff}
.rbtn-oc{background:#1e3a5f;color:#60a5fa}
.rbtn-sh{background:#3a1a5f;color:#a78bfa}
.rbtn:hover{opacity:.85}
.elapsed{color:#333;font-size:.78rem;text-align:center;margin-top:8px}
input[type=file]{display:none}
input[type=text]{width:100%;background:#0a0f0c;border:1px solid #1a2a1e;border-radius:7px;padding:9px 12px;color:#e0e0e0;font-size:.9rem}
input[type=text]:focus{outline:none;border-color:#22c55e}
</style>
</head>
<body>
<div class="hero">
  <h1>Vlog Builder</h1>
  <p>Upload your clips in order — AI removes silences, stitches them together, adds your intro and outro.</p>
</div>
<div class="container">

  <!-- Title -->
  <div class="section-title">Vlog Title</div>
  <div class="card" style="padding:12px">
    <input type="text" id="vlog-title" placeholder="My Vlog — Beach Trip Aug 2026" value="My Vlog">
  </div>

  <!-- Clips -->
  <div class="section-title">Your Clips — in order</div>
  <div class="card" style="padding:14px">
    <div class="clip-list" id="clip-list"></div>
    <div class="add-zone" id="add-zone" onclick="document.getElementById('clip-input').click()"
         ondragover="event.preventDefault();this.classList.add('drag')"
         ondragleave="this.classList.remove('drag')"
         ondrop="handleClipDrop(event)">
      <div class="icon">＋</div>
      <div class="label">Click to add a clip — or drop video files here</div>
    </div>
    <input type="file" id="clip-input" accept="video/*" multiple onchange="handleClipFiles(this.files)">
  </div>

  <!-- Intro / Outro -->
  <div class="section-title">Intro &amp; Outro (optional)</div>
  <div class="io-row" style="margin-bottom:18px">
    <div class="io-box" id="intro-box" onclick="document.getElementById('intro-input').click()">
      <div class="io-icon">🎬</div>
      <div class="io-label">Add Intro clip</div>
      <div class="io-ok" id="intro-ok" style="display:none"></div>
    </div>
    <div class="io-box" id="outro-box" onclick="document.getElementById('outro-input').click()">
      <div class="io-icon">🎤</div>
      <div class="io-label">Add Outro clip</div>
      <div class="io-ok" id="outro-ok" style="display:none"></div>
    </div>
  </div>
  <input type="file" id="intro-input" accept="video/*" onchange="handleIO('intro',this.files[0])">
  <input type="file" id="outro-input" accept="video/*" onchange="handleIO('outro',this.files[0])">

  <!-- Channel & Info -->
  <div class="section-title">Channel &amp; Info</div>
  <div class="card" style="padding:14px;display:grid;gap:10px">
    <select id="vlog-chan" style="background:#0a0f0c;border:1px solid #1a2a1e;border-radius:7px;padding:9px 12px;color:#e0e0e0;font-size:.9rem">
      <option value="">No channel selected</option>
    </select>
    <input type="text" id="vlog-info" placeholder="Extra info (e.g. Beach trip Aug 2026 — keep upbeat moments)">
  </div>

  <!-- Trip Context -->
  <div class="section-title">Trip Context <span style="color:#555;font-weight:400;font-size:.8em;text-transform:none">(optional — AI uses your GPS, weather &amp; drone data)</span></div>
  <div class="card" id="ctx-card" style="padding:14px">
    <div id="ctx-timestamps" style="font-size:.78rem;color:#555;margin-bottom:10px">Upload clips above to see detected timestamps</div>
    <div style="display:grid;grid-template-columns:1fr auto;gap:10px;align-items:end;margin-bottom:10px">
      <div>
        <label style="font-size:.72rem;color:#555;display:block;margin-bottom:3px">Date (for GPS &amp; weather sync)</label>
        <input type="date" id="ctx-date" style="background:#060c09;border:1px solid #1a2a1e;border-radius:6px;padding:7px 10px;color:#e0e0e0;font-size:.85rem;width:100%">
      </div>
      <button onclick="syncContext()" style="background:#0e2a15;color:#22c55e;border:none;border-radius:7px;padding:9px 16px;cursor:pointer;font-weight:700;font-size:.85rem">🌍 Sync Context</button>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px">
      <label style="display:flex;align-items:center;gap:6px;font-size:.82rem;cursor:pointer">
        <input type="checkbox" id="ctx-enable" style="accent-color:#22c55e"> Use context in AI editing
      </label>
      <button onclick="document.getElementById('ctx-log-input').click()" style="background:#0e1a2a;color:#60a5fa;border:none;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:.78rem">🚁 Import Drone Log CSV</button>
      <input type="file" id="ctx-log-input" accept=".csv" style="display:none" onchange="importDroneLog(this.files[0])">
    </div>
    <div id="ctx-status" style="font-size:.78rem;color:#666"></div>
  </div>

  <!-- Options -->
  <div class="section-title">Options</div>
  <div class="card" style="padding:14px">
    <div class="opts-row">
      <label class="toggle"><input type="checkbox" id="rm-sil" checked> Remove silences from each clip</label>
    </div>
  </div>

  <!-- Build -->
  <button class="build-btn" id="build-btn" onclick="buildVlog()" disabled>🎥 Build My Vlog</button>

  <!-- Progress -->
  <div id="progress-div" style="display:none;margin-top:24px">
    <div class="section-title">Building...</div>
    <div class="card" id="steps-div"></div>
    <div class="elapsed" id="elapsed"></div>
  </div>

  <!-- Result -->
  <div id="result-div" style="display:none;margin-top:24px">
    <div class="section-title">Your Vlog is Ready</div>
    <div class="result-box">
      <div class="result-dur" id="res-dur">—</div>
      <div class="result-sub" id="res-sub"></div>
      <div class="result-btns" id="res-btns">
        <a class="rbtn rbtn-dl" id="res-dl" href="#" download>⬇ My Voice</a>
        <a class="rbtn" id="res-ai" href="#" download style="background:#1a2a3f;color:#60a5fa;display:none">⬇ AI Voice</a>
        <a class="rbtn" id="res-bt" href="#" download style="background:#251540;color:#a78bfa;display:none">⬇ Both Tracks</a>
        <button class="rbtn rbtn-oc" id="res-oc" onclick="openInOpenCut()">🎞 OpenCut</button>
        <a class="rbtn rbtn-sh" id="res-shorts" href="#">✂️ Make a Short</a>
      </div>
    </div>
  </div>

</div>
<script>
const API = '__DIRECTOR_URL__';
let clips = [];      // {id, path, filename, duration, description, thumb_url}
let introPth = '', outroPth = '';
let jobId = '', pollTimer, startTime;
let dragSrc = null;

// Load channels
fetch(API+'/api/channels').then(r=>r.json()).then(chs=>{
  const sel = document.getElementById('vlog-chan');
  chs.forEach(c=>{
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.name;
    sel.appendChild(o);
  });
}).catch(()=>{});

function renderClips() {
  const el = document.getElementById('clip-list');
  if (!clips.length) { el.innerHTML = ''; return; }
  el.innerHTML = clips.map((c,i) => {
    const ts   = c.creation_time ? c.creation_time.slice(0,16).replace('T',' ') : '';
    const loc  = c.location_name ? `📍 ${c.location_name}` : '';
    const cam  = c.camera        ? `📷 ${c.camera}` : '';
    const meta = [ts, loc, cam].filter(Boolean).join(' · ');
    return `
    <div class="clip-item" draggable="true" data-idx="${i}"
         ondragstart="dragSrc=this"
         ondragover="event.preventDefault();this.classList.add('drag-over')"
         ondragleave="this.classList.remove('drag-over')"
         ondrop="dropClip(event,${i})">
      <span class="drag-handle">☰</span>
      ${c.thumb_url
        ? `<img class="clip-thumb" src="${API+c.thumb_url}" onerror="this.style.display='none'">`
        : `<div class="clip-thumb-ph">🎬</div>`}
      <div class="clip-body">
        <div class="clip-top">
          <span class="clip-num">${i+1}</span>
          <span class="clip-name">${esc(c.filename)}</span>
          <span class="clip-dur">${fmtDur(c.duration)}</span>
        </div>
        ${meta ? `<div style="font-size:.7rem;color:#22c55e;margin-bottom:4px;opacity:.8">${meta}</div>` : ''}
        <textarea class="clip-desc" rows="1" placeholder="What's in this clip? (e.g. Arriving at the hotel)${loc?' — context detected: '+c.location_name:''}"
          oninput="clips[${i}].description=this.value">${esc(c.description||'')}</textarea>
      </div>
      <button class="clip-del" onclick="removeClip(${i})">✕</button>
    </div>`;
  }).join('');
  document.getElementById('build-btn').disabled = false;
}

function dropClip(e, toIdx) {
  e.preventDefault();
  document.querySelectorAll('.clip-item').forEach(el=>el.classList.remove('drag-over'));
  const fromIdx = parseInt(dragSrc.dataset.idx);
  if (fromIdx === toIdx) return;
  const moved = clips.splice(fromIdx, 1)[0];
  clips.splice(toIdx, 0, moved);
  renderClips();
}

function removeClip(i) {
  clips.splice(i, 1);
  renderClips();
  if (!clips.length) document.getElementById('build-btn').disabled = true;
}

async function uploadClip(file) {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(API+'/vlog/upload', {method:'POST', body:fd});
  const j = await r.json();
  if (j.path) {
    clips.push({id:j.id, path:j.path, filename:j.filename,
                duration:j.duration, description:'', thumb_url:j.thumb_url||'',
                creation_time:j.creation_time||'', camera:j.camera||'',
                location_name:j.location_name||'', latitude:j.latitude,
                longitude:j.longitude});
    renderClips();
    updateCtxTimestamps();
    // Auto-set context date from first clip timestamp
    if (j.creation_time && !document.getElementById('ctx-date').value) {
      document.getElementById('ctx-date').value = j.creation_time.slice(0,10);
    }
  }
}

function updateCtxTimestamps() {
  const el = document.getElementById('ctx-timestamps');
  const withTs = clips.filter(c=>c.creation_time);
  if (!withTs.length) {
    el.textContent = clips.length ? '⚠️ No timestamps found in these clips — add dates manually' : 'Upload clips above to see detected timestamps';
    return;
  }
  el.innerHTML = '<span style="color:#22c55e">✓ Timestamps detected: </span>' +
    withTs.map(c=>`<span style="margin-right:8px">${c.filename}: <b style="color:#aaa">${c.creation_time.slice(0,16).replace('T',' ')}</b>${c.camera?' ('+c.camera+')':''}</span>`).join('');
}

async function syncContext() {
  const date = document.getElementById('ctx-date').value;
  const st   = document.getElementById('ctx-status');
  if (!date) { st.textContent = '⚠️ Pick a date first'; return; }
  st.textContent = '⏳ Syncing GPS...';
  const ha  = await fetch(API+'/api/context/ha-sync',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({date})}).then(r=>r.json());
  // Try to get lat/lon from first clip with GPS
  const gpsClip = clips.find(c=>c.latitude && c.longitude);
  let wx = {count:0, msg:'no GPS to anchor'};
  if (!gpsClip) {
    // Try using HA events for lat/lon
    const evs = await fetch(API+`/api/context/events?start=${date}T00:00:00+00:00&end=${date}T23:59:59+00:00`).then(r=>r.json());
    const gpsEv = evs.find(e=>e.source==='ha_gps' && e.latitude);
    if (gpsEv) {
      wx = await fetch(API+'/api/context/weather',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({latitude:gpsEv.latitude,longitude:gpsEv.longitude,date})}).then(r=>r.json());
    }
  } else {
    wx = await fetch(API+'/api/context/weather',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({latitude:gpsClip.latitude,longitude:gpsClip.longitude,date})}).then(r=>r.json());
  }
  st.innerHTML = `✓ GPS: ${ha.count} pts · Weather: ${wx.count} hrs — <label style="cursor:pointer"><input type="checkbox" id="ctx-enable" style="accent-color:#22c55e"> Use context in AI editing</label>`;
  if (ha.count > 0) document.getElementById('ctx-enable').checked = true;
}

async function importDroneLog(file) {
  if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const st = document.getElementById('ctx-status');
  st.textContent = '⏳ Importing drone log...';
  const j = await fetch(API+'/api/context/flight-log',{method:'POST',body:fd}).then(r=>r.json());
  st.textContent = j.count > 0 ? `🚁 Drone: ${j.count} flight points imported` : `Drone import: ${j.msg}`;
}

async function handleClipFiles(files) {
  const zone = document.getElementById('add-zone');
  zone.innerHTML = '<div class="icon" style="color:#22c55e">⏳</div><div class="label">Uploading...</div>';
  for (const f of files) await uploadClip(f);
  zone.innerHTML = '<div class="icon">＋</div><div class="label">Click to add more clips — or drop here</div>';
}

function handleClipDrop(e) {
  e.preventDefault();
  document.getElementById('add-zone').classList.remove('drag');
  handleClipFiles(e.dataTransfer.files);
}

async function handleIO(which, file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(API+'/vlog/upload', {method:'POST', body:fd});
  const j = await r.json();
  if (j.path) {
    if (which==='intro') { introPth=j.path; document.getElementById('intro-ok').textContent='✓ '+j.filename; document.getElementById('intro-ok').style.display=''; }
    else { outroPth=j.path; document.getElementById('outro-ok').textContent='✓ '+j.filename; document.getElementById('outro-ok').style.display=''; }
  }
}

async function buildVlog() {
  if (!clips.length) return;
  document.getElementById('build-btn').disabled = true;
  document.getElementById('build-btn').textContent = '⏳ Building...';
  document.getElementById('progress-div').style.display = '';
  document.getElementById('progress-div').scrollIntoView({behavior:'smooth'});

  const chanSel = document.getElementById('vlog-chan');
  const chanId = chanSel.value;
  const chanName = chanId ? chanSel.options[chanSel.selectedIndex].text : '';
  const useCtx = document.getElementById('ctx-enable') && document.getElementById('ctx-enable').checked;
  const body = {
    title: document.getElementById('vlog-title').value.trim() || 'My Vlog',
    clips: clips.map(c=>({path:c.path, filename:c.filename,
                          description:c.description||'',
                          duration:c.duration||0,
                          creation_time:c.creation_time||''})),
    intro_path: introPth, outro_path: outroPth,
    remove_silences: document.getElementById('rm-sil').checked,
    channel_id: chanId, channel_name: chanName,
    info: document.getElementById('vlog-info').value.trim(),
    use_context: useCtx
  };

  const r = await fetch(API+'/api/vlog', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  const j = await r.json();
  if (j.job_id) {
    jobId = j.job_id;
    startTime = Date.now();
    pollTimer = setInterval(poll, 3000);
    poll();
  }
}

async function poll() {
  if (!jobId) return;
  const r = await fetch(API+'/api/vlog/'+jobId);
  const j = await r.json();
  const steps = j.steps || {};
  const div = document.getElementById('steps-div');
  div.innerHTML = Object.entries(steps).map(([k,v])=>{
    let cls = ''; let ico = '○';
    if (v.status==='done'){cls='ok';ico='✓';}
    else if (v.status==='running'){cls='run';ico='⟳';}
    else if (v.status==='error'){cls='err';ico='✗';}
    return `<div class="step-row">
      <div class="step-icon">${ico}</div>
      <div class="step-name">${k.replace(/_/g,' ')}</div>
      <div class="step-st ${cls}">${v.msg||''}</div>
    </div>`;
  }).join('') || '<div style="color:#555;padding:8px">Starting...</div>';

  const secs = Math.floor((Date.now()-startTime)/1000);
  document.getElementById('elapsed').textContent = `${Math.floor(secs/60)}m ${secs%60}s elapsed`;

  if (j.status === 'done') {
    clearInterval(pollTimer);
    showResult(j);
  } else if (j.status === 'error') {
    clearInterval(pollTimer);
    document.getElementById('elapsed').textContent = '❌ ' + (j.error||'error');
  }
}

function showResult(j) {
  document.getElementById('result-div').style.display = '';
  document.getElementById('result-div').scrollIntoView({behavior:'smooth'});
  const dur = j.total_dur || 0;
  document.getElementById('res-dur').textContent = `${Math.floor(dur/60)}m ${dur%60}s`;
  const ch = j.channel_name ? ` · ${j.channel_name}` : '';
  document.getElementById('res-sub').textContent = `${j.clip_count||j.clips?.length||0} clips${ch}`;
  const base = API+'/api/vlog/'+j.job_id;
  const mv = document.getElementById('res-dl');
  mv.href = base+'/download/my-voice'; mv.download = (j.title||'vlog')+'_MyVoice.mp4';
  const av = document.getElementById('res-ai');
  if (j.ai_voice_file) {
    av.href = base+'/download/ai-voice'; av.download = (j.title||'vlog')+'_AIVoice.mp4';
    av.style.display = '';
  }
  const bt = document.getElementById('res-bt');
  if (j.both_tracks_file) {
    bt.href = base+'/download/both-tracks'; bt.download = (j.title||'vlog')+'_BothTracks.mp4';
    bt.style.display = '';
  }
  document.getElementById('res-shorts').href = API+'/shorts';
}

function openInOpenCut() {
  const oc = 'http://'+location.hostname+':9500/ai-bridge.html?vlog='+jobId;
  window.open(oc, '_blank');
}

function fmtDur(s){s=s||0;return`${Math.floor(s/60)}m${(s%60).toString().padStart(2,'0')}s`}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
</script>
</body>
</html>"""


# ── Context Engine API endpoints ─────────────────────────────────────────────

@app.route("/api/context/status")
def api_ctx_status():
    return jsonify(ctx_stats())

@app.route("/api/context/events")
def api_ctx_events():
    ts_start = request.args.get("start", "")
    ts_end   = request.args.get("end",   "")
    if not ts_start or not ts_end:
        return jsonify({"error": "start and end required"}), 400
    return jsonify(ctx_query(ts_start, ts_end))

@app.route("/api/context/ha-sync", methods=["POST"])
def api_ctx_ha_sync():
    data   = request.get_json(force=True) or {}
    date   = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    entity = data.get("entity", "")
    count, msg = sync_ha_gps(date, entity or None)
    return jsonify({"count": count, "msg": msg, "date": date})

@app.route("/api/context/weather", methods=["POST"])
def api_ctx_weather():
    data = request.get_json(force=True) or {}
    lat  = data.get("latitude")
    lon  = data.get("longitude")
    date = data.get("date", datetime.now().strftime("%Y-%m-%d"))
    if lat is None or lon is None:
        return jsonify({"error": "latitude and longitude required"}), 400
    count, msg = fetch_weather(float(lat), float(lon), date)
    return jsonify({"count": count, "msg": msg, "date": date})

@app.route("/api/context/flight-log", methods=["POST"])
def api_ctx_flight_log():
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f   = request.files["file"]
    tmp = Path("/tmp") / f"autel_{gen_id()}.csv"
    f.save(str(tmp))
    count, msg = parse_autel_log(str(tmp))
    try: tmp.unlink()
    except: pass
    return jsonify({"count": count, "msg": msg})

@app.route("/api/context/match", methods=["POST"])
def api_ctx_match():
    data = request.get_json(force=True) or {}
    ct   = data.get("creation_time", "")
    dur  = float(data.get("duration", 60))
    evs  = match_clip_to_context(ct, dur)
    return jsonify({"events": evs, "summary": format_context_for_ai(evs)})

@app.route("/api/context/ha-config")
def api_ctx_ha_config():
    return jsonify({
        "ha_url":     HA_URL,
        "ha_tracker": HA_TRACKER,
        "ha_token_set": bool(HA_TOKEN)
    })

_CONTEXT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trip Context — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080a0e;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif}
header{background:#0d1018;border-bottom:1px solid #1a2030;padding:16px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.1rem;font-weight:700;color:#60a5fa}
header .stats{margin-left:auto;font-size:.78rem;color:#555}
.container{max-width:900px;margin:0 auto;padding:24px}
.section-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.12em;color:#60a5fa;margin-bottom:10px}
.card{background:#0d1117;border:1px solid #1a2030;border-radius:10px;padding:16px;margin-bottom:16px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:600px){.row{grid-template-columns:1fr}}
label{display:block;font-size:.78rem;color:#666;margin-bottom:4px}
input,select{width:100%;background:#0a0f16;border:1px solid #1a2030;border-radius:6px;padding:8px 12px;color:#e0e0e0;font-size:.88rem}
input:focus,select:focus{outline:none;border-color:#60a5fa}
.btn{background:#1a3a5f;color:#60a5fa;border:none;border-radius:7px;padding:9px 18px;cursor:pointer;font-size:.88rem;font-weight:700;transition:background .15s}
.btn:hover{background:#1e4a7a}
.btn-grn{background:#0e2a15;color:#22c55e}.btn-grn:hover{background:#143a1f}
.btn-pur{background:#1a0535;color:#a78bfa}.btn-pur:hover{background:#220a45}
.btn-ora{background:#2a1a00;color:#fb923c}.btn-ora:hover{background:#3a2400}
.msg{font-size:.8rem;padding:6px 10px;border-radius:5px;margin-top:8px}
.msg.ok{background:#0e2a15;color:#22c55e}
.msg.err{background:#1a0808;color:#f87171}
.msg.info{background:#0d1117;color:#888}
.timeline-row{display:flex;align-items:flex-start;gap:12px;padding:8px 0;border-bottom:1px solid #111820;font-size:.8rem}
.timeline-row:last-child{border-bottom:none}
.tl-time{color:#555;flex-shrink:0;width:70px;font-family:monospace;font-size:.75rem}
.tl-src{flex-shrink:0;width:70px;font-size:.72rem;padding:2px 6px;border-radius:4px;text-align:center;font-weight:700}
.src-ha_gps{background:#0d2030;color:#60a5fa}
.src-weather{background:#1a1500;color:#fbbf24}
.src-autel{background:#1a0535;color:#a78bfa}
.tl-body{flex:1;min-width:0;color:#aaa}
.tl-loc{color:#888;font-size:.72rem;margin-top:2px}
.pag{display:flex;gap:8px;justify-content:center;margin-top:12px}
.pag-btn{padding:5px 14px;background:#0d1117;border:1px solid #1a2030;border-radius:5px;cursor:pointer;font-size:.8rem;color:#aaa}
.pag-btn:hover{border-color:#60a5fa;color:#60a5fa}
</style>
</head>
<body>
<header>
  <span>🌍</span>
  <h1>Trip Context Engine</h1>
  <div class="stats" id="stats">Loading...</div>
</header>
<div class="container">

  <!-- HA GPS Sync -->
  <div class="section-title">Pixel 9 GPS — Home Assistant Sync</div>
  <div class="card">
    <div class="row">
      <div>
        <label>Date</label>
        <input type="date" id="ha-date">
      </div>
      <div>
        <label>HA Entity</label>
        <input type="text" id="ha-entity" placeholder="auto from .env">
      </div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn btn-grn" onclick="syncHA()">📍 Sync GPS</button>
      <span id="ha-msg" class="msg info" style="display:none"></span>
    </div>
    <div id="ha-config" style="margin-top:8px;font-size:.75rem;color:#555"></div>
  </div>

  <!-- Weather -->
  <div class="section-title">Weather (Open-Meteo — free)</div>
  <div class="card">
    <div class="row" style="grid-template-columns:1fr 1fr 1fr auto">
      <div><label>Date</label><input type="date" id="wx-date"></div>
      <div><label>Latitude</label><input type="number" step="0.0001" id="wx-lat" placeholder="51.5074"></div>
      <div><label>Longitude</label><input type="number" step="0.0001" id="wx-lon" placeholder="-0.1278"></div>
    </div>
    <div style="margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <button class="btn" onclick="fetchWeather()">🌤 Fetch Weather</button>
      <span class="msg info" style="font-size:.72rem">Historical data available up to ~5 days ago</span>
      <span id="wx-msg" class="msg info" style="display:none"></span>
    </div>
  </div>

  <!-- Drone Log -->
  <div class="section-title">Drone Flight Log (Autel CSV)</div>
  <div class="card" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
    <button class="btn btn-pur" onclick="document.getElementById('log-input').click()">🚁 Import Flight Log CSV</button>
    <input type="file" id="log-input" accept=".csv,.txt" style="display:none" onchange="importLog(this.files[0])">
    <span id="log-msg" class="msg info" style="display:none"></span>
  </div>

  <!-- Timeline -->
  <div class="section-title" style="margin-top:8px">Context Timeline</div>
  <div class="card" style="padding:12px">
    <div style="display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap">
      <div style="flex:1"><label>Start</label><input type="datetime-local" id="tl-start"></div>
      <div style="flex:1"><label>End</label><input type="datetime-local" id="tl-end"></div>
      <button class="btn" onclick="loadTimeline()" style="align-self:flex-end">🔍 Query</button>
    </div>
    <div id="timeline-list"><div style="color:#444;text-align:center;padding:24px">Enter a date range above to view events</div></div>
    <div class="pag" id="pag"></div>
  </div>

</div>
<script>
const API = '__DIRECTOR_URL__';
let tlPage = 0, tlEvents = [];

// Set default dates to today
const today = new Date().toISOString().slice(0,10);
document.getElementById('ha-date').value = today;
document.getElementById('wx-date').value = today;

// Load HA config
fetch(API+'/api/context/ha-config').then(r=>r.json()).then(c=>{
  const el = document.getElementById('ha-config');
  el.textContent = `HA: ${c.ha_url} · Entity: ${c.ha_tracker} · Token: ${c.ha_token_set ? '✓ set' : '✗ missing — add HA_TOKEN to .env'}`;
  document.getElementById('ha-entity').placeholder = c.ha_tracker;
});

// Load stats
function loadStats() {
  fetch(API+'/api/context/status').then(r=>r.json()).then(s=>{
    const parts = Object.entries(s.by_source||{}).map(([k,n])=>`${k}: ${n}`).join(' · ');
    document.getElementById('stats').textContent = `${s.total} events${parts?' ('+parts+')':''}`;
  });
}
loadStats();

async function syncHA() {
  const date   = document.getElementById('ha-date').value;
  const entity = document.getElementById('ha-entity').value.trim();
  const msg    = document.getElementById('ha-msg');
  msg.className='msg info'; msg.textContent='Syncing...'; msg.style.display='';
  const r = await fetch(API+'/api/context/ha-sync',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date, entity: entity||undefined})});
  const j = await r.json();
  msg.className = j.count > 0 ? 'msg ok' : (j.msg==='ok'?'msg info':'msg err');
  msg.textContent = j.count > 0 ? `✓ ${j.count} GPS points synced for ${date}` : `${j.msg}`;
  loadStats();
}

async function fetchWeather() {
  const date = document.getElementById('wx-date').value;
  const lat  = document.getElementById('wx-lat').value;
  const lon  = document.getElementById('wx-lon').value;
  const msg  = document.getElementById('wx-msg');
  if (!lat || !lon) { msg.className='msg err'; msg.textContent='Enter lat/lon'; msg.style.display=''; return; }
  msg.className='msg info'; msg.textContent='Fetching...'; msg.style.display='';
  const r = await fetch(API+'/api/context/weather',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({latitude:parseFloat(lat),longitude:parseFloat(lon),date})});
  const j = await r.json();
  msg.className = j.count > 0 ? 'msg ok' : 'msg err';
  msg.textContent = j.count > 0 ? `✓ ${j.count} hourly records for ${date}` : j.msg;
  loadStats();
}

async function importLog(file) {
  if (!file) return;
  const msg = document.getElementById('log-msg');
  msg.className='msg info'; msg.textContent='Importing...'; msg.style.display='';
  const fd = new FormData(); fd.append('file', file);
  const r = await fetch(API+'/api/context/flight-log',{method:'POST',body:fd});
  const j = await r.json();
  msg.className = j.count > 0 ? 'msg ok' : 'msg err';
  msg.textContent = j.count > 0 ? `✓ ${j.count} flight points imported` : `Import failed: ${j.msg}`;
  loadStats();
}

async function loadTimeline() {
  const start = document.getElementById('tl-start').value;
  const end   = document.getElementById('tl-end').value;
  if (!start || !end) return;
  const r = await fetch(API+`/api/context/events?start=${encodeURIComponent(start+':00+00:00')}&end=${encodeURIComponent(end+':00+00:00')}`);
  tlEvents = await r.json();
  tlPage = 0;
  renderTimeline();
}

function renderTimeline() {
  const el = document.getElementById('timeline-list');
  const pg = document.getElementById('pag');
  const PAGE = 50;
  const slice = tlEvents.slice(tlPage*PAGE, (tlPage+1)*PAGE);
  if (!tlEvents.length) { el.innerHTML='<div style="color:#444;text-align:center;padding:16px">No events in this range</div>'; pg.innerHTML=''; return; }
  el.innerHTML = slice.map(e=>{
    const time = e.ts_utc ? new Date(e.ts_utc).toLocaleTimeString() : '';
    const meta = typeof e.metadata==='string' ? JSON.parse(e.metadata||'{}') : (e.metadata||{});
    let body = '';
    if (e.source==='ha_gps')  body = `${e.location_name||''} · ${e.latitude?.toFixed(4)},${e.longitude?.toFixed(4)}`;
    if (e.source==='weather') body = `${meta.description||''} · ${meta.temp_c?.toFixed(0)||'?'}°C · ${meta.wind_kmh?.toFixed(0)||'?'} km/h wind`;
    if (e.source==='autel')   body = `${e.latitude?.toFixed(4)},${e.longitude?.toFixed(4)} · Alt: ${e.altitude?.toFixed(0)||'?'}m`;
    return `<div class="timeline-row">
      <div class="tl-time">${time}</div>
      <div class="tl-src src-${e.source}">${e.source}</div>
      <div class="tl-body">${body}<div class="tl-loc">${e.device||''}</div></div>
    </div>`;
  }).join('');
  const totalPages = Math.ceil(tlEvents.length / PAGE);
  pg.innerHTML = totalPages > 1 ? `
    <span class="pag-btn" onclick="tlPage=Math.max(0,tlPage-1);renderTimeline()">← Prev</span>
    <span style="font-size:.8rem;color:#555;align-self:center">${tlPage+1}/${totalPages} (${tlEvents.length} events)</span>
    <span class="pag-btn" onclick="tlPage=Math.min(${totalPages-1},tlPage+1);renderTimeline()">Next →</span>` : '';
}
</script>
</body>
</html>"""

@app.route("/context")
def context_page():
    html = _CONTEXT_HTML.replace("__DIRECTOR_URL__",
                                  f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}")
    return Response(html, mimetype="text/html")


# ── Settings API ──────────────────────────────────────────────────────────────

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    s = load_settings()
    # Don't send token in plain — just indicate if it's set
    out = dict(s)
    if out.get("ha_token"):
        out["ha_token_set"] = True
        out["ha_token"]     = ""   # blank it in response
    else:
        out["ha_token_set"] = False
    return jsonify(out)

@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    data = request.get_json(force=True) or {}
    # Don't overwrite token if blank (means "don't change")
    if not data.get("ha_token"):
        data.pop("ha_token", None)
    msg = save_settings(data)
    return jsonify({"status": msg})

@app.route("/api/settings/test-ha", methods=["POST"])
def api_settings_test_ha():
    """Test HA connection with provided or stored credentials."""
    import requests as req
    data  = request.get_json(force=True) or {}
    url   = data.get("ha_url")   or load_settings().get("ha_url",   HA_URL)
    token = data.get("ha_token") or load_settings().get("ha_token", HA_TOKEN)
    if not token:
        return jsonify({"ok": False, "msg": "No HA token — add it in settings"})
    try:
        r = req.get(f"{url}/api/", headers={"Authorization": f"Bearer {token}"}, timeout=5)
        if r.status_code == 200:
            j = r.json()
            return jsonify({"ok": True, "msg": f"Connected ✓ · HA {j.get('version','')}"})
        return jsonify({"ok": False, "msg": f"HTTP {r.status_code}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/api/settings/ha-entities", methods=["GET"])
def api_settings_ha_entities():
    """List device_tracker entities from HA."""
    import requests as req
    cfg   = load_settings()
    url   = cfg.get("ha_url",   HA_URL)
    token = cfg.get("ha_token", HA_TOKEN)
    if not token:
        return jsonify([])
    try:
        r = req.get(f"{url}/api/states", headers={"Authorization": f"Bearer {token}"}, timeout=8)
        r.raise_for_status()
        trackers = [{"entity_id": s["entity_id"],
                     "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                     "state": s.get("state", "")}
                    for s in r.json()
                    if s["entity_id"].startswith("device_tracker.")]
        return jsonify(trackers)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_SETTINGS_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Settings — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080a0e;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
header{background:#0d1018;border-bottom:1px solid #1a2030;padding:14px 24px;display:flex;align-items:center;gap:14px}
header h1{font-size:1.05rem;font-weight:700;color:#fff}
header a{color:#555;font-size:.82rem;text-decoration:none;margin-left:auto}header a:hover{color:#60a5fa}
.tabs{display:flex;gap:0;border-bottom:1px solid #1a2030;background:#0a0d14;padding:0 24px}
.tab{padding:12px 18px;font-size:.82rem;font-weight:600;cursor:pointer;border-bottom:2px solid transparent;color:#555;transition:all .15s}
.tab.active{color:#fff;border-color:#60a5fa}
.tab:hover:not(.active){color:#aaa}
.pane{display:none;max-width:760px;margin:0 auto;padding:24px}
.pane.active{display:block}
.section{margin-bottom:24px}
.section-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#60a5fa;margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid #131a26}
.field{margin-bottom:14px}
label{display:block;font-size:.78rem;color:#888;margin-bottom:4px}
input[type=text],input[type=url],input[type=number],input[type=password],select,textarea{
  width:100%;background:#0a0f16;border:1px solid #1a2030;border-radius:7px;
  padding:9px 13px;color:#e0e0e0;font-size:.88rem;transition:border-color .15s}
input:focus,select:focus,textarea:focus{outline:none;border-color:#60a5fa}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:540px){.row2{grid-template-columns:1fr}}
.btn{background:#1a3a5f;color:#60a5fa;border:none;border-radius:7px;padding:9px 18px;
     cursor:pointer;font-size:.85rem;font-weight:700;transition:background .15s}
.btn:hover{background:#204a7a}.btn-grn{background:#0e2a15;color:#22c55e}.btn-grn:hover{background:#143a1f}
.btn-red{background:#2a0808;color:#f87171}.btn-red:hover{background:#3a0e0e}
.btn-save{width:100%;padding:12px;font-size:.95rem;margin-top:16px;background:#1a3a5f;color:#60a5fa}
.msg{font-size:.8rem;padding:6px 10px;border-radius:5px;display:inline-block;margin-left:10px}
.msg.ok{background:#0e2a15;color:#22c55e}.msg.err{background:#1a0808;color:#f87171}
.msg.info{background:#0d1117;color:#888}
.cam-card{background:#0d1117;border:1px solid #1a2030;border-radius:10px;padding:16px;margin-bottom:12px}
.cam-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.cam-icon{font-size:1.6rem}
.cam-name{font-weight:700;font-size:.95rem}
.cam-type{font-size:.72rem;color:#555;background:#111;padding:2px 8px;border-radius:4px;margin-left:auto}
.toggle-row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #0e1520}
.toggle-row:last-child{border-bottom:none}
.toggle-label{flex:1;font-size:.85rem}
.toggle-desc{font-size:.72rem;color:#555;margin-top:1px}
input[type=checkbox]{width:16px;height:16px;accent-color:#60a5fa}
.badge{display:inline-block;font-size:.65rem;padding:2px 7px;border-radius:3px;font-weight:700;margin-left:6px;vertical-align:middle}
.badge-grn{background:#0e2a15;color:#22c55e}.badge-red{background:#1a0808;color:#f87171}
.entity-list{max-height:160px;overflow-y:auto;background:#0a0f16;border:1px solid #1a2030;border-radius:6px;margin-top:8px}
.entity-row{padding:7px 12px;font-size:.82rem;cursor:pointer;border-bottom:1px solid #111820;display:flex;align-items:center;gap:8px}
.entity-row:last-child{border-bottom:none}
.entity-row:hover{background:#0f1a28;color:#60a5fa}
.entity-state{font-size:.7rem;color:#555}
</style>
</head>
<body>
<header>
  <span>⚙️</span>
  <h1>Mini Studio — Settings</h1>
  <a href="__DIRECTOR_URL__">← Back to Director</a>
</header>

<div class="tabs">
  <div class="tab active" onclick="switchTab('ha')">📍 Home Assistant</div>
  <div class="tab" onclick="switchTab('devices')">📷 My Devices</div>
  <div class="tab" onclick="switchTab('context')">🌍 Context Engine</div>
  <div class="tab" onclick="switchTab('tts')">🎙 TTS &amp; Voice</div>
</div>

<!-- ── Home Assistant ── -->
<div class="pane active" id="pane-ha">
  <div class="section">
    <div class="section-title">Home Assistant Connection</div>
    <div class="field row2">
      <div>
        <label>HA URL</label>
        <input type="url" id="ha-url" placeholder="http://192.168.0.164:8123">
      </div>
      <div>
        <label>Long-lived Access Token <a href="#" onclick="showTokenHelp()" style="color:#60a5fa;font-size:.7rem;margin-left:6px">How to get?</a></label>
        <input type="password" id="ha-token" placeholder="Paste token here (leave blank to keep existing)">
      </div>
    </div>
    <div id="token-help" style="display:none;background:#0d1117;border:1px solid #1a2030;border-radius:7px;padding:12px;margin-bottom:12px;font-size:.8rem;color:#888;line-height:1.6">
      In Home Assistant: <b style="color:#e0e0e0">Profile</b> (bottom-left) → scroll to
      <b style="color:#e0e0e0">Security</b> → <b style="color:#e0e0e0">Long-lived access tokens</b>
      → <b style="color:#e0e0e0">Create token</b> → copy and paste here.
    </div>
    <div id="ha-token-status" style="font-size:.78rem;color:#555;margin-bottom:10px"></div>
    <button class="btn btn-grn" onclick="testHA()">🔗 Test Connection</button>
    <span id="ha-test-msg"></span>
  </div>

  <div class="section">
    <div class="section-title">GPS Tracker Entity</div>
    <div class="field">
      <label>device_tracker entity for your Pixel 9</label>
      <input type="text" id="ha-tracker" placeholder="device_tracker.sam_pixel_9">
    </div>
    <button class="btn" onclick="loadEntities()">🔍 Browse HA entities</button>
    <div id="entity-list" style="display:none" class="entity-list"></div>
  </div>

  <button class="btn btn-save btn-grn" onclick="saveHA()">💾 Save HA Settings</button>
  <div id="ha-save-msg" style="margin-top:8px;font-size:.8rem"></div>
</div>

<!-- ── My Devices ── -->
<div class="pane" id="pane-devices">
  <div class="section">
    <div class="section-title">Your Camera Devices</div>
    <p style="font-size:.8rem;color:#666;margin-bottom:14px">
      These profiles help the system understand your clips — which device filmed them,
      how to read the timestamp, and whether to use GPS or HA for location.
    </p>

    <!-- Pixel 9 -->
    <div class="cam-card">
      <div class="cam-header">
        <div class="cam-icon">📱</div>
        <div>
          <div class="cam-name">Google Pixel 9</div>
          <div style="font-size:.75rem;color:#666">Phone camera + main GPS device</div>
        </div>
        <div class="cam-type">PHONE</div>
      </div>
      <div class="row2">
        <div class="field">
          <label>HA Tracker entity (GPS source)</label>
          <input type="text" id="cam-pixel-tracker" placeholder="device_tracker.sam_pixel_9">
        </div>
        <div class="field">
          <label>Local timezone offset (hours from UTC)</label>
          <input type="number" id="cam-pixel-tz" value="0" min="-12" max="14" step="1" style="width:100px">
          <div style="font-size:.7rem;color:#555;margin-top:4px">e.g. 1 = BST (UK summer), 0 = GMT (UK winter)</div>
        </div>
      </div>
    </div>

    <!-- Feiyu Pocket 2S -->
    <div class="cam-card">
      <div class="cam-header">
        <div class="cam-icon">🎥</div>
        <div>
          <div class="cam-name">Feiyu Pocket 2S</div>
          <div style="font-size:.75rem;color:#666">4K wearable action cam — no built-in GPS</div>
        </div>
        <div class="cam-type">ACTION CAM</div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Timestamp timezone offset (hours from UTC)</label>
          <input type="number" id="cam-feiyu-tz" value="0" min="-12" max="14" step="1">
          <div style="font-size:.7rem;color:#555;margin-top:4px">
            Set this to your local offset so clip times match HA GPS.
            e.g. filming at 23:01 BST = 22:01 UTC → set to 1
          </div>
        </div>
        <div class="field">
          <label>Filename prefix pattern</label>
          <input type="text" id="cam-feiyu-prefix" value="FVIDEO,Video,FPV" placeholder="FVIDEO,Video,FPV">
          <div style="font-size:.7rem;color:#555;margin-top:4px">Comma-separated prefixes to recognise Feiyu clips</div>
        </div>
      </div>
      <div class="field">
        <label>Notes</label>
        <input type="text" id="cam-feiyu-notes" value="4K wearable — timestamp from file metadata or filename" style="color:#666">
      </div>
    </div>

    <!-- Autel Nano -->
    <div class="cam-card">
      <div class="cam-header">
        <div class="cam-icon">🚁</div>
        <div>
          <div class="cam-name">Autel EVO Nano</div>
          <div style="font-size:.75rem;color:#666">Drone — GPS in flight log CSV</div>
        </div>
        <div class="cam-type">DRONE</div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Timezone offset for flight logs</label>
          <input type="number" id="cam-autel-tz" value="0" min="-12" max="14" step="1">
        </div>
        <div class="field">
          <label>Notes</label>
          <input type="text" id="cam-autel-notes" value="Drone — use flight log CSV for GPS telemetry">
        </div>
      </div>
      <div style="font-size:.78rem;color:#555;background:#0a0d10;border:1px solid #111820;border-radius:6px;padding:10px;margin-top:4px">
        📁 To get Autel flight logs: open the <b>Autel Sky</b> app → My Flights → select flight → export CSV.
        Then import via the <a href="__DIRECTOR_URL__/context" style="color:#a78bfa">Context page</a>.
      </div>
    </div>

    <div class="field">
      <label>Global timezone offset (hours from UTC) — used when clip-specific offset not set</label>
      <input type="number" id="global-tz" value="0" min="-12" max="14" step="1" style="width:120px">
    </div>

    <button class="btn btn-save btn-grn" onclick="saveDevices()">💾 Save Device Settings</button>
    <div id="dev-save-msg" style="margin-top:8px;font-size:.8rem"></div>
  </div>
</div>

<!-- ── Context Engine ── -->
<div class="pane" id="pane-context">
  <div class="section">
    <div class="section-title">Auto-Context on Upload</div>
    <div class="toggle-row">
      <div class="toggle-label">
        <div>📍 Auto-locate clip on upload</div>
        <div class="toggle-desc">When you upload a clip, instantly look up where your Pixel 9 was at that time via HA GPS</div>
      </div>
      <input type="checkbox" id="ctx-auto-locate" checked>
    </div>
    <div class="toggle-row">
      <div class="toggle-label">
        <div>🌍 Auto-sync GPS when building vlog</div>
        <div class="toggle-desc">Automatically pull HA GPS history for the vlog's date range</div>
      </div>
      <input type="checkbox" id="ctx-auto-sync" checked>
    </div>
    <div class="toggle-row">
      <div class="toggle-label">
        <div>🌤 Auto-fetch weather</div>
        <div class="toggle-desc">Automatically pull Open-Meteo weather for the date and location</div>
      </div>
      <input type="checkbox" id="ctx-auto-weather" checked>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Context Database</div>
    <div id="ctx-stats" style="font-size:.82rem;color:#888;margin-bottom:12px">Loading...</div>
    <div style="display:flex;gap:10px">
      <a class="btn" href="__DIRECTOR_URL__/context" style="text-decoration:none">🌍 Open Context Timeline</a>
    </div>
  </div>

  <button class="btn btn-save btn-grn" onclick="saveContext()">💾 Save Context Settings</button>
  <div id="ctx-save-msg" style="margin-top:8px;font-size:.8rem"></div>
</div>

<!-- ── TTS & Voice ── -->
<div class="pane" id="pane-tts">
  <div class="section">
    <div class="section-title">Pocket TTS (Voice Clone)</div>
    <div class="field row2">
      <div>
        <label>Pocket TTS URL</label>
        <input type="url" id="tts-url" placeholder="http://127.0.0.1:5020">
      </div>
      <div>
        <label>Voice WAV path (on this server)</label>
        <input type="text" id="voice-wav" placeholder="/opt/studio/voices/voice_clone.wav">
      </div>
    </div>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:4px">
      <button class="btn" onclick="testTTS()">🎙 Test Voice Clone</button>
      <span id="tts-test-msg" class="msg info" style="display:none"></span>
    </div>
    <audio id="tts-preview" controls style="display:none;margin-top:12px;width:100%"></audio>
  </div>

  <button class="btn btn-save btn-grn" onclick="saveTTS()">💾 Save TTS Settings</button>
  <div id="tts-save-msg" style="margin-top:8px;font-size:.8rem"></div>
</div>

<script>
const API = '__DIRECTOR_URL__';
let settings = {};

// Load settings on page load
fetch(API+'/api/settings').then(r=>r.json()).then(s=>{
  settings = s;
  populate(s);
  loadCtxStats();
});

function populate(s) {
  // HA
  setVal('ha-url',     s.ha_url     || '');
  setVal('ha-tracker', s.ha_tracker || '');
  const ts = document.getElementById('ha-token-status');
  ts.innerHTML = s.ha_token_set
    ? '<span class="badge badge-grn">Token set ✓</span> Leave blank to keep existing token'
    : '<span class="badge badge-red">No token</span> Paste a long-lived access token above';

  // Devices
  const cams = s.cameras || [];
  const pixel = cams.find(c=>c.id==='pixel9') || {};
  const feiyu = cams.find(c=>c.id==='feiyu')  || {};
  const autel = cams.find(c=>c.id==='autel')  || {};
  setVal('cam-pixel-tracker',  pixel.notes?.match(/device_tracker\.\S+/)?.[0] || s.ha_tracker || '');
  setVal('cam-pixel-tz',       pixel.tz_offset ?? 0);
  setVal('cam-feiyu-tz',       feiyu.tz_offset ?? 0);
  setVal('cam-feiyu-prefix',   feiyu.filename_hint || 'FVIDEO,Video,FPV');
  setVal('cam-feiyu-notes',    feiyu.notes || '');
  setVal('cam-autel-tz',       autel.tz_offset ?? 0);
  setVal('cam-autel-notes',    autel.notes || '');
  setVal('global-tz',          s.timezone_offset ?? 0);

  // Context
  setChk('ctx-auto-locate',  s.context_auto_locate  !== false);
  setChk('ctx-auto-sync',    s.context_auto_sync    !== false);
  setChk('ctx-auto-weather', s.context_auto_weather !== false);

  // TTS
  setVal('tts-url',   s.pocket_tts_url || '');
  setVal('voice-wav', s.voice_wav       || '');
}

function setVal(id, v) { const el=document.getElementById(id); if(el) el.value=v; }
function setChk(id, v) { const el=document.getElementById(id); if(el) el.checked=!!v; }
function getVal(id)    { const el=document.getElementById(id); return el?el.value:''; }
function getChk(id)    { const el=document.getElementById(id); return el?el.checked:false; }

function switchTab(name) {
  document.querySelectorAll('.tab').forEach((t,i)=>{
    const panes=['ha','devices','context','tts'];
    t.classList.toggle('active', panes[i]===name);
  });
  document.querySelectorAll('.pane').forEach(p=>{
    p.classList.toggle('active', p.id==='pane-'+name);
  });
}

function showTokenHelp() {
  const el=document.getElementById('token-help');
  el.style.display = el.style.display==='none' ? '' : 'none';
}

async function testHA() {
  const msg = document.getElementById('ha-test-msg');
  msg.className='msg info'; msg.textContent='Testing...'; msg.style.display='';
  msg.style.display='inline-block';
  const r = await fetch(API+'/api/settings/test-ha',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ha_url:getVal('ha-url'), ha_token:getVal('ha-token')||undefined})});
  const j = await r.json();
  msg.className = j.ok ? 'msg ok' : 'msg err';
  msg.textContent = j.msg;
}

async function loadEntities() {
  const el = document.getElementById('entity-list');
  el.style.display = '';
  el.innerHTML = '<div class="entity-row" style="color:#555">Loading...</div>';
  const r = await fetch(API+'/api/settings/ha-entities');
  const j = await r.json();
  if (j.error) { el.innerHTML=`<div class="entity-row" style="color:#f87171">${j.error}</div>`; return; }
  if (!j.length) { el.innerHTML='<div class="entity-row" style="color:#555">No device_tracker entities found — check connection</div>'; return; }
  el.innerHTML = j.map(e=>`
    <div class="entity-row" onclick="document.getElementById('ha-tracker').value='${e.entity_id}'">
      <span>${e.entity_id}</span>
      <span style="color:#888;font-size:.8rem">${e.name}</span>
      <span class="entity-state">${e.state}</span>
    </div>`).join('');
}

async function saveHA() {
  const tok = getVal('ha-token');
  const body = {ha_url: getVal('ha-url'), ha_tracker: getVal('ha-tracker')};
  if (tok) body.ha_token = tok;
  const r = await fetch(API+'/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j = await r.json();
  const msg = document.getElementById('ha-save-msg');
  msg.className = j.status==='ok' ? 'msg ok' : 'msg err';
  msg.textContent = j.status==='ok' ? '✓ HA settings saved' : j.status;
  msg.style.display='inline-block';
}

async function saveDevices() {
  const cameras = [
    {id:'pixel9', name:'Pixel 9',        type:'phone',      tz_offset:parseInt(getVal('cam-pixel-tz')||0),  is_gps_device:true},
    {id:'feiyu',  name:'Feiyu Pocket 2S',type:'action_cam', tz_offset:parseInt(getVal('cam-feiyu-tz')||0), filename_hint:getVal('cam-feiyu-prefix'), notes:getVal('cam-feiyu-notes')},
    {id:'autel',  name:'Autel EVO Nano', type:'drone',      tz_offset:parseInt(getVal('cam-autel-tz')||0), notes:getVal('cam-autel-notes')},
  ];
  const r = await fetch(API+'/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({cameras, timezone_offset:parseInt(getVal('global-tz')||0),
                         ha_tracker:getVal('cam-pixel-tracker')})});
  const j = await r.json();
  const msg = document.getElementById('dev-save-msg');
  msg.className = j.status==='ok' ? 'msg ok' : 'msg err';
  msg.textContent = j.status==='ok' ? '✓ Device settings saved' : j.status;
  msg.style.display='inline-block';
}

async function saveContext() {
  const r = await fetch(API+'/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      context_auto_locate:  getChk('ctx-auto-locate'),
      context_auto_sync:    getChk('ctx-auto-sync'),
      context_auto_weather: getChk('ctx-auto-weather'),
    })});
  const j = await r.json();
  const msg = document.getElementById('ctx-save-msg');
  msg.className = j.status==='ok' ? 'msg ok' : 'msg err';
  msg.textContent = j.status==='ok' ? '✓ Context settings saved' : j.status;
  msg.style.display='inline-block';
}

async function saveTTS() {
  const r = await fetch(API+'/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pocket_tts_url:getVal('tts-url'), voice_wav:getVal('voice-wav')})});
  const j = await r.json();
  const msg = document.getElementById('tts-save-msg');
  msg.className = j.status==='ok' ? 'msg ok' : 'msg err';
  msg.textContent = j.status==='ok' ? '✓ TTS settings saved' : j.status;
  msg.style.display='inline-block';
}

async function testTTS() {
  const msg  = document.getElementById('tts-test-msg');
  const aud  = document.getElementById('tts-preview');
  msg.className='msg info'; msg.textContent='Generating...'; msg.style.display='inline-block';
  const r = await fetch(API+'/tts-preview',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:"Hello, this is my voice clone speaking. Ready to narrate your vlog."})});
  if (r.ok) {
    const blob = await r.blob();
    aud.src = URL.createObjectURL(blob);
    aud.style.display='';
    aud.play();
    msg.className='msg ok'; msg.textContent='✓ Voice clone working';
  } else {
    msg.className='msg err'; msg.textContent='TTS failed — check Pocket TTS URL';
  }
}

function loadCtxStats() {
  fetch(API+'/api/context/status').then(r=>r.json()).then(s=>{
    const el = document.getElementById('ctx-stats');
    if (!s.total) { el.textContent='No context events yet — sync GPS and weather from the Context page'; return; }
    const parts = Object.entries(s.by_source||{}).map(([k,n])=>`${k}: ${n}`).join(' · ');
    el.innerHTML = `<span class="badge badge-grn">${s.total} events</span> ${parts}`;
  });
}
</script>
</body>
</html>"""

@app.route("/settings")
def settings_page():
    html = _SETTINGS_HTML.replace("__DIRECTOR_URL__",
                                   f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}")
    return Response(html, mimetype="text/html")


# ── List & chat endpoints ────────────────────────────────────────────────────

@app.route("/api/vlogs")
def api_vlogs_list():
    with _vlog_lock:
        jobs = list(_vlog_jobs.values())
    base = f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}"
    out = []
    for j in sorted(jobs, key=lambda x: x.get("created_at",""), reverse=True):
        vjid = j["job_id"]
        row = {
            "job_id":       vjid,
            "title":        j.get("title",""),
            "status":       j.get("status",""),
            "created_at":   j.get("created_at",""),
            "done_at":      j.get("done_at",""),
            "total_dur":    j.get("total_dur",0),
            "clip_count":   j.get("clip_count", len(j.get("clips",[]))),
            "channel_name": j.get("channel_name",""),
            "info":         j.get("info",""),
            "error":        j.get("error",""),
        }
        if j.get("my_voice_file"):
            row["dl_my_voice"]    = f"{base}/api/vlog/{vjid}/download/my-voice"
        if j.get("ai_voice_file"):
            row["dl_ai_voice"]    = f"{base}/api/vlog/{vjid}/download/ai-voice"
        if j.get("both_tracks_file"):
            row["dl_both_tracks"] = f"{base}/api/vlog/{vjid}/download/both-tracks"
        out.append(row)
    return jsonify(out)


@app.route("/api/shorts/all")
def api_shorts_list():
    with _shorts_lock:
        jobs = list(_shorts_jobs.values())
    base = f"http://{DIRECTOR_HOST}:{DIRECTOR_PORT}"
    out = []
    for j in sorted(jobs, key=lambda x: x.get("created_at",""), reverse=True):
        sjid = j["job_id"]
        row = {
            "job_id":       sjid,
            "source_name":  j.get("source_name",""),
            "status":       j.get("status",""),
            "created_at":   j.get("created_at",""),
            "done_at":      j.get("done_at",""),
            "style":        j.get("style",""),
            "duration":     j.get("duration",0),
            "total_dur":    j.get("total_dur",0),
            "channel_name": j.get("channel_name",""),
            "info":         j.get("info",""),
            "error":        j.get("error",""),
        }
        if j.get("reel_path"):
            row["dl_reel"]   = f"{base}/api/shorts/{sjid}/media/{Path(j['reel_path']).name}"
        if j.get("sq_path"):
            row["dl_square"] = f"{base}/api/shorts/{sjid}/media/{Path(j['sq_path']).name}"
        out.append(row)
    return jsonify(out)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    data = request.get_json(force=True) or {}
    msg  = data.get("message", "").strip()
    if not msg:
        return jsonify({"error": "message required"}), 400
    history = data.get("history", [])
    messages = [{"role": "system", "content":
        "You are the Mini Studio AI assistant. "
        "Help the user with video editing, the vlog builder, shorts builder, "
        "edit plans, and channel strategy. Be concise and practical."}]
    for h in history[-6:]:
        if h.get("role") in ("user","assistant"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": msg})
    try:
        reply, provider = call_llm(messages, priority="normal")
        return jsonify({"reply": reply, "provider": provider})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_VLOGS_LIST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vlogs — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080b0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif}
header{background:#0a1210;border-bottom:1px solid #0e2010;padding:16px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.1rem;font-weight:700;color:#4ade80}
header a{color:#555;font-size:.85rem;text-decoration:none;margin-left:auto}
header a:hover{color:#4ade80}
.container{max-width:900px;margin:0 auto;padding:24px}
.job{background:#0d1117;border:1px solid #161f1a;border-radius:10px;padding:16px;margin-bottom:12px;display:flex;align-items:center;gap:14px}
.job-icon{font-size:2rem;flex-shrink:0}
.job-body{flex:1;min-width:0}
.job-title{font-size:.95rem;font-weight:700;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job-meta{font-size:.75rem;color:#555;margin-top:3px}
.job-ch{font-size:.78rem;color:#22c55e;margin-top:2px}
.job-info{font-size:.75rem;color:#888;margin-top:2px;font-style:italic}
.status{font-size:.72rem;padding:2px 8px;border-radius:4px;font-weight:700;flex-shrink:0}
.status.done{background:#0e2a15;color:#22c55e}
.status.running{background:#1a1500;color:#fbbf24}
.status.error{background:#1a0808;color:#f87171}
.exports{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.dl{display:inline-block;padding:5px 12px;border-radius:6px;font-size:.75rem;font-weight:700;text-decoration:none;cursor:pointer}
.dl.mv{background:#0e2a15;color:#22c55e}
.dl.av{background:#0e1a2a;color:#60a5fa}
.dl.bt{background:#1a1030;color:#a78bfa}
.dl:hover{opacity:.8}
.empty{text-align:center;color:#444;padding:48px;font-size:.9rem}
</style>
</head>
<body>
<header>
  <span>🎥</span>
  <h1>All Vlogs</h1>
  <a href="/vlog">+ New Vlog</a>
</header>
<div class="container" id="app"><div class="empty">Loading...</div></div>
<script>
fetch('/api/vlogs').then(r=>r.json()).then(jobs=>{
  const el = document.getElementById('app');
  if (!jobs.length){el.innerHTML='<div class="empty">No vlogs yet — <a href="/vlog" style="color:#22c55e">Build your first vlog</a></div>';return;}
  el.innerHTML = jobs.map(j=>{
    const dt = j.created_at ? new Date(j.created_at).toLocaleDateString() : '';
    const dur = j.total_dur ? `${Math.floor(j.total_dur/60)}m ${j.total_dur%60}s` : '';
    const clips = j.clip_count ? `${j.clip_count} clips` : '';
    let exports = '';
    if (j.dl_my_voice)    exports += `<a class="dl mv" href="${j.dl_my_voice}" download>⬇ My Voice</a>`;
    if (j.dl_ai_voice)    exports += `<a class="dl av" href="${j.dl_ai_voice}" download>⬇ AI Voice</a>`;
    if (j.dl_both_tracks) exports += `<a class="dl bt" href="${j.dl_both_tracks}" download>⬇ Both Tracks</a>`;
    return `<div class="job">
      <div class="job-icon">🎥</div>
      <div class="job-body">
        <div class="job-title">${j.title||'Untitled Vlog'}</div>
        <div class="job-meta">${[dt,dur,clips].filter(Boolean).join(' · ')}</div>
        ${j.channel_name?`<div class="job-ch">📺 ${j.channel_name}</div>`:''}
        ${j.info?`<div class="job-info">${j.info}</div>`:''}
        ${j.error?`<div style="color:#f87171;font-size:.75rem">⚠️ ${j.error}</div>`:''}
        ${exports?`<div class="exports">${exports}</div>`:''}
      </div>
      <span class="status ${j.status}">${j.status}</span>
    </div>`;
  }).join('');
});
setInterval(()=>location.reload(), 15000);
</script>
</body>
</html>"""

_SHORTS_LIST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Shorts — Mini Studio</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0d;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif}
header{background:#0f0520;border-bottom:1px solid #1e1a3f;padding:16px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.1rem;font-weight:700;color:#a78bfa}
header a{color:#555;font-size:.85rem;text-decoration:none;margin-left:auto}
header a:hover{color:#a78bfa}
.container{max-width:900px;margin:0 auto;padding:24px}
.job{background:#0d0d14;border:1px solid #1e1e2e;border-radius:10px;padding:16px;margin-bottom:12px;display:flex;align-items:center;gap:14px}
.job-icon{font-size:2rem;flex-shrink:0}
.job-body{flex:1;min-width:0}
.job-title{font-size:.95rem;font-weight:700;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.job-meta{font-size:.75rem;color:#555;margin-top:3px}
.job-ch{font-size:.78rem;color:#a78bfa;margin-top:2px}
.job-info{font-size:.75rem;color:#888;margin-top:2px;font-style:italic}
.status{font-size:.72rem;padding:2px 8px;border-radius:4px;font-weight:700;flex-shrink:0}
.status.done{background:#1a0535;color:#a78bfa}
.status.running{background:#1a1500;color:#fbbf24}
.status.error{background:#1a0808;color:#f87171}
.exports{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.dl{display:inline-block;padding:5px 12px;border-radius:6px;font-size:.75rem;font-weight:700;text-decoration:none}
.dl.reel{background:#1a0535;color:#a78bfa}
.dl.sq{background:#0e1a2a;color:#60a5fa}
.dl:hover{opacity:.8}
.empty{text-align:center;color:#444;padding:48px;font-size:.9rem}
</style>
</head>
<body>
<header>
  <span>✂️</span>
  <h1>All Shorts</h1>
  <a href="/shorts">+ New Short</a>
</header>
<div class="container" id="app"><div class="empty">Loading...</div></div>
<script>
fetch('/api/shorts/all').then(r=>r.json()).then(jobs=>{
  const el = document.getElementById('app');
  if (!jobs.length){el.innerHTML='<div class="empty">No shorts yet — <a href="/shorts" style="color:#a78bfa">Build your first short</a></div>';return;}
  el.innerHTML = jobs.map(j=>{
    const dt = j.created_at ? new Date(j.created_at).toLocaleDateString() : '';
    const dur = j.total_dur ? `${j.total_dur}s` : (j.duration ? `${j.duration}s target`:'');
    let exports = '';
    if (j.dl_reel)   exports += `<a class="dl reel" href="${j.dl_reel}" download>⬇ 9:16 Reel</a>`;
    if (j.dl_square) exports += `<a class="dl sq" href="${j.dl_square}" download>⬇ 1:1 Square</a>`;
    const styleTag = j.style ? `<span style="background:#1a0535;color:#a78bfa;border-radius:4px;padding:1px 6px;font-size:.7rem;margin-right:4px">${j.style}</span>` : '';
    return `<div class="job">
      <div class="job-icon">✂️</div>
      <div class="job-body">
        <div class="job-title">${styleTag}${j.source_name||'Untitled'}</div>
        <div class="job-meta">${[dt,dur].filter(Boolean).join(' · ')}</div>
        ${j.channel_name?`<div class="job-ch">📺 ${j.channel_name}</div>`:''}
        ${j.info?`<div class="job-info">${j.info}</div>`:''}
        ${j.error?`<div style="color:#f87171;font-size:.75rem">⚠️ ${j.error}</div>`:''}
        ${exports?`<div class="exports">${exports}</div>`:''}
      </div>
      <span class="status ${j.status}">${j.status}</span>
    </div>`;
  }).join('');
});
setInterval(()=>location.reload(), 15000);
</script>
</body>
</html>"""

@app.route("/vlogs")
def vlogs_list_page():
    return Response(_VLOGS_LIST_HTML, mimetype="text/html")

@app.route("/shorts-history")
def shorts_list_page():
    return Response(_SHORTS_LIST_HTML, mimetype="text/html")


# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[director] AI Edit Director starting on :{DIRECTOR_PORT}")
    print(f"[director] Projects dir: {PROJECTS_DIR}")
    print(f"[director] Whisper: {WHISPER_URL}")
    print(f"[director] Groq model: {GROQ_MODEL}")
    app.run(host="0.0.0.0", port=DIRECTOR_PORT, debug=False)
