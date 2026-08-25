"""
Studio Watcher — port 9531
Monitors /opt/studio/media/channels/{name}/inbox/ for new video files.
When a video lands, auto-runs the pipeline for that channel and notifies Telegram.
"""
import os, json, time, threading, urllib.request, urllib.parse
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CHANNELS_FILE  = Path("/opt/studio/channels.json")
BLUEPRINTS_FILE = Path("/opt/studio/blueprints.json")
CHANNELS_ROOT  = Path("/opt/studio/media/channels")
AUTO_URL       = "http://localhost:9530"
WHISPER_URL    = "http://localhost:8421"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}

# In-memory job log
watch_log = []
processing = set()   # filenames currently in flight

def load_json(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}

def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        body = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                           "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[telegram] failed: {e}")

def find_channel_for_path(file_path):
    """Given /opt/studio/media/channels/{name}/inbox/video.mp4, find matching channel."""
    parts = Path(file_path).parts
    # Look for 'channels' in path, then next segment is channel folder name
    try:
        idx = parts.index("channels")
        folder_name = parts[idx + 1]
    except (ValueError, IndexError):
        return None, None
    channels = load_json(CHANNELS_FILE)
    for cid, ch in channels.items():
        safe = ch["name"].lower().replace(" ", "_").replace("/", "-")
        if safe == folder_name or ch.get("folder_name") == folder_name:
            return cid, ch
    return None, None

def find_blueprint_for_channel(channel_id):
    bps = load_json(BLUEPRINTS_FILE)
    for bid, bp in bps.items():
        if channel_id in (bp.get("channel_ids") or []):
            return bid, bp
    return None, None

def process_file(file_path):
    fp = Path(file_path)
    if fp.suffix.lower() not in VIDEO_EXTS:
        return
    if str(fp) in processing:
        return
    processing.add(str(fp))

    cid, channel = find_channel_for_path(file_path)
    ch_name = channel["name"] if channel else fp.parent.parent.name

    log_entry = {
        "file": fp.name, "channel": ch_name,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "running", "job_id": None
    }
    watch_log.append(log_entry)

    telegram(f"⚡ <b>Auto-pipeline started</b>\n📹 {fp.name}\n📡 Channel: {ch_name}")

    try:
        # Build form data
        blueprint_id = ""
        voice_path   = ""
        channel_ids  = []

        if cid:
            channel_ids = [cid]
            voice_path  = channel.get("voice_path", "")
            bid, bp = find_blueprint_for_channel(cid)
            if bid:
                blueprint_id = bid
                if not voice_path:
                    voice_path = bp.get("voice_path", "")

        # Send to pipeline via multipart POST
        import uuid
        boundary = uuid.uuid4().hex
        def field(name, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f"name=\"{name}\"\r\n\r\n{value}\r\n").encode()

        with open(file_path, "rb") as f:
            video_data = f.read()

        body = (
            field("voice_path", voice_path) +
            field("blueprint_id", blueprint_id) +
            field("channel_ids", json.dumps(channel_ids)) +
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{fp.name}\"\r\nContent-Type: video/mp4\r\n\r\n".encode() +
            video_data +
            f"\r\n--{boundary}--\r\n".encode()
        )

        req = urllib.request.Request(
            f"{AUTO_URL}/process", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)

        jid = resp.get("job_id")
        log_entry["job_id"] = jid

        # Poll for completion
        if jid:
            while True:
                time.sleep(5)
                try:
                    with urllib.request.urlopen(f"{AUTO_URL}/job/{jid}", timeout=10) as r2:
                        status = json.load(r2)
                    if status.get("status") in ("done", "error"):
                        break
                except Exception:
                    break

            # auto.py sends the rich done/error Telegram messages (with the
            # OpenCut link) — only track state and write the marker here.
            if status.get("status") == "done":
                log_entry["status"] = "done"
                try:
                    (fp.parent / f"{fp.name}.done").touch()
                    # Raw is now in the asset package + on Google Drive —
                    # remove the inbox copy to keep CT500 lean.
                    fp.unlink(missing_ok=True)
                except Exception:
                    pass
            else:
                log_entry["status"] = "error"

    except Exception as e:
        log_entry["status"] = "error"
        telegram(f"❌ <b>Watcher error</b>\n{fp.name}: {e}")
        print(f"[watcher] error processing {fp}: {e}")
    finally:
        processing.discard(str(fp))


def wait_for_stable_file(fp, checks=3, interval=4, max_wait=1800):
    """Wait until file size stops changing (SMB copies can take minutes).
    Returns True when the file is stable and non-empty."""
    stable = 0
    last_size = -1
    waited = 0
    while waited < max_wait:
        try:
            size = fp.stat().st_size
        except FileNotFoundError:
            return False
        if size > 0 and size == last_size:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
        last_size = size
        time.sleep(interval)
        waited += interval
    return False


class InboxHandler(FileSystemEventHandler):
    def _maybe_process(self, path):
        fp = Path(path)
        if fp.suffix.lower() not in VIDEO_EXTS:
            return
        # Project folders are manual: Sam presses AI Check when ready.
        # (any parent holding project_brief.json = hands off)
        for parent in fp.parents:
            if (parent / "project_brief.json").exists():
                return
            if parent == CHANNELS_ROOT:
                break
        # Skip temp/hidden files created by SMB clients mid-copy
        if fp.name.startswith((".", "~")) or fp.name.endswith((".part", ".tmp", ".crdownload")):
            return
        if str(fp) in processing:
            return
        if not wait_for_stable_file(fp):
            print(f"[watcher] file never stabilised: {fp}")
            return
        print(f"[watcher] new file ready: {fp}")
        threading.Thread(target=process_file, args=(str(fp),), daemon=True).start()

    def on_created(self, event):
        if event.is_directory:
            return
        threading.Thread(target=self._maybe_process, args=(event.src_path,), daemon=True).start()

    def on_moved(self, event):
        # SMB clients often write to a temp name then rename into place
        if event.is_directory:
            return
        threading.Thread(target=self._maybe_process, args=(event.dest_path,), daemon=True).start()


