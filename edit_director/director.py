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
GROQ_API_KEY   = os.environ.get("GROQ_API_KEY", "")
WHISPER_URL    = os.environ.get("WHISPER_URL",   "http://127.0.0.1:8421")
TTS_URL        = os.environ.get("TTS_URL",       "http://127.0.0.1:9532")
OPENCUT_URL    = os.environ.get("OPENCUT_URL",   "http://192.168.0.78:9500")
PROJECTS_DIR   = os.environ.get("PROJECTS_DIR",  "/opt/studio/projects")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT  = os.environ.get("TELEGRAM_CHAT_ID",   "7819702619")
DIRECTOR_HOST  = os.environ.get("DIRECTOR_HOST",  "192.168.0.78")
DIRECTOR_PORT  = int(os.environ.get("DIRECTOR_PORT", "9533"))
GROQ_MODEL     = "groq/compound-mini"

os.makedirs(PROJECTS_DIR, exist_ok=True)

# In-memory job registry
_jobs: dict = {}
_jobs_lock = threading.Lock()

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

# ── Whisper STT ──────────────────────────────────────────────────────────────
def run_whisper(video_path: str) -> dict:
    """Upload to :8421 Whisper service and return transcript dict."""
    import requests as req
    with open(video_path, "rb") as f:
        r = req.post(
            f"{WHISPER_URL}/transcribe",
            files={"file": (Path(video_path).name, f, "video/mp4")},
            timeout=300,
        )
    r.raise_for_status()
    return r.json()

# ── Video Analysis ────────────────────────────────────────────────────────────
def analyze_video(video_path: str, duration: float) -> dict:
    """FFprobe-based silence detection and basic analysis."""
    analysis = {"duration": duration, "silences": [], "audio_levels": [], "error": None}
    try:
        cmd = [
            "ffmpeg", "-i", video_path, "-af",
            "silencedetect=noise=-30dB:d=0.5",
            "-f", "null", "-"
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = r.stderr

        # Parse silence events
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

        # Get duration via ffprobe
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

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 8000,
        "stream": False
    }

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "MiniStudio/1.0"
    }

    r = req.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=180
    )
    r.raise_for_status()
    resp_json = r.json()
    choices = resp_json.get("choices", [])
    if not choices:
        raise ValueError(f"Groq returned no choices: {json.dumps(resp_json)[:200]}")
    raw_content = choices[0].get("message", {}).get("content")
    if not raw_content:
        # compound-mini sometimes puts answer in reasoning
        raw_content = choices[0].get("message", {}).get("reasoning", "")
        # Extract JSON from reasoning
        m = re.search(r'\{.*\}', raw_content, re.DOTALL)
        if m:
            raw_content = m.group(0)
        else:
            raise ValueError(f"Groq returned empty content. finish_reason={choices[0].get('finish_reason')}")
    print(f"[director] LLM raw response ({len(raw_content)} chars): {raw_content[:100]}...")

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

        # Step 8: TTS generation (optional — only if plan has tts entries)
        tts_entries = plan.get("tts", [])
        if tts_entries:
            update_step(jid, "tts", "running", f"Generating {len(tts_entries)} TTS segments...")
            tts_dir = pdir / "tts"
            tts_dir.mkdir(exist_ok=True)
            failed_tts = []
            for tts in tts_entries:
                tts_file = tts_dir / f"{tts['id']}.wav"
                try:
                    import requests as req
                    r = req.post(f"{TTS_URL}/api/tts",
                                 json={"text": tts["text"], "engine": "pocket"},
                                 timeout=120)
                    if r.status_code == 200:
                        tts_data = r.json()
                        tts_src = tts_data.get("file")
                        if tts_src and os.path.exists(tts_src):
                            shutil.copy(tts_src, tts_file)
                            tts["generated_file"] = str(tts_file)
                    else:
                        failed_tts.append(tts["id"])
                except Exception as e:
                    print(f"[director] TTS {tts['id']} failed: {e}")
                    failed_tts.append(tts["id"])

            if failed_tts:
                update_step(jid, "tts", "warn", f"Failed: {failed_tts}")
                with _jobs_lock:
                    _jobs[jid]["tts_failed"] = failed_tts
            else:
                update_step(jid, "tts", "done", f"{len(tts_entries)} segments generated")

            # Rebuild OpenCut project with TTS audio
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
  const body = { video_path: uploadedPath, instruction: instr, project_name: 'demo' };
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
    job = {
        "job_id": jid,
        "status": "running",
        "created_at": now_iso(),
        "source_path": video_path,
        "source_name": Path(video_path).name,
        "channel_id": data.get("channel_id", ""),
        "instruction": data.get("instruction", "Edit this video. Remove silences. Keep best content."),
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
    try:
        import requests as req
        r = req.post(
            f"{TTS_URL}/generate",
            json={"text": text, "language": "en"},
            headers={"User-Agent": "MiniStudio/1.0"},
            timeout=60
        )
        r.raise_for_status()
        return Response(r.content, mimetype="audio/wav")
    except Exception as e:
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
<div class="sub">Pipeline: SHA256 → STT → Analysis → Edit Plan → Validate → OpenCut → Preview | Port {DIRECTOR_PORT} &nbsp;·&nbsp; <a href="/demo" style="color:#a78bfa;text-decoration:none">🚀 Upload Demo Page →</a></div>

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

# ── Startup ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[director] AI Edit Director starting on :{DIRECTOR_PORT}")
    print(f"[director] Projects dir: {PROJECTS_DIR}")
    print(f"[director] Whisper: {WHISPER_URL}")
    print(f"[director] Groq model: {GROQ_MODEL}")
    app.run(host="0.0.0.0", port=DIRECTOR_PORT, debug=False)
