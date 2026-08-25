"""
Channel Export Service — port 9510
3-channel audio export from a single recording:
  A: Background + your voice
  B: Background + AI clone voice
  C: Background + your voice + AI clone (YouTube — both layered/swap)
"""
import os, json, uuid, subprocess, threading, time, requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.utils import secure_filename

app = Flask(__name__)
CORS(app, origins=["*"])
WORK    = Path("/opt/studio/channel_work")
EXPORT  = Path("/opt/studio/channel_exports")
META_F  = Path("/opt/studio/voices_meta.json")   # name → TTS container path

for d in [WORK, EXPORT]:
    d.mkdir(parents=True, exist_ok=True)

TTS_URL     = os.getenv("TTS_URL",     "http://localhost:8422")
WHISPER_URL = os.getenv("WHISPER_URL", "http://localhost:8421")

jobs = {}

CHANNELS = {
    "A": {"label": "Background + Your Voice",          "mix": "voice_only"},
    "B": {"label": "Background + AI Clone Voice",       "mix": "clone_only"},
    "C": {"label": "Both (YouTube — voice + AI layer)", "mix": "both"},
}

# ─── Voice meta storage ──────────────────────────────────────────────────────
def load_meta():
    if META_F.exists():
        try: return json.loads(META_F.read_text())
        except: pass
    return {}

def save_meta(name, tts_path):
    meta = load_meta()
    meta[name] = {"tts_path": tts_path, "added": time.strftime("%Y-%m-%d %H:%M")}
    META_F.write_text(json.dumps(meta, indent=2))