def ensure_channel_folders():
    """Create inbox (+ a-roll/b-roll camera subfolders) and exports for all channels."""
    channels = load_json(CHANNELS_FILE)
    for cid, ch in channels.items():
        safe = ch["name"].lower().replace(" ", "_").replace("/", "-")
        ch["folder_name"] = safe
        base = CHANNELS_ROOT / safe
        for sub in ("inbox", "inbox/a-roll", "inbox/b-roll", "exports"):
            (base / sub).mkdir(parents=True, exist_ok=True)


def ensure_whisper_loaded():
    """Whisper unloads its model on container restart — reload it if needed."""
    try:
        with urllib.request.urlopen(f"{WHISPER_URL}/health", timeout=5) as r:
            health = json.load(r)
        if not health.get("model", {}).get("loaded"):
            req = urllib.request.Request(f"{WHISPER_URL}/load", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=120)
            print("[watcher] whisper model loaded")
    except Exception as e:
        print(f"[watcher] whisper check: {e}")


PROCESSED_MARKER = ".processed"

def catch_up_scan():
    """Process any videos that landed in inboxes while the watcher was down."""
    handler = InboxHandler()
    for inbox in CHANNELS_ROOT.glob("*/inbox"):
        for f in inbox.iterdir():
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            marker = inbox / f"{f.name}.done"
            if marker.exists():
                continue
            print(f"[watcher] catch-up: {f}")
            threading.Thread(target=handler._maybe_process, args=(str(f),), daemon=True).start()


def cleanup_old_work(days=7):
    """Delete pipeline work dirs older than `days` to stop the disk filling up."""
    import shutil
    cutoff = time.time() - days * 86400
    work_root = Path("/opt/studio/auto_work")
    if not work_root.exists():
        return
    for d in work_root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


def start_watcher():
    ensure_channel_folders()
    observer = Observer()

    def watch_all():
        # Watch the root channels dir — handles new subfolders too
        observer.schedule(InboxHandler(), str(CHANNELS_ROOT), recursive=True)
        observer.start()
        print(f"[watcher] watching {CHANNELS_ROOT}")
        catch_up_scan()
        cleanup_counter = 0
        try:
            while True:
                # Re-create folders for any new channels added while running
                ensure_channel_folders()
                ensure_whisper_loaded()
                cleanup_counter += 1
                if cleanup_counter >= 60:   # roughly hourly
                    cleanup_old_work()
                    cleanup_counter = 0
                time.sleep(60)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

    threading.Thread(target=watch_all, daemon=True).start()


# ── Status API ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    channels = load_json(CHANNELS_FILE)
    ch_list = []
    for cid, ch in channels.items():
        safe = ch["name"].lower().replace(" ", "_").replace("/", "-")
        inbox = CHANNELS_ROOT / safe / "inbox"
        ch_list.append({
            "id": cid,
            "name": ch["name"],
            "folder": f"/opt/studio/media/channels/{safe}/inbox",
            "smb": f"\\\\192.168.0.78\\studio\\channels\\{safe}\\inbox",
            "files": len(list(inbox.glob("*"))) if inbox.exists() else 0,
        })
    return jsonify({
        "status": "running",
        "watching": str(CHANNELS_ROOT),
        "channels": ch_list,
        "recent_jobs": watch_log[-20:],
        "processing": list(processing),
        "telegram_active": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
    })

@app.route("/refresh-folders", methods=["POST"])
def refresh_folders():
    ensure_channel_folders()
    return jsonify({"ok": True})

@app.route("/log")
def log_view():
    return jsonify(watch_log[-50:])

@app.route("/telegram", methods=["POST"])
def save_telegram():
    global TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
    data = request.get_json(force=True)
    token   = data.get("token", "").strip()
    chat_id = data.get("chat_id", "").strip()
    if not token or not chat_id:
        return jsonify({"error": "token and chat_id required"}), 400
    TELEGRAM_TOKEN   = token
    TELEGRAM_CHAT_ID = chat_id
    # Persist to service env file so it survives restarts
    env_file = Path("/etc/systemd/system/studio-watcher.service")
    if env_file.exists():
        txt = env_file.read_text()
        txt = __import__('re').sub(r'Environment=TELEGRAM_BOT_TOKEN=.*', f'Environment=TELEGRAM_BOT_TOKEN={token}', txt)
        txt = __import__('re').sub(r'Environment=TELEGRAM_CHAT_ID=.*', f'Environment=TELEGRAM_CHAT_ID={chat_id}', txt)
        env_file.write_text(txt)
        __import__('subprocess').run(['systemctl', 'daemon-reload'], capture_output=True)
    # Send test message
    try:
        telegram(f"✅ <b>Mini Studio connected!</b>\nTelegram notifications are now active.")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


if __name__ == "__main__":
    start_watcher()
    app.run(host="0.0.0.0", port=9531, debug=False)
