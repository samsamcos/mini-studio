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
    """Call Pocket TTS and return WAV bytes."""
    resp = requests.post(
        POCKET_TTS_URL + "/api/tts",
        json={"text": text, "voice": voice},
        timeout=30
    )
    if resp.status_code == 200:
        return resp.content, None
    return None, f"Pocket TTS {resp.status_code}"


def call_fish_tts(text):
    """Call Fish Speech TTS and return WAV bytes."""
    resp = requests.post(
        FISH_TTS_URL + "/v1/tts",
        json={"text": text, "format": "wav", "streaming": False},
        timeout=60
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
