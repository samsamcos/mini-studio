"""
Mini Studio — TTS/STT Pipeline  :9532
- POST /api/transcribe        video/audio file → Whisper transcript
- POST /api/tts               text → TTS audio (Pocket TTS :5020 or Fish :5022)
- POST /api/video-to-tts      video → transcript → TTS audio (full pipeline)
- GET  /api/status            health + last job info
"""
import os, time, wave, json, struct, subprocess, threading, tempfile
from flask import Flask, request, jsonify, send_file
import requests

app = Flask(__name__)

POCKET_TTS_URL = os.environ.get("POCKET_TTS_URL", "http://127.0.0.1:5020")
FISH_TTS_URL   = os.environ.get("FISH_TTS_URL",   "http://127.0.0.1:5022")
TTS_OUT_DIR    = os.environ.get("TTS_OUT_DIR",    "/opt/studio/tts_output")
WHISPER_MODEL  = os.environ.get("WHISPER_MODEL",  "small")

os.makedirs(TTS_OUT_DIR, exist_ok=True)

_status = {"transcriptions": 0, "tts_jobs": 0, "last_transcript": "", "last_tts_file": "", "whisper_ready": False}
_model = None
_model_lock = threading.Lock()

def _load_whisper():
    global _model
    with _model_lock:
        if _model is None:
            try:
                from faster_whisper import WhisperModel
                cache = "/opt/studio/whisper_models"
                os.makedirs(cache, exist_ok=True)
                _model = WhisperModel(WHISPER_MODEL, compute_type="int8", download_root=cache)
                _status["whisper_ready"] = True
                print(f"[STT] Whisper {WHISPER_MODEL} loaded")
            except Exception as e:
                print(f"[STT] Whisper load failed: {e}")
    return _model

threading.Thread(target=_load_whisper, daemon=True).start()


