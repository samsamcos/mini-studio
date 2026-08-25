# Mini Studio (CT103)

Full-auto video/audio studio on CT103 Proxmox LXC at `192.168.0.10` (container 103).

Dashboard: `http://192.168.0.10:85`

## One-Command Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/samsamcos/mini-studio/main/install.sh)
```

## Services

| Service | Port | Description |
|---------|------|-------------|
| `studio-dashboard` | 85 | Static dashboard (index.html) |
| `studio-auto` | 9530 | Auto pipeline — SMB drop → AI processing |
| `studio-watcher` | — | File watcher, triggers auto pipeline + Telegram alerts |
| `tts-stt-pipeline` | 9532 | TTS + STT API (Whisper + Pocket TTS + Fish TTS) |
| `channel-export` | 9510 | 3-channel audio export |
| `pocket-tts` | 5020 | Kyutai Pocket TTS voice cloning |
| `fish-tts` | 5022 | Fish Speech 1.5 TTS |
| `nodeagent` | 7070 | Node health agent |

## TTS/STT API — port 9532

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/status` | GET | Health + job counters |
| `/api/transcribe` | POST | Video/audio → Whisper transcript |
| `/api/tts` | POST | Text → WAV (Pocket TTS or Fish TTS) |
| `/api/video-to-tts` | POST | Video → transcript → TTS audio (full pipeline) |

### Examples

```bash
# Transcribe a video
curl -X POST http://192.168.0.10:9532/api/transcribe \
  -H 'Content-Type: application/json' \
  -d '{"path": "/opt/studio/inbox/clip.mp4"}'

# Text to speech (Pocket TTS, save to file)
curl -X POST http://192.168.0.10:9532/api/tts \
  -H 'Content-Type: application/json' \
  -d '{"text": "Hello world", "engine": "pocket", "save": true}'

# Full video → transcript → TTS pipeline
curl -X POST http://192.168.0.10:9532/api/video-to-tts \
  -H 'Content-Type: application/json' \
  -d '{"path": "/opt/studio/inbox/clip.mp4", "engine": "pocket"}'
```

## Layout

```
/opt/studio/
  dashboard/     ← Auto pipeline + watcher + static UI
    auto.py            Auto pipeline API (port 9530)
    watcher.py         File watcher (SMB inbox)
    auto.html          Auto pipeline UI
    index.html         Studio dashboard
    archive_browser.py Archive viewer
  pipeline/      ← TTS/STT API
    tts_stt_pipeline.py   (port 9532)
  channel_export.py  ← 3-channel audio export (port 9510)
  tts_output/    ← Generated TTS WAV files
  inbox/         ← SMB drop folder (watcher picks up here)
  whisper_models/ ← Local Whisper model cache
  venv/          ← Python venv
  .env           ← API keys and config
```
