"""
Studio Watcher — port 9531
Monitors /opt/studio/media/channels/{name}/inbox/ for new video files.

Full flow:
  1. File lands in channel inbox (SMB drop or direct copy)
  2. Wait for file to stabilise (SMB copies can be slow)
  3. Ask Sam via Telegram: "How do you want this edited?"
  4. Wait up to 5 minutes for reply; default if no reply
  5. Submit to Edit Director (:9533) with instruction + channel profile
  6. Poll until done; send Telegram with bridge URL + stats
"""
import os, json, time, threading, urllib.request, urllib.parse
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

CHANNELS_FILE   = Path("/opt/studio/channels.json")
CHANNELS_ROOT   = Path("/opt/studio/media/channels")
DIRECTOR_URL    = "http://localhost:9533"
WHISPER_URL     = "http://localhost:8421"
OPENCUT_HOST    = os.getenv("DIRECTOR_HOST", "192.168.0.78")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

INSTRUCTION_TIMEOUT = 300   # seconds to wait for Sam's reply
DEFAULT_INSTRUCTION = "Edit this video. Remove all silences longer than 1 second. Remove filler words. Keep the most interesting content."

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv"}

watch_log  = []
processing = set()


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_json(path):
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}

AUTO_URL = "http://localhost:9530"