def extract_audio(video_path, out_wav=None):
    """Extract 16kHz mono WAV from any video/audio file."""
    if out_wav is None:
        out_wav = tempfile.mktemp(suffix=".wav", dir="/tmp")
    subprocess.run([
        "ffmpeg", "-y", "-i", video_path,
        "-ar", "16000", "-ac", "1", "-f", "wav", out_wav
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_wav


def transcribe_file(path):
    """Transcribe audio/video file using local Whisper. Returns full text."""
    model = _load_whisper()
    if model is None:
        return "", "Whisper not loaded"
    wav = extract_audio(path)
    try:
        segments, info = model.transcribe(wav, beam_size=3)
        text = " ".join(s.text.strip() for s in segments).strip()
        return text, None
    except Exception as e:
        return "", str(e)
    finally:
        try: os.unlink(wav)
        except: pass


def call_pocket_tts(text, voice="default"):
    """Call Pocket TTS and return WAV bytes. Uses multipart/form-data."""
    data = {"text": text}
    if voice and voice != "default":
        data["voice_url"] = voice  # built-in name e.g. "alba", or http:// URL
    resp = requests.post(
        POCKET_TTS_URL + "/tts",
        data=data,
        timeout=60
    )
    if resp.status_code == 200:
        return resp.content, None
    return None, f"Pocket TTS {resp.status_code}: {resp.text[:100]}"


def call_fish_tts(text):
    """Call Fish Speech TTS and return WAV bytes."""
    resp = requests.post(
        FISH_TTS_URL + "/v1/tts",
        json={"text": text, "format": "wav", "streaming": False},
        timeout=300
    )
    if resp.status_code == 200:
        return resp.content, None
    return None, f"Fish TTS {resp.status_code}"


def save_tts(wav_bytes, label="tts"):
    fname = os.path.join(TTS_OUT_DIR, f"{label}_{int(time.time())}.wav")
    with open(fname, "wb") as f:
        f.write(wav_bytes)
    return fname


# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mini Studio — TTS / STT</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#111;color:#e0e0e0;min-height:100vh;padding:24px}
  h1{font-size:1.4rem;font-weight:700;color:#fff;margin-bottom:6px}
  .sub{color:#888;font-size:.85rem;margin-bottom:24px}
  .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:20px;margin-bottom:18px}
  .card h2{font-size:1rem;font-weight:600;color:#ccc;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .badge{background:#2a2a2a;color:#888;font-size:.7rem;padding:2px 7px;border-radius:20px;font-weight:500}
  label{display:block;font-size:.8rem;color:#888;margin-bottom:5px}
  textarea,select,input[type=file],input[type=text]{width:100%;background:#111;border:1px solid #333;color:#e0e0e0;border-radius:6px;padding:10px;font-size:.9rem;font-family:inherit;outline:none}
  textarea:focus,select:focus,input:focus{border-color:#555}
  textarea{resize:vertical;min-height:90px}
  .row{display:flex;gap:10px;margin-bottom:12px}
  .row>*{flex:1}
  button{background:#3a6fd8;color:#fff;border:none;border-radius:6px;padding:10px 18px;font-size:.9rem;font-weight:600;cursor:pointer;width:100%;margin-top:4px}
  button:hover{background:#4a7fe8}
  button:disabled{background:#333;color:#666;cursor:not-allowed}
  .result{margin-top:14px;background:#111;border:1px solid #2a2a2a;border-radius:6px;padding:12px;display:none}
  .result.show{display:block}
  .result audio{width:100%;margin-top:8px}
  .transcript-box{font-size:.85rem;color:#aaa;line-height:1.5;white-space:pre-wrap;max-height:120px;overflow-y:auto}
  .err{color:#e05555;font-size:.85rem}
  .ok{color:#55c055;font-size:.85rem}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid #555;border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite;margin-right:6px;vertical-align:middle}
  @keyframes spin{to{transform:rotate(360deg)}}
  .status-bar{display:flex;gap:16px;font-size:.78rem;color:#666;margin-bottom:20px}
  .dot{width:7px;height:7px;border-radius:50%;background:#444;display:inline-block;margin-right:4px}
  .dot.on{background:#55c055}
  .tabs{display:flex;gap:4px;margin-bottom:16px}
  .tab{padding:7px 16px;border-radius:6px;font-size:.85rem;font-weight:600;cursor:pointer;border:1px solid #2a2a2a;background:#1a1a1a;color:#888}
  .tab.active{background:#3a6fd8;color:#fff;border-color:#3a6fd8}
  .panel{display:none}.panel.active{display:block}
</style>
</head>
<body>
<h1>Mini Studio — TTS / STT</h1>
<p class="sub">192.168.0.78:9532 &nbsp;·&nbsp; Pocket TTS &amp; Fish TTS &nbsp;·&nbsp; Whisper STT</p>

<div class="status-bar">
  <span><span class="dot" id="d-pocket"></span>Pocket TTS :5020</span>
  <span><span class="dot" id="d-fish"></span>Fish TTS :5022</span>
  <span><span class="dot" id="d-whisper"></span>Whisper STT</span>
  <span id="job-count" style="margin-left:auto"></span>
</div>

<div class="tabs">
  <div class="tab active" onclick="switchTab('tts')">Text → TTS</div>
  <div class="tab" onclick="switchTab('video')">Video → TTS</div>
  <div class="tab" onclick="switchTab('transcribe')">Transcribe Only</div>
</div>

<!-- Tab 1: Text to TTS -->
<div id="tab-tts" class="panel active">
<div class="card">
  <h2>Type or paste text <span class="badge">Text → Audio</span></h2>
  <label>Text</label>
  <textarea id="tts-text" placeholder="Enter your script here..."></textarea>
  <div class="row" style="margin-top:12px">
    <div>
      <label>Engine</label>
      <select id="tts-engine">
        <option value="pocket">Pocket TTS (fast)</option>
        <option value="fish">Fish TTS (slower)</option>
      </select>
    </div>
  </div>
  <button id="tts-btn" onclick="doTTS()">Generate Audio</button>
  <div class="result" id="tts-result">
    <div id="tts-msg"></div>
    <audio id="tts-audio" controls></audio>
  </div>
</div>
</div>

<!-- Tab 2: Video → Transcript → TTS -->
<div id="tab-video" class="panel">
<div class="card">
  <h2>Upload video/audio <span class="badge">Transcribe → TTS</span></h2>
  <label>Video or audio file (mp4, mp3, wav, mkv…)</label>
  <input type="file" id="vid-file" accept="video/*,audio/*">
  <div class="row" style="margin-top:12px">
    <div>
      <label>TTS Engine</label>
      <select id="vid-engine">
        <option value="pocket">Pocket TTS (fast)</option>
        <option value="fish">Fish TTS (slower)</option>
      </select>
    </div>
  </div>
  <button id="vid-btn" onclick="doVideoTTS()">Transcribe + Generate TTS</button>
  <div class="result" id="vid-result">
    <div id="vid-msg"></div>
    <div id="vid-transcript" class="transcript-box" style="margin-top:8px"></div>
    <audio id="vid-audio" controls style="margin-top:10px;width:100%"></audio>
  </div>
</div>
</div>

<!-- Tab 3: Transcribe only -->
<div id="tab-transcribe" class="panel">
<div class="card">
  <h2>Upload video/audio <span class="badge">Transcribe Only</span></h2>
  <label>Video or audio file</label>
  <input type="file" id="stt-file" accept="video/*,audio/*">
  <button id="stt-btn" onclick="doTranscribe()">Transcribe</button>
  <div class="result" id="stt-result">
    <div id="stt-msg"></div>
    <div id="stt-transcript" class="transcript-box" style="margin-top:8px"></div>
    <button id="stt-copy" style="margin-top:8px;background:#222;font-size:.8rem" onclick="copyTranscript()">Copy text</button>
    <button id="stt-to-tts" style="margin-top:6px;background:#2a5a28;font-size:.8rem" onclick="sendToTTS()">→ Send to TTS</button>
  </div>
</div>
</div>

<script>
const BASE = window.location.origin;
let lastTranscript = '';

function switchTab(t) {
  document.querySelectorAll('.tab').forEach((el,i) => el.classList.toggle('active', ['tts','video','transcribe'][i]===t));
  document.querySelectorAll('.panel').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-'+t).classList.add('active');
}

async function refreshStatus() {
  try {
    const s = await fetch(BASE+'/api/status').then(r=>r.json());
    document.getElementById('d-whisper').className = 'dot '+(s.whisper_ready?'on':'');
    document.getElementById('job-count').textContent = s.tts_jobs+' TTS · '+s.transcriptions+' transcripts';
  } catch(e){}
  // ping TTS engines
  for (const [id, port] of [['d-pocket',5020],['d-fish',5022]]) {
    fetch('http://'+location.hostname+':'+port+'/health').then(r=>{
      document.getElementById(id).className = 'dot '+(r.ok?'on':'');
    }).catch(()=>{ document.getElementById(id).className = 'dot'; });
  }
}
refreshStatus(); setInterval(refreshStatus, 8000);

function setWorking(btnId, working) {
  const btn = document.getElementById(btnId);
  if (working) { btn._orig = btn.innerHTML; btn.innerHTML='<span class="spinner"></span>Working…'; btn.disabled=true; }
  else { btn.innerHTML = btn._orig||btn.innerHTML; btn.disabled=false; }
}

async function uploadAndFetch(url, formData) {
  const r = await fetch(url, {method:'POST', body: formData});
  if (!r.ok) { const t = await r.text(); throw new Error(t); }
  return r.json();
}

async function doTTS() {
  const text = document.getElementById('tts-text').value.trim();
  if (!text) return;
  setWorking('tts-btn', true);
  const res = document.getElementById('tts-result');
  const msg = document.getElementById('tts-msg');
  res.className = 'result show';
  msg.innerHTML = '<span class="spinner"></span>Generating…';
  try {
    const fd = new FormData();
    fd.append('text', text);
    fd.append('engine', document.getElementById('tts-engine').value);
    fd.append('save', 'true');
    const d = await uploadAndFetch(BASE+'/api/tts-upload', fd);
    msg.innerHTML = '<span class="ok">Done — ' + (d.size/1024).toFixed(0) + ' KB</span>';
    document.getElementById('tts-audio').src = BASE+'/api/audio?f='+encodeURIComponent(d.file);
  } catch(e) { msg.innerHTML = '<span class="err">Error: '+e.message+'</span>'; }
  setWorking('tts-btn', false);
}

async function doVideoTTS() {
  const file = document.getElementById('vid-file').files[0];
  if (!file) return;
  setWorking('vid-btn', true);
  const res = document.getElementById('vid-result');
  const msg = document.getElementById('vid-msg');
  res.className = 'result show';
  msg.innerHTML = '<span class="spinner"></span>Uploading & transcribing…';
  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('engine', document.getElementById('vid-engine').value);
    const d = await uploadAndFetch(BASE+'/api/video-to-tts-upload', fd);
    lastTranscript = d.transcript || '';
    document.getElementById('vid-transcript').textContent = lastTranscript;
    msg.innerHTML = '<span class="ok">Done — TTS ' + (d.tts_size/1024).toFixed(0) + ' KB</span>';
    document.getElementById('vid-audio').src = BASE+'/api/audio?f='+encodeURIComponent(d.tts_file);
  } catch(e) { msg.innerHTML = '<span class="err">Error: '+e.message+'</span>'; }
  setWorking('vid-btn', false);
}

async function doTranscribe() {
  const file = document.getElementById('stt-file').files[0];
  if (!file) return;
  setWorking('stt-btn', true);
  const res = document.getElementById('stt-result');
  const msg = document.getElementById('stt-msg');
  res.className = 'result show';
  msg.innerHTML = '<span class="spinner"></span>Transcribing…';
  try {
    const fd = new FormData();
    fd.append('file', file);
    const d = await uploadAndFetch(BASE+'/api/transcribe', fd);
    lastTranscript = d.transcript || '';
    document.getElementById('stt-transcript').textContent = lastTranscript;
    msg.innerHTML = '<span class="ok">Done in ' + d.duration_s + 's</span>';
  } catch(e) { msg.innerHTML = '<span class="err">Error: '+e.message+'</span>'; }
  setWorking('stt-btn', false);
}

function copyTranscript() {
  navigator.clipboard.writeText(document.getElementById('stt-transcript').textContent);
}

function sendToTTS() {
  document.getElementById('tts-text').value = document.getElementById('stt-transcript').textContent;
  switchTab('tts');
}
</script>
</body>
</html>"""


@app.route("/api/tts-upload", methods=["POST"])
def tts_upload():
    """Form-data version of /api/tts for browser uploads."""
    text   = (request.form.get("text") or "").strip()
    engine = request.form.get("engine", "pocket")
    if not text:
        return jsonify({"error": "no text"}), 400
    if engine == "fish":
        wav_bytes, err = call_fish_tts(text)
    else:
        wav_bytes, err = call_pocket_tts(text)
    if err:
        return jsonify({"error": err}), 502
    _status["tts_jobs"] += 1
    label = text[:30].replace(" ", "_").replace("/", "")
    fname = save_tts(wav_bytes, label)
    _status["last_tts_file"] = fname
    return jsonify({"file": fname, "size": len(wav_bytes)})


@app.route("/api/video-to-tts-upload", methods=["POST"])
def video_to_tts_upload():
    """Multipart file upload version of /api/video-to-tts for browser."""
    if "file" not in request.files:
        return jsonify({"error": "no file"}), 400
    f = request.files["file"]
    engine = request.form.get("engine", "pocket")
    tmp = tempfile.mktemp(suffix=os.path.splitext(f.filename)[1] or ".mp4", dir="/tmp")
    f.save(tmp)
    try:
        transcript, err = transcribe_file(tmp)
        if err:
            return jsonify({"error": f"transcribe failed: {err}"}), 500
        if not transcript:
            return jsonify({"error": "empty transcript"}), 422
        if engine == "fish":
            wav_bytes, err = call_fish_tts(transcript)
        else:
            wav_bytes, err = call_pocket_tts(transcript)
        if err:
            return jsonify({"transcript": transcript, "error": f"TTS failed: {err}"}), 502
        label = os.path.splitext(os.path.basename(f.filename))[0]
        tts_file = save_tts(wav_bytes, label)
        _status["transcriptions"] += 1
        _status["tts_jobs"] += 1
        _status["last_transcript"] = transcript[:200]
        _status["last_tts_file"] = tts_file
        return jsonify({"transcript": transcript, "tts_file": tts_file, "tts_size": len(wav_bytes)})
    finally:
        try: os.unlink(tmp)
        except: pass


@app.route("/api/audio")
def serve_audio():
    """Serve a generated WAV file by path."""
    path = request.args.get("f", "")
    if not path or not os.path.exists(path) or not path.startswith(TTS_OUT_DIR):
        return "not found", 404
    return send_file(path, mimetype="audio/wav")


@app.route("/api/status")
def status():
    return jsonify(_status)


@app.route("/api/transcribe", methods=["POST"])
def transcribe():
    """
    Body: {"path": "/opt/studio/media/video.mp4"}
    OR upload file as multipart: field "file"
    Returns: {"transcript": "...", "duration_s": 1.2}
    """
    t0 = time.time()

    # File upload path
    if "file" in request.files:
        f = request.files["file"]
        tmp = tempfile.mktemp(suffix=os.path.splitext(f.filename)[1] or ".mp4", dir="/tmp")
        f.save(tmp)
        path = tmp
        cleanup = True
    else:
        data = request.get_json(silent=True) or {}
        path = data.get("path", "").strip()
        cleanup = False

    if not path or not os.path.exists(path):
        return jsonify({"error": "file not found", "path": path}), 400

    text, err = transcribe_file(path)
    if cleanup:
        try: os.unlink(path)
        except: pass

    if err:
        return jsonify({"error": err}), 500

    _status["transcriptions"] += 1
    _status["last_transcript"] = text[:200]
    return jsonify({"transcript": text, "duration_s": round(time.time() - t0, 2)})


@app.route("/api/tts", methods=["POST"])
def tts():
    """
    Body: {"text": "...", "engine": "pocket"|"fish", "voice": "default", "save": true}
    Returns: WAV file if save=false, else {"file": "/path/to/tts.wav"}
    """
    data = request.get_json(silent=True) or {}
    text   = data.get("text", "").strip()
    engine = data.get("engine", "pocket")
    voice  = data.get("voice", "default")
    save   = data.get("save", True)

    if not text:
        return jsonify({"error": "no text"}), 400

    if engine == "fish":
        wav_bytes, err = call_fish_tts(text)
    else:
        wav_bytes, err = call_pocket_tts(text, voice)

    if err:
        return jsonify({"error": err}), 502

    _status["tts_jobs"] += 1

    if save:
        label = text[:30].replace(" ", "_").replace("/", "")
        fname = save_tts(wav_bytes, label)
        _status["last_tts_file"] = fname
        return jsonify({"file": fname, "size": len(wav_bytes)})
    else:
        tmp = tempfile.mktemp(suffix=".wav", dir="/tmp")
        with open(tmp, "wb") as f:
            f.write(wav_bytes)
        return send_file(tmp, mimetype="audio/wav", as_attachment=True,
                         download_name="tts_output.wav")


@app.route("/api/video-to-tts", methods=["POST"])
def video_to_tts():
    """
    Full pipeline: video/audio → transcript → TTS audio
    Body: {"path": "/opt/studio/media/clip.mp4", "engine": "pocket", "voice": "default"}
    Returns: {"transcript": "...", "tts_file": "/opt/studio/tts_output/xxx.wav"}
    """
    data = request.get_json(silent=True) or {}
    path   = data.get("path", "").strip()
    engine = data.get("engine", "pocket")
    voice  = data.get("voice", "default")

    if not path or not os.path.exists(path):
        return jsonify({"error": "file not found", "path": path}), 400

    # Step 1: transcribe
    transcript, err = transcribe_file(path)
    if err:
        return jsonify({"error": f"transcribe failed: {err}"}), 500
    if not transcript:
        return jsonify({"error": "empty transcript"}), 422

    # Step 2: TTS
    if engine == "fish":
        wav_bytes, err = call_fish_tts(transcript)
    else:
        wav_bytes, err = call_pocket_tts(transcript, voice)

    if err:
        return jsonify({"transcript": transcript, "error": f"TTS failed: {err}"}), 502

    label = os.path.splitext(os.path.basename(path))[0]
    tts_file = save_tts(wav_bytes, label)

    _status["transcriptions"] += 1
    _status["tts_jobs"] += 1
    _status["last_transcript"] = transcript[:200]
    _status["last_tts_file"] = tts_file

    return jsonify({
        "transcript": transcript,
        "tts_file":   tts_file,
        "tts_size":   len(wav_bytes),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9532, debug=False)
