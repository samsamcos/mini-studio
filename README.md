# Mini Studio — CT103

Full-auto AI video editing studio running on a Proxmox LXC (CT103, `192.168.0.78`).

Drop a video → AI transcribes → removes silences/fillers → generates edit plan → builds OpenCut project → renders preview → clones your voice → sends Telegram alert → open in browser to approve.

---

## Quick Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/samsamcos/mini-studio/main/install.sh)
```

Needs: Debian 12 LXC with Docker available. Prompts for Groq key + Telegram credentials.

---

## Services & Ports

| Service | Port | What it does |
|---------|------|-------------|
| `edit-director` | **9533** | Main AI edit pipeline — upload → transcript → LLM edit plan → preview → exports |
| `studio-dashboard` | **85** | Studio control panel (index.html) |
| `studio-auto` | **9530** | SMB drop-folder auto pipeline + Telegram conversation |
| `studio-watcher` | — | Watches `/opt/studio/inbox`, routes new files to Edit Director |
| `tts-stt-pipeline` | **9532** | TTS + STT API (Whisper + Pocket TTS) |
| `channel-export` | **9510** | 3-channel audio export |
| `nodeagent` | **7070** | Node health check |
| OmniRoute (Docker) | **127.0.0.1:20128** | Local LLM router — free, self-contained, no external dependency |

---

## Edit Director — port 9533

### How the pipeline works

```
SMB drop / demo upload
        │
        ▼
1. SHA256 hash  ──── dedup check
        │
        ▼
2. Whisper STT  ──── local faster-whisper (:8421) → Groq fallback
        │
        ▼
3. Silence analysis  ── ffmpeg volumedetect, adaptive threshold
        │
        ▼
4. AI Edit Plan  ─── OmniRoute (local) → Pollinations (free) → Groq (rush only) → defer
        │
        ▼
5. Validate + auto-fix timeline
        │
        ▼
6. Build OpenCut project JSON
        │
        ▼
7. Render preview.mp4
        │
        ▼
8. Pocket TTS  ──── Sam's voice clone WAV → voice_clone.wav
        │
        ▼
9. Telegram alert  ── "Ready: open in OpenCut"
        │
        ▼
10. Approve → 3 exports: _AI.mp4 / _MyVoice.mp4 / _MultiAudio.mp4
```

### LLM Cascade (cost order)

1. **OmniRoute** — runs locally on CT103 (`127.0.0.1:20128`, Docker). Zero external cost. Always first.
2. **Pollinations** — free public API (`text.pollinations.ai`). Fallback if OmniRoute is down.
3. **Groq** — only used if job is marked **rush** AND both free options failed. Uses quota.
4. **Defer** — if not rush and no free LLM is available, job is queued until tomorrow 09:00.

Set `priority` on a job:
- API: `"priority": "rush"` in the POST body
- Demo page: tick the **Rush** checkbox
- Auto-detect from instruction: words like `urgent`, `today`, `asap` → rush; `no rush`, `tomorrow` → defer

### STT Cascade

1. **Local Whisper** (`:8421`, faster-whisper) — free, punctuates natively (., !, ?) → perfect for TTS
2. **Groq Whisper** — fallback only if local fails

### TTS

- **Pocket TTS** (`http://192.168.0.235:5020`) — Sam's voice clone (`voice_clone.wav`), human-sounding, no API cost
- **XTTS** (`:8422`) — fallback if Pocket TTS unavailable

### 3 Exports

| File | Audio |
|------|-------|
| `{id}_AI.mp4` | Original + AI voice clone + music + SFX |
| `{id}_MyVoice.mp4` | Original + Sam's mic + music + SFX |
| `{id}_MultiAudio.mp4` | Both audio streams (dual AAC — pick in media player) |

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Redirect to `/demo` |
| `/demo` | GET | Upload demo page with live pipeline progress |
| `/api/status` | GET | Service health + job counts |
| `/api/process` | POST | Start a new job |
| `/api/jobs` | GET | List all jobs |
| `/api/jobs/<id>` | GET | Job status + step progress |
| `/api/jobs/<id>/edit-plan` | GET | Current edit plan JSON |
| `/api/jobs/<id>/approve` | POST | Approve → trigger 3 exports |
| `/api/jobs/<id>/reject` | POST | Reject edit plan |
| `/api/jobs/<id>/reedit` | POST | Re-run LLM with new instruction (no re-transcribe) |
| `/api/jobs/<id>/revert/<version>` | POST | Roll back to earlier plan version |
| `/api/channels` | GET/POST | Channel list |
| `/tts-preview` | POST | Test Pocket TTS voice clone |
| `/media/<id>/<path>` | GET | Serve project files (preview, exports) |