# ─── Routes ──────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return """<!DOCTYPE html><html><head><title>Channel Export</title>
<style>body{font-family:sans-serif;background:#111;color:#eee;padding:40px;max-width:600px}
h1{margin-bottom:8px}p{color:#888;margin-bottom:24px}
a{display:inline-block;background:#3b82f6;color:#fff;padding:10px 20px;border-radius:8px;
text-decoration:none;margin:6px 4px;font-size:.9rem}a:hover{background:#2563eb}
code{background:#222;padding:2px 6px;border-radius:4px;font-size:.85rem}</style></head>
<body><h1>🎚️ Channel Export Service</h1>
<p>Running on port 9510. Use these endpoints or go back to the control panel.</p>
<a href="/voices">View Voices</a>
<a href="/health">Health Check</a>
<a href="/list-exports">List Exports</a>
<a href="http://192.168.0.78:85">← Control Panel</a>
<h3 style="margin-top:28px;font-size:.85rem;color:#666">API Endpoints</h3>
<p style="font-size:.8rem;line-height:2">
<code>POST /clone-voice</code> — upload audio to clone a voice<br>
<code>POST /export</code> — export A/B/C channel mix<br>
<code>POST /transcribe</code> — transcribe audio via Whisper<br>
<code>GET /job/&lt;id&gt;</code> — check export job status
</p></body></html>""", 200, {"Content-Type": "text/html"}

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "channel-export"})

@app.route("/channels")
def list_channels():
    return jsonify({"channels": CHANNELS})

@app.route("/voices")
def list_voices():
    meta = load_meta()
    # Also try to get TTS service status
    tts_up = False
    try:
        r = requests.get(f"{TTS_URL}/health", timeout=3)
        tts_up = r.json().get("status") == "ok"
    except: pass
    return jsonify({"voices": list(meta.keys()), "tts_online": tts_up})

@app.route("/clone-voice", methods=["POST"])
def clone_voice():
    """Upload audio sample → stores in TTS service for cloning"""
    name = request.form.get("name", "").strip()
    f = request.files.get("file")
    if not name or not f:
        return jsonify({"error": "need name and audio file"}), 400

    # Sanitise name
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)

    try:
        resp = requests.post(
            f"{TTS_URL}/clone-voice",
            files={"file": (f.filename, f.stream, f.mimetype)},
            data={"name": safe},
            timeout=60
        )
        d = resp.json()
        if resp.status_code == 200 and "path" in d:
            save_meta(name, d["path"])   # store container-internal path
            return jsonify({"success": True, "name": name, "tts_path": d["path"]})
        else:
            return jsonify({"error": d.get("detail", "TTS upload failed")}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/transcribe", methods=["POST"])
def transcribe():
    f = request.files.get("file")
    if not f: return jsonify({"error": "no file"}), 400
    tmp = WORK / f"{uuid.uuid4().hex[:6]}_{secure_filename(f.filename)}"
    f.save(str(tmp))
    try:
        resp = requests.post(f"{WHISPER_URL}/transcribe",
                             files={"file": open(str(tmp), "rb")}, timeout=300)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        tmp.unlink(missing_ok=True)

@app.route("/export", methods=["POST"])
def start_export():
    """
    {
      "video_path": "...",
      "voice_path": "...",      (optional — user's recorded voice)
      "bg_path": "...",         (optional — background music)
      "clone_voice": "Sam",     (voice name from /clone-voice)
      "clone_text": "...",      (text to speak; if blank, auto-transcribed from voice)
      "channels": ["A","B","C"],
      "bg_volume": 0.15,
      "voice_volume": 1.0,
      "clone_volume": 1.0,
      "clone_speed": 1.0
    }
    """
    data = request.json or {}
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "Queued", "outputs": {}}
    threading.Thread(target=run_export, args=(job_id, data), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/job/<job_id>")
def job_status(job_id):
    return jsonify(jobs.get(job_id, {"status": "not_found"}))

@app.route("/exports/<path:filename>")
def download(filename):
    return send_from_directory(str(EXPORT), filename)

@app.route("/list-exports")
def list_exports():
    items = []
    for p in sorted(EXPORT.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix in (".mp4", ".wav", ".mp3"):
            items.append({"name": p.name, "size_mb": round(p.stat().st_size/1024/1024, 1),
                          "url": f"/exports/{p.name}"})
    return jsonify({"exports": items})

# ─── Export logic ────────────────────────────────────────────────────────────
def run_export(job_id, data):
    try:
        video      = data.get("video_path", "")
        voice      = data.get("voice_path", "")     # user's recorded voice
        bg         = data.get("bg_path", "")
        clone_name = data.get("clone_voice", "")
        clone_text = data.get("clone_text", "")
        channels   = data.get("channels", ["A","B","C"])
        bg_vol     = float(data.get("bg_volume", 0.15))
        v_vol      = float(data.get("voice_volume", 1.0))
        cl_vol     = float(data.get("clone_volume", 1.0))
        cl_speed   = float(data.get("clone_speed", 1.0))

        if not video or not Path(video).exists():
            jobs[job_id] = {"status": "error", "error": "video_path not found or not provided"}
            return

        # Step 1: Auto-transcribe voice if clone needed but no text given
        if ("B" in channels or "C" in channels) and clone_name and not clone_text and voice:
            upd(job_id, 5, "Transcribing your voice for AI clone...")
            clone_text = transcribe_audio(voice)

        # Step 2: Generate AI clone audio
        clone_audio = None
        if ("B" in channels or "C" in channels) and clone_name and clone_text:
            upd(job_id, 15, "Generating AI clone voice...")
            meta = load_meta()
            tts_path = meta.get(clone_name, {}).get("tts_path")
            clone_audio = gen_clone(job_id, tts_path, clone_text, cl_speed)

        # Step 3: Export each requested channel
        outputs = {}
        total_ch = len(channels)
        for i, ch in enumerate(channels):
            p0 = 25 + (i * 70 // total_ch)
            p1 = 25 + ((i+1) * 70 // total_ch)
            upd(job_id, p0, f"Exporting channel {ch} — {CHANNELS[ch]['label']}...")
            out = str(EXPORT / f"{job_id}_ch{ch}.mp4")
            mix = CHANNELS[ch]["mix"]

            if mix == "voice_only":
                mix_audio(video, voice or None, bg or None, out, v_vol, None, bg_vol)
            elif mix == "clone_only":
                mix_audio(video, clone_audio, bg or None, out, cl_vol, None, bg_vol)
            elif mix == "both":
                mix_audio(video, voice or None, bg or None, out, v_vol, clone_audio, bg_vol, cl_vol)

            outputs[ch] = {
                "label": CHANNELS[ch]["label"],
                "file":  f"{job_id}_ch{ch}.mp4",
                "url":   f"/exports/{job_id}_ch{ch}.mp4"
            }
            upd(job_id, p1, f"Channel {ch} done")

        jobs[job_id] = {"status": "done", "progress": 100, "outputs": outputs,
                        "message": f"{total_ch} channel(s) exported"}
    except Exception as e:
        import traceback
        jobs[job_id] = {"status": "error", "error": str(e), "trace": traceback.format_exc()}

def upd(job_id, pct, msg):
    jobs[job_id]["progress"] = pct
    jobs[job_id]["message"]  = msg

# ─── FFmpeg audio mixer ───────────────────────────────────────────────────────
def mix_audio(video, voice, bg, out, v_vol=1.0, clone=None, bg_vol=0.15, cl_vol=1.0):
    """
    Mix video with up to 3 audio sources:
      voice  — user's voice
      clone  — AI clone audio (only for channel C "both")
      bg     — background music
    """
    inputs, filter_parts, audio_labels = ["-i", video], [], []
    idx = 1

    if voice and Path(voice).exists():
        inputs += ["-i", voice]
        filter_parts.append(f"[{idx}:a]volume={v_vol}[v{idx}]")
        audio_labels.append(f"[v{idx}]")
        idx += 1

    if clone and Path(clone).exists():
        inputs += ["-i", clone]
        filter_parts.append(f"[{idx}:a]volume={cl_vol}[c{idx}]")
        audio_labels.append(f"[c{idx}]")
        idx += 1

    if bg and Path(bg).exists():
        inputs += ["-i", bg]
        filter_parts.append(f"[{idx}:a]volume={bg_vol}[b{idx}]")
        audio_labels.append(f"[b{idx}]")
        idx += 1

    cmd = ["ffmpeg", "-y"] + inputs
    if audio_labels:
        n = len(audio_labels)
        mix_filter = "".join(audio_labels) + f"amix=inputs={n}:duration=first:normalize=0[aout]"
        filter_parts.append(mix_filter)
        cmd += ["-filter_complex", ";".join(filter_parts)]
        cmd += ["-map", "0:v", "-map", "[aout]"]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]   # keep original audio

    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)

# ─── Helpers ──────────────────────────────────────────────────────────────────
def get_duration(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", path], capture_output=True, text=True, timeout=10)
    try: return float(r.stdout.strip())
    except: return 60.0

def gen_clone(job_id, tts_path, text, speed=1.0):
    """Call TTS service /generate with cloned voice"""
    out = str(WORK / f"{job_id}_clone.wav")
    payload = {"text": text, "language": "en", "speed": speed}
    if tts_path:
        payload["speaker_wav"] = tts_path   # container-internal path
    try:
        r = requests.post(f"{TTS_URL}/generate", json=payload, timeout=300)
        if r.status_code == 200:
            with open(out, "wb") as f: f.write(r.content)
            return out
        else:
            print(f"TTS generate failed: {r.text}")
    except Exception as e:
        print(f"Clone gen error: {e}")
    return None

def transcribe_audio(path):
    try:
        with open(path, "rb") as f:
            r = requests.post(f"{WHISPER_URL}/transcribe", files={"file": f}, timeout=300)
        d = r.json()
        if "text" in d: return d["text"]
        if "segments" in d: return " ".join(s["text"] for s in d.get("segments", []))
    except Exception as e:
        print(f"Transcribe failed: {e}")
    return ""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9510, debug=False)