def telegram(msg):
    """Send a Telegram message via auto.py (which owns the TG polling loop)."""
    try:
        body = json.dumps({"prompt": msg, "timeout": 0}).encode()
        req = urllib.request.Request(
            f"{AUTO_URL}/tg-wait-reply", data=body,
            headers={"Content-Type": "application/json", "User-Agent": "MiniStudio/1.0"}
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[watcher] telegram send error: {e}")

def tg_ask_instruction(channel_name: str, filename: str, filesize_mb: float) -> str:
    """
    Send 'how do you want this edited?' via auto.py and wait for Sam's reply.
    auto.py owns the getUpdates long-poll so there's no 409 conflict.
    """
    size_str = f"{filesize_mb:.1f} MB" if filesize_mb > 0 else ""
    prompt = (
        f"🎬 <b>New video ready to edit</b>\n"
        f"📹 {filename}" + (f" ({size_str})" if size_str else "") + "\n"
        f"📡 Channel: {channel_name}\n\n"
        f"Reply with your editing instruction within {INSTRUCTION_TIMEOUT // 60} minutes.\n"
        "<i>e.g. \"Cut tight, remove silences, keep the funny bits\"</i>\n"
        "<i>(No reply = auto-edit with defaults)</i>"
    )

    try:
        body = json.dumps({"prompt": prompt, "timeout": INSTRUCTION_TIMEOUT}).encode()
        req = urllib.request.Request(
            f"{AUTO_URL}/tg-wait-reply", data=body,
            headers={"Content-Type": "application/json", "User-Agent": "MiniStudio/1.0"}
        )
        with urllib.request.urlopen(req, timeout=INSTRUCTION_TIMEOUT + 30) as r:
            resp = json.loads(r.read())
        reply = (resp.get("reply") or "").strip()
        if reply:
            print(f"[watcher] TG instruction received: {reply[:80]}")
            return reply
        print("[watcher] TG instruction timeout — using default")
        return DEFAULT_INSTRUCTION
    except Exception as e:
        print(f"[watcher] tg_ask_instruction error: {e}")
        return DEFAULT_INSTRUCTION


# ── Channel helpers ───────────────────────────────────────────────────────────

def find_channel_for_path(file_path):
    parts = Path(file_path).parts
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


# ── Main file processor ───────────────────────────────────────────────────────

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

    try:
        filesize_mb = fp.stat().st_size / 1024 / 1024
    except Exception:
        filesize_mb = 0

    try:
        # ── Step 1: Ask Sam for instruction ───────────────────────────────
        instruction = tg_ask_instruction(ch_name, fp.name, filesize_mb)

        # ── Step 2: Submit to Edit Director ───────────────────────────────
        payload = json.dumps({
            "video_path":   str(fp),
            "channel_id":   cid or "",
            "instruction":  instruction,
            "project_name": fp.stem
        }).encode()

        req = urllib.request.Request(
            f"{DIRECTOR_URL}/api/process",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "MiniStudio/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())

        jid = resp.get("job_id")
        log_entry["job_id"] = jid
        print(f"[watcher] submitted job {jid} for {fp.name}")

        if not jid:
            raise ValueError(f"Director returned no job_id: {resp}")

        # ── Step 3: Poll until done ───────────────────────────────────────
        max_wait = 3600   # 1 hour ceiling
        waited   = 0
        final_status = {}
        while waited < max_wait:
            time.sleep(8)
            waited += 8
            try:
                with urllib.request.urlopen(
                    f"{DIRECTOR_URL}/api/jobs/{jid}", timeout=10
                ) as r2:
                    final_status = json.loads(r2.read())
                if final_status.get("status") in ("awaiting_review", "error"):
                    break
            except Exception as e:
                print(f"[watcher] poll error: {e}")

        if final_status.get("status") == "awaiting_review":
            log_entry["status"] = "done"
            # Director already sends a TG done message — add the bridge link on top
            bridge = f"http://{OPENCUT_HOST}:9500/ai-bridge.html?job={jid}"
            src_s = int(final_status.get("source_duration", 0))
            plan = load_json(Path(f"/opt/studio/projects/{jid}/edit-plan/current.json"))
            clips = plan.get("clips", [])
            out_s = int(sum(c.get("source_end", 0) - c.get("source_start", 0) for c in clips))
            saved  = src_s - out_s
            confidence = plan.get("review", {}).get("ai_confidence", 0)

            telegram(
                f"✅ <b>Edit ready — {channel_name_for_tg(ch_name)}</b>\n"
                f"⏱ {fmt_dur(src_s)} → {fmt_dur(out_s)}"
                + (f" (saved {fmt_dur(saved)})" if saved > 0 else "") + "\n"
                f"✂️ {len(plan.get('cuts', []))} cuts · "
                f"{int(confidence * 100)}% confidence\n\n"
                f"🔗 Open in OpenCut:\n{bridge}\n\n"
                f"<i>Make your final 5% edits → export all 3 versions</i>"
            )

            # Write .done marker and remove inbox copy to keep CT103 lean
            try:
                (fp.parent / f"{fp.name}.done").touch()
                fp.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            log_entry["status"] = "error"
            err = final_status.get("steps", {}).get(
                final_status.get("current_step", ""), {}
            ).get("msg", "unknown error")
            telegram(f"❌ <b>Edit failed</b> — {fp.name}\n{err[:200]}")

    except Exception as e:
        import traceback
        log_entry["status"] = "error"
        print(f"[watcher] error processing {fp}: {traceback.format_exc()}")
        telegram(f"❌ <b>Watcher error</b>\n{fp.name}: {e}")
    finally:
        processing.discard(str(fp))


def channel_name_for_tg(name: str) -> str:
    return name.replace("<", "").replace(">", "").replace("&", "+")

def fmt_dur(seconds: int) -> str:
    if seconds <= 0:
        return "0:00"
    return f"{seconds // 60}:{seconds % 60:02d}"


# ── File stability + watchdog ─────────────────────────────────────────────────

def wait_for_stable_file(fp, checks=3, interval=4, max_wait=1800):
    stable, last_size, waited = 0, -1, 0
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
        for parent in fp.parents:
            if (parent / "project_brief.json").exists():
                return
            if parent == CHANNELS_ROOT:
                break
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
        if not event.is_directory:
            threading.Thread(target=self._maybe_process, args=(event.src_path,), daemon=True).start()

    def on_moved(self, event):
        if not event.is_directory:
            threading.Thread(target=self._maybe_process, args=(event.dest_path,), daemon=True).start()


# ── Background maintenance ────────────────────────────────────────────────────

def ensure_channel_folders():
    channels = load_json(CHANNELS_FILE)
    for cid, ch in channels.items():
        safe = ch["name"].lower().replace(" ", "_").replace("/", "-")
        ch["folder_name"] = safe
        base = CHANNELS_ROOT / safe
        for sub in ("inbox", "inbox/a-roll", "inbox/b-roll", "exports"):
            (base / sub).mkdir(parents=True, exist_ok=True)

def ensure_whisper_loaded():
    try:
        with urllib.request.urlopen(f"{WHISPER_URL}/health", timeout=5) as r:
            health = json.load(r)
        if not health.get("model", {}).get("loaded"):
            req = urllib.request.Request(
                f"{WHISPER_URL}/load", data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req, timeout=120)
            print("[watcher] whisper model loaded")
    except Exception as e:
        print(f"[watcher] whisper check: {e}")

def catch_up_scan():
    handler = InboxHandler()
    for inbox in CHANNELS_ROOT.glob("*/inbox"):
        for f in inbox.iterdir():
            if not f.is_file() or f.suffix.lower() not in VIDEO_EXTS:
                continue
            if (inbox / f"{f.name}.done").exists():
                continue
            print(f"[watcher] catch-up: {f}")
            threading.Thread(target=handler._maybe_process, args=(str(f),), daemon=True).start()

def cleanup_old_projects(days=30):
    """Remove Edit Director project dirs older than `days` to keep CT103 lean."""
    import shutil
    cutoff = time.time() - days * 86400
    proj_root = Path("/opt/studio/projects")
    if not proj_root.exists():
        return
    for d in proj_root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

def start_watcher():
    ensure_channel_folders()
    observer = Observer()

    def watch_all():
        observer.schedule(InboxHandler(), str(CHANNELS_ROOT), recursive=True)
        observer.start()
        print(f"[watcher] watching {CHANNELS_ROOT}")
        catch_up_scan()
        cleanup_counter = 0
        try:
            while True:
                ensure_channel_folders()
                ensure_whisper_loaded()
                cleanup_counter += 1
                if cleanup_counter >= 60:
                    cleanup_old_projects()
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
            "smb": f"\\\\192.168.0.78\\studio\\channels\\{safe}\\inbox",
            "files": len([f for f in inbox.iterdir() if f.is_file() and not f.name.endswith(".done")])
                     if inbox.exists() else 0,
        })
    return jsonify({
        "status": "running",
        "watching": str(CHANNELS_ROOT),
        "channels": ch_list,
        "recent_jobs": watch_log[-20:],
        "processing": list(processing),
        "telegram_active": bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID),
        "instruction_timeout_sec": INSTRUCTION_TIMEOUT,
    })