### Start a job

```bash
curl -X POST http://192.168.0.78:9533/api/process \
  -H 'Content-Type: application/json' \
  -d '{
    "video_path": "/opt/studio/inbox/clip.mp4",
    "instruction": "Remove all silences. Keep the best moments. Make it punchy.",
    "priority": "normal"
  }'
```

Response: `{"job_id": "abc123", "status": "running"}`

Poll status: `GET /api/jobs/abc123`

### Rush job

```bash
curl -X POST http://192.168.0.78:9533/api/process \
  -H 'Content-Type: application/json' \
  -d '{
    "video_path": "/opt/studio/inbox/clip.mp4",
    "instruction": "Edit this urgently — needs done today",
    "priority": "rush"
  }'
```

---

## OmniRoute (local LLM router)

Docker image: `diegosouzapw/omniroute:latest`  
Runs on `127.0.0.1:20128` — LAN-invisible, CT103 only.

Install (if not using `install.sh`):
```bash
docker run -d \
  --name omniroute \
  --restart unless-stopped \
  -p 127.0.0.1:20128:20128 \
  -e GROQ_API_KEY=<your_key> \
  diegosouzapw/omniroute:latest
```

The Edit Director always uses model `auto` — OmniRoute picks the best available free model.

---

## Voice Clone Setup

Drop Sam's reference WAV to:
```
/opt/studio/voices/voice_clone.wav
```

Requirements: any length, mono or stereo, WAV format. The Pocket TTS server at `.235:5020` uses it as the voice reference for every TTS segment in the edit plan.

Test it:
```bash
curl -X POST http://192.168.0.78:9533/tts-preview \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello, this is my cloned voice."}'
# Returns WAV audio
```

---

## File Layout

```
/opt/studio/
  edit_director/
    director.py          ← Edit Director (port 9533) — main AI pipeline
  dashboard/
    index.html           ← Studio control panel (port 85)
    auto.py              ← Auto pipeline API (port 9530)
    watcher.py           ← File watcher (SMB inbox → Edit Director)
    auto.html            ← Auto pipeline UI
    ai-bridge.html       ← OpenCut project loader (served from :9500)
    archive_browser.py   ← Archive viewer (port 9531)
  pipeline/
    tts_stt_pipeline.py  ← TTS + STT API (port 9532)
  channel_export.py      ← 3-channel audio export (port 9510)
  projects/              ← Per-job dirs (transcript, plan, preview, exports)
  voices/
    voice_clone.wav      ← Sam's voice reference for Pocket TTS
  inbox/                 ← SMB drop folder (watcher picks up here)
  logs/                  ← Service logs (edit-director.log, etc.)
  venv/                  ← Python venv
  .env                   ← API keys and config (see .env.example)
```

---

## .env Reference

Copy `.env.example` to `/opt/studio/.env` and fill in:

| Variable | Default | Description |
|----------|---------|-------------|
| `GROQ_API_KEY` | — | Groq API key (rush LLM + Whisper fallback) |
| `TELEGRAM_BOT_TOKEN` | — | Telegram bot token |
| `TELEGRAM_CHAT_ID` | `7819702619` | Your Telegram chat ID |
| `OMNIROUTE_URL` | `http://127.0.0.1:20128` | Local OmniRoute (leave as-is after install) |
| `POLLINATIONS_URL` | `https://text.pollinations.ai` | Pollinations free LLM |
| `WHISPER_URL` | `http://127.0.0.1:8421` | Local Whisper STT |
| `POCKET_TTS_URL` | `http://192.168.0.235:5020` | Pocket TTS with voice clone |
| `VOICE_WAV_PATH` | `/opt/studio/voices/voice_clone.wav` | Voice reference WAV |
| `TTS_URL` | `http://127.0.0.1:8422` | XTTS fallback |
| `OPENCUT_URL` | `http://192.168.0.78:9500` | OpenCut editor URL |
| `DIRECTOR_HOST` | `192.168.0.78` | This machine's IP |
| `DIRECTOR_PORT` | `9533` | Edit Director port |
| `PROJECTS_DIR` | `/opt/studio/projects` | Job storage |

---

## Logs

```bash
# Edit Director
tail -f /opt/studio/logs/edit-director.log

# All services
tail -f /opt/studio/logs/*.log

# OmniRoute
docker logs -f omniroute
```