@app.route("/refresh-folders", methods=["POST"])
def refresh_folders():
    ensure_channel_folders()
    return jsonify({"ok": True})

@app.route("/log")
def log_view():
    return jsonify(watch_log[-50:])

@app.route("/trigger", methods=["POST"])
def manual_trigger():
    """Manually trigger processing for a file path (for testing)."""
    data = request.get_json(force=True) or {}
    path = data.get("path", "")
    if not path or not Path(path).exists():
        return jsonify({"error": "path not found"}), 400
    threading.Thread(target=process_file, args=(path,), daemon=True).start()
    return jsonify({"ok": True, "path": path})

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
    import re, subprocess
    svc = Path("/etc/systemd/system/studio-watcher.service")
    if svc.exists():
        txt = svc.read_text()
        txt = re.sub(r'Environment=TELEGRAM_BOT_TOKEN=.*', f'Environment=TELEGRAM_BOT_TOKEN={token}', txt)
        txt = re.sub(r'Environment=TELEGRAM_CHAT_ID=.*', f'Environment=TELEGRAM_CHAT_ID={chat_id}', txt)
        svc.write_text(txt)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True)
    telegram("✅ <b>Mini Studio watcher connected!</b>\nTelegram notifications active.")
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_watcher()
    app.run(host="0.0.0.0", port=9531, debug=False)
