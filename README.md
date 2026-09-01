# Mini Studio — Node 4

**Raw recording in. Editable OpenCut project out. ~98% of the work done before you open the editor.**

Node 4 is a dedicated offline editing box that runs CPU-only as a VM on an Unraid host. It never touches the live-stream chain — Nodes 1–3 have zero dependency on it. Drop a video file on it, pick a character voice, press one button, and walk away. When the job finishes you have a complete OpenCut timeline: the best moments picked and timed, dead air cut, bad takes flagged, AI narration drafted and spoken in the character's voice, captions styled and word-timed for karaoke animation, chapter markers, thumbnails, and a 9:16 vertical alongside the widescreen version.

The final 2% is yours: watch it, move anything you don't like, and export.

---

## At a glance

```
You drop a file                You open the editor
       ↓                              ↓
  recording.mp4    →→→  Node 4  →→→  timeline.json
                      (14 stages)
                     transcribe
                     dead air out
                     bad takes flagged
                     best 2% scored
                     script cleaned
                     AI voice generated
                     audio processed
                     captions timed
                     chapters set
                     thumbnails picked
                     9:16 cut
                     OpenCut project built
```

---

## What's running right now

| Port | Service | Status |
|---|---|---|
| `:9545` | **Auto-Edit orchestrator** | Active |
| `:9544` | **Highlight Extractor** | Active |
| `:9543` | **Gemini vision** | Active |
| `:9542` | **Script polish (Gemma)** | Active |
| `:9541` | **STT consensus (Qwen)** | Active |
| `:9540` | **Studio settings panel** | Active |
| `:5020` | **Pocket TTS** | Active — 3.3× realtime |
| `:9500` | **OpenCut editor** | Active (Docker) |
| `:85`   | **Dashboard / home** | Active |
| `:81`   | **CasaOS** | Active |

**On-demand LLMs** — started automatically on first request, stopped after 10 min idle:

| Port | Model | Size | Role |
|---|---|---|---|
| `20131` | Qwen 2.5 3B Q4_K_M | ~2 GiB | STT consensus |
| `8082`  | Gemma 2 9B Q4_K_M  | ~5.5 GiB | Script polish |

---

## The Auto-Edit Pipeline — all 14 stages

### Stage 1 — Probe

`ffprobe` reads duration, resolution, bitrate, and codec from the source file before anything else starts. A 4K/64 Mbps source is handled correctly throughout — preview clips are capped at 1080p to keep disk usage sane, but every reference in the final timeline.json points at the original file, so you always edit at full quality.

The probe data goes into the job record and is shown in the status panel: you can see source duration, resolution, and bitrate before any processing finishes.

### Stage 2 — Transcribe (word-level)

`faster-whisper small` (int8, CPU-only) runs with `vad_filter=True` and `word_timestamps=True`. Every word gets its own start and end timestamp. These timings flow into four downstream consumers:

- **Scoring** — words-per-second is one of the three scoring axes
- **Bad-take detection** — pattern-matches on words and their timing
- **Captions** — word-level timing drives the karaoke pop animation
- **Chapter detection** — gaps in speech become chapter boundaries

Output: `transcript.json` — array of segments, each with full text and a `words` array of `{word, start, end}` objects.

The `small` model strikes the right balance for CPU: fast enough that a 20-minute recording finishes before you've made a coffee, accurate enough that the transcript is usable without manual fixes.

### Stage 3 — Dead Air Removal

`ffmpeg silencedetect` runs on the full source with a noise floor of −32 dB and a minimum silence duration of 0.55 s. Every gap is recorded as a (start, end) pair. A 0.18 s pad is applied on each side before inverting to speaking ranges — this keeps natural breath pauses and preserves the rhythm of speech rather than cutting it up into choppy fragments.

The job reports total dead air removed. On a typical 20-minute vlog this is often 3–5 minutes — content that would otherwise drag the pacing down.

### Stage 4 — Bad Take Detection

Five patterns that signal a take went wrong:

**Repeated words** — the same word or phrase appearing within a 3-word window. `"I want to, I want to say"` — that gets flagged.

**Stutters** — a word appears twice in immediate succession. `"the the camera"` — flagged.

**Filler overload** — more than 28% of the words in a segment are filler (um, uh, like, you know, sort of, basically, right). A segment that's 30% filler is a segment the speaker wasn't happy with either.

**Restarts** — explicit restart markers in the transcript: "let me start again", "actually wait", "hang on", "cut that".

**Fragments** — segments under 1.5 seconds with fewer than 4 words. These are almost always orphaned after a restart — the speaker had already moved on.

Every flagged take writes an entry to `bad_takes.json` with the time, the text, and the reasons. Bad take windows are excluded from highlight scoring so they can't accidentally end up in the edit. They also appear as **red markers** in the OpenCut timeline — you can see exactly where each one is and decide whether to keep or remove it.

### Stage 5 — Loudness Curve

`ffmpeg ebur128` measures momentary loudness (M value) every 0.1 seconds across the full recording. This produces a time-indexed loudness array that the scoring stage z-scores against all windows.

> **Critical implementation note:** The ebur128 filter prints lines like `t: 0.0999  TARGET:-23 LUFS    M:-41.2 S:-41.4 I:-70.0 LRA:...`. The `TARGET:-23 LUFS` token sits between the timestamp and the `M:` reading, and `M:` has no space after the colon. A regex that expects `M:` to follow immediately after `t:` matches nothing. Every window silently defaults to −70 LUFS and loudness contributes nothing to the score without any error or warning. Correct pattern: `t:\s*([\d.]+).*?M:\s*(-?[\d.]+)`

### Stage 6 — Highlight Scoring

A sliding window (default 8 s length, 2 s stride) moves across the full recording. Each window gets a score:

```
score = 0.45 × z(loudness) + 0.35 × z(speech_density) + 0.20 × keyword_hits
```

**Loudness** (`z(loudness)`): z-scored across all windows. The per-window value is the mean of the **top half** of samples in that window. Averaging everything including the natural pauses would penalise a great delivery that happened to include a breath. Using the top half focuses on the peaks — the moments the speaker was actually speaking with energy.

**Speech density** (`z(speech_density)`): words per second, z-scored. Dense, engaged speech scores higher than halting, uncertain speech.

**Keyword hits**: counts of domain-relevant words in the window. Defaults cover the most common content patterns (reaction, tutorial, commentary). You can override the keyword list per job.

Windows dominated by a bad take (more than 50% overlap with a flagged segment) are excluded from scoring before the pick phase runs.

**Non-overlapping selection**: windows are ranked by score, then picked greedily — highest score first, skip anything that overlaps an already-chosen window — until the keep-% budget is spent or the clip limit is hit. Default: 2% of total recording duration, maximum 8 clips.

For each picked window, the next-best non-overlapping alternative is pre-computed and stored. This is the **swap** feature — if you open the Highlight Extractor and clip 3 looks wrong, one click replaces it with its alternate with no re-analysis.

### Stage 7 — Script Polish (source-grounded)

The transcript text from the selected clips is sent to Gemma 2 9B for cleanup. This is not a creative writing task — it's a disciplined cleanup task.

**The grounding system:**

Temperature is set to **0.1** — near-deterministic output that follows the source rather than inventing around it. The system prompt carries five explicit rules:

1. *Do not invent facts, metaphors, disasters, characters, or narrative arcs*
2. *Use only facts directly stated in the source transcript and video notes*
3. *Insert `...` for short pauses and `--` for dramatic shifts*
4. *Hard word limit — must be under N words total*
5. *Return only the spoken line — no conversational wrapper or quotes*

The word limit N is derived from the clip duration: `(total_clip_seconds − 1.0) × 2.3`. Pocket TTS speaks at approximately 2.3 words per second. The 1.0 s margin covers the `...` and `--` cadence pauses without letting narration run over the available footage. `max_tokens` is set to `N × 3` to prevent the model from running long.

> **Why this matters:** At temperature 0.7 with an open creative prompt ("documentary narrator, rewrite into polished narration"), Gemma invented a narrative about honey, fire, and dramatic metaphors for a source video that was about grocery budgets and monthly spending. The speaker never mentioned honey or fire. At temperature 0.1 with strict anchoring rules, the same Gemma 2 9B model cleans the speech and stays entirely within the source.

Output: `script.txt`

### Stage 8 — Text-to-Speech

The polished script is sent to **Pocket TTS** (Kyutai Moshi) at `:5020`. The character's `voice` field determines which voice model is used. Current performance: **6.6 seconds of audio in 2.0 seconds = 3.3× realtime** on CPU, no GPU required.

> **API detail:** Pocket TTS takes multipart form-data (`text`, `voice_url`), not JSON. Sending a JSON body returns 200 with empty audio. This is not documented anywhere obvious.

> **Duration detail:** The WAV length header written by this version of Pocket TTS is unreliable. Always use `ffprobe` to measure actual audio duration. `wave.getnframes() / framerate` gives the wrong value and will break any timing logic built on it.

### Stage 9 — Audio Cleanup

The raw narration WAV is processed through a three-stage chain:

1. **Highpass filter at 80 Hz** — cuts low rumble, HVAC hum, and mic handling noise
2. **afftdn** — neural network noise suppression, removes background hiss and room tone
3. **loudnorm** `I=-16 TP=-1.5 LRA=11` — broadcast-standard integrated loudness normalisation. −16 LUFS is the standard for YouTube and most streaming platforms.

Output: `narration_clean.m4a`

### Stage 10 — Timing Fit

After audio cleanup the actual narration duration is measured. If the narration runs longer than the total selected clip footage, the last video clip's `trimEnd` is extended to cover the overage plus a 0.5 s tail pad. The footage continues playing under the end of the narration rather than the audio playing over a black frame.

This is a timeline-only change — no re-encoding, no new files. The trimEnd value in `timeline.json` is adjusted. You can drag it back in the editor if you prefer a tighter cut.

The word budget from Stage 7 is designed to prevent this from being needed most of the time. The trim extension is a safety net for the cases where TTS speaking rate varies from the 2.3 wps estimate.

### Stage 11 — Captions

Whisper word timestamps are rebased from the original source timeline onto the cut timeline. Each segment's `start` and `end` are offset by the clip's position in the edit, and every word's timing is adjusted to match. This means caption timings stay in sync with the audio even after dead air has been removed and clips have been reordered.

Two caption formats are written:

**`captions.srt`** — standard SRT for any player or platform. Each segment is one subtitle block.

**`captions.ass`** — Advanced SubStation Alpha with the character's full style applied: font, size, primary colour, outline colour, outline weight, shadow, bold, alignment, and vertical margin. This is the format to use when you want the captions to look exactly like the character's style, including in ffmpeg burn-in workflows.

The timeline's caption track carries `wordTimings` arrays on each text element — OpenCut uses these for karaoke-style word highlighting as the narration plays.

### Stage 12 — Chapters

Gaps between speech segments longer than 6 seconds become chapter boundaries, up to a maximum of 12 chapters. Each chapter gets the first few words of the following segment as its title. Output:

**`chapters.txt`** — YouTube-format chapter markers (`0:00 Introduction`, `2:34 Main point`, etc.)

Chapter objects also appear in `timeline.json` for OpenCut's chapter panel.

### Stage 13 — Thumbnails

`ffmpeg thumbnail=n=100` scans the source at 1 frame per 100 frames, then picks the 6 most visually distinct frames using scene-change scoring. Output: `thumbs/thumb_001.jpg` through `thumb_006.jpg`.

These are candidates for manual selection — the pipeline doesn't choose the final thumbnail for you. On a talking-head video the 6 frames will typically cover different expressions and angles; pick the strongest one.

### Stage 14 — Timeline Assembly

All outputs are assembled into a single `timeline.json` that OpenCut loads directly.

**The critical design decision here:** video elements reference the original source file with `trimStart` and `trimEnd` values, not pre-cut segments. This means:

- **No re-encoding** to produce the edit — the original quality is always intact
- **Trim handles work in the editor** — if a cut feels 2 seconds too short, drag the handle in OpenCut rather than re-running the pipeline
- **4K sources stay 4K in the edit** even though preview clips are 1080p

Timeline structure:

```
timeline.json
  └─ scenes
       └─ scene-main
            ├─ tracks
            │    ├─ Video (isMain: true)
            │    │    └─ elements: one per clip
            │    │         trimStart: source position
            │    │         trimEnd: source tail
            │    │         transform.scale: 1.0 or 1.08 (zoom plan)
            │    │         muted: true (when narration exists)
            │    ├─ AI Narration
            │    │    └─ narration_clean.m4a element
            │    └─ Captions
            │         └─ elements with wordTimings arrays
            └─ markers: bad takes (red), chapters
```

### 15 — 9:16 Vertical Cut

Preview clips are cut from the source (capped at 1080p), concatenated into a reel, then converted to 1080×1920.

Two modes controlled by the character's `vertical_mode`:

**`centre`**: `scale=-2:1920, crop=1080:1920` — simple centre crop. Works well for content where the subject is centred.

**`blur`**: background is scaled to **cover** the full 1080×1920 frame (not fit — fit leaves missing rows), then blurred with boxblur radius 24, then the sharp 1080-wide foreground is overlaid centred.

> **Cover vs fit:** Using `scale=1080:-2` for the background gives a 1080×608 image. Then `crop=1080:1920` tries to extract 1920 rows from a 608-row image — ffmpeg exits with code 1 and writes a 0-byte file, silently, because the file handle was opened before the error. The background must use `force_original_aspect_ratio=increase` (COVER) before the crop.

---

## Fault Tolerance

Every stage is wrapped by a `soft()` helper:

```python
def soft(name, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as e:
        job.setdefault('warnings', []).append(f'{name}: {e}')
        return None
```

A Gemini quota hit, a TTS timeout, a missing ffmpeg codec — none of these kill the job. The failure is recorded in `job['warnings']` and the pipeline carries on with whatever it has. You might get a job with no 9:16 version because ffmpeg wasn't installed, but you'll still get the transcript, the timeline, and the captions.

---

## Characters

Each character is a complete editing personality. Picking a character sets the voice, the narration tone, the caption style, and the vertical crop mode.

| Field | What it controls |
|---|---|
| `voice` | Pocket TTS voice URL |
| `tone` | Passed to the script polish as style guidance |
| `caption_style.font` | Caption font family |
| `caption_style.size` | Font size in points |
| `caption_style.primary` | Word highlight colour (karaoke pop) |
| `caption_style.outline_col` | Outline colour |
| `caption_style.outline` | Outline weight (pixels) |
| `caption_style.shadow` | Drop shadow on/off |
| `caption_style.bold` | Bold on/off |
| `caption_style.align` | ASS alignment code (2 = bottom centre) |
| `caption_style.margin_v` | Vertical margin from edge |
| `zoom_every` | Clips between zoom-scale changes |
| `vertical_mode` | `centre` or `blur` |

### Built-in characters

**JARVIS** — corporate, precise, confident. Clean white captions, blue highlights, tight outline. Suited to tutorials, product demos, and professional content where authority matters.

**ARIA** — warm, energetic, conversational. Yellow highlights, larger font, looser margins. Suited to lifestyle, personality, and entertainment content where energy and connection matter.

**Narrator** — neutral documentary style. Clean white, minimal styling, centre alignment. Suited to factual content, explainers, and any video where the visuals should do more work than the captions.

### Adding a character

Via API:

```bash
curl -X POST http://192.168.0.19:9545/characters/mychannelvoice \
  -H 'Content-Type: application/json' \
  -d '{
    "label": "My Channel",
    "voice": "http://192.168.0.19:5020/voices/custom",
    "tone": "casual and direct, like talking to a mate",
    "caption_style": {
      "font": "Montserrat-Bold",
      "size": 58,
      "primary": "#FF6B00",
      "outline_col": "#000000",
      "outline": 2,
      "shadow": true,
      "bold": true,
      "align": 2,
      "margin_v": 80
    },
    "zoom_every": 3,
    "vertical_mode": "blur"
  }'
```

Or through the settings panel at `:9540`.

---

## Channel Style Profiles

Per-channel editing dials stored in SQLite at `/opt/studio/projects/channel_profiles.db`. Separate from character voices — a character profile controls how the edit sounds and looks, a channel profile controls how the engine makes decisions.

The idea: each channel you run has different editing preferences. One channel uses tight cuts and lots of zoom. Another is slower-paced with wider caption margins. Rather than adjusting settings per-job, you set the dials once per channel and the engine uses them on every subsequent run for that channel.

| Dial | Default | Controls |
|---|---|---|
| `silence_threshold_db` | −30.0 | What counts as silence for dead air removal |
| `min_silence_duration` | 0.5 s | Minimum gap length to remove |
| `max_highlight_seconds` | 30.0 s | Cap on total kept footage regardless of percent |
| `words_per_second` | 2.3 | TTS rate used for word budget calculation |
| `zoom_frequency_hz` | 0.2 | How often zoom changes (per second of edit) |
| `caption_font` | Montserrat-Bold | Override the character's caption font |
| `caption_color` | #FFFFFF | Override the character's caption colour |
| `tts_voice_id` | narrator | Default character for this channel |
| `intro_hook_style` | question | Hook format: question / statement / statistic |
| `cut_frequency` | medium | slow / medium / fast — affects clip stride |
| `music_style` | none | Music mood (for when a music library is wired in) |
| `thumbnail_style` | face | face / text-overlay / split — selection preference |

---

## On-Demand Model Management

Two large models are used by the pipeline — Qwen 2.5 3B and Gemma 2 9B. With both resident the VM used **7.3 GiB with 208 MiB free and the system reaching into swap**. On demand it idles at **~0.8 GiB with 10 GiB available**.

The on-demand system in `ondemand.py`:

1. Any service that needs a model calls `ensure(unit_name, port)`
2. `ondemand` checks if the llama-server is already responding on that port
3. If not, `systemctl start <unit>` fires
4. The function polls `http://127.0.0.1:<port>/health` every 2 seconds until it gets 200
5. Qwen comes up in ~14 seconds. Gemma in ~25 seconds. Both are within the Flask timeout.
6. A background reaper runs every 60 seconds. Any model idle for `MODEL_IDLE_SECONDS` (default 600 = 10 minutes) gets stopped.

The reaper records `_last_use[unit] = (time, port)`. `touch(unit, port)` updates this timestamp on every completed request, so a second job arriving while the model is warm does not trigger a cold start.

Both llama units are `systemctl disable`d. They will not start on boot or restart if they crash — the on-demand system is the only thing that starts them.

---

## Smart Highlight Extractor (standalone)

The same scoring pipeline as Auto-Edit, exposed as a standalone tool with an interactive clip-swap interface. Use this when you want just the clips without running the full production chain.

```
POST   /highlight
  video_path      required — full path on Node 4
  percent         default 2 — percentage of total duration to keep
  clip_seconds    default 8 — target length per clip
  stride_seconds  default 2 — window advance step
  max_clips       optional — hard cap on clip count
  use_gemini      default false — tag clips via Gemini vision
  keywords        optional list — override default keyword set
  wait            default false — block until done if true

GET    /highlight/<id>           Status, selected clips, alternates
POST   /highlight/<id>/swap      {"index": N} — swap clip N for its ranked alternate
GET    /highlight/<id>/file/<name>   Download clip, reel, or transcript
GET    /browse                   List video files under /opt/studio (>1 MB, video extensions)
GET    /jobs                     Recent job list
GET    /                         Web UI
```

### The swap workflow

After scoring, for each picked clip the next-best non-overlapping window is stored as an alternate. If clip 3 landed on a bad moment, `POST /highlight/<id>/swap {"index": 2}` re-cuts that slot from the alternate with no re-analysis. The reel is rebuilt from the updated clip list. You can swap any clip as many times as there are alternates.

This is designed for the case where the algorithm picks technically high-scoring moments that aren't the right creative choice — loud moments that are off-topic, for example. Swap them out without waiting for a re-run.

---

## OpenCut Editor

Timeline editor at `:9500`. Built from the OpenCut open-source project on Next.js 16.1.3, running in Docker.

**Importing a generated timeline:**

1. Open `http://192.168.0.19:9500`
2. New project → Import → select `timeline.json` from the job output
3. Video clips load with trim handles pointing at the original source
4. Drag any handle to extend or shorten a cut — no re-encoding
5. Red markers show bad take positions in the ruler
6. Caption track has per-word timing for the karaoke animation
7. AI narration is on its own audio track — adjust volume or mute it if you're using your own voice (Version B)

**Version A vs Version B:**

- **Version A (AI voice):** source audio is muted in each clip element, narration track carries the AI voice. Everything is in the timeline already.
- **Version B (your own voice):** set `tts: false` when starting the job. The narration track is absent. Source audio is unmuted. Captions are still on the cut timeline. You record or drop in your own audio and place it on the narration track.

**Docker stack required:**

```bash
docker compose -f /opt/studio/docker-compose.slim.yml up -d --no-deps \
  web db redis serverless-redis-http
```

The `ai-backend` and `tts-service` images do not build and are not needed. Use `--no-deps` to bypass the `depends_on` constraint on `web`.

---

## Pocket TTS

Kyutai Moshi running on `:5020`.

Current measured performance: **3.3× realtime** — 10 seconds of narration in ~3 seconds.

**API (not JSON — this is important):**

```bash
curl -X POST http://192.168.0.19:5020/tts \
  -F "text=Your narration text here" \
  -F "voice_url=http://192.168.0.19:5020/voices/narrator" \
  -o narration.wav
```

Sending `Content-Type: application/json` returns 200 with empty or corrupted audio. The API is multipart form-data only.

After receiving the WAV, always measure duration with ffprobe:

```bash
ffprobe -v quiet -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 narration.wav
```

The WAV header written by this version of Pocket TTS is unreliable. The `wave` Python module's `getnframes() / framerate` calculation will give you the wrong duration and break any timing logic that depends on it.

---

## Gemini Integration (Tier 3)

Gemini 2.5 Flash is used for two things: tagging highlight clips with descriptions (via the Highlight Extractor's `use_gemini` option), and as a fallback for any task where Gemma's output isn't sufficient.

**Privacy:** raw footage is never uploaded. Every clip goes through a 480p / 1 FPS proxy before upload (~300 tokens per second of video). The remote file is deleted after every run, including on the failure path.

**Free tier limits:** 15 RPM · 1500 RPD · 1M TPM · 2 GB upload · 48 h retention.

**Retry behaviour:** 503 and quota errors get three retries with 15 s → 30 s → 45 s backoff.

---

## Setup (fresh machine)

### System dependencies

```bash
apt-get update
apt-get install -y ffmpeg python3 python3-pip python3-venv

# llama.cpp server — build from source or download a release binary
# Place at /usr/local/bin/llama-server
```

### Models

Place GGUF files at:

```
/opt/models/qwen2.5-3b-instruct-q4_k_m.gguf    # ~2 GiB
/opt/models/gemma-2-9b-it-q4_k_m.gguf          # ~5.5 GiB
```

### Python environment

```bash
python3 -m venv /opt/studio-env
source /opt/studio-env/bin/activate
pip install flask flask-cors requests faster-whisper google-genai
```

### Environment file

```bash
cd /opt/mini-studio
cp .env.example .env
# Edit .env and fill in:
#   GEMINI_API_KEY=...
#   GROQ_API_KEY=...
```

The `.env` file is gitignored. It is never committed. It holds live API keys and must not be pushed to any repository.

### Systemd services

```bash
cp /opt/mini-studio/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

# Services that run always
systemctl enable --now pocket-tts stt-consensus voiceover gemini-ingest \
  highlight autoedit studio-settings

# Models stay DISABLED — ondemand.py starts them on request
systemctl disable llama-qwen llama-gemma
```

### OpenCut

```bash
cd /opt/studio
docker compose -f docker-compose.slim.yml up -d --no-deps \
  web db redis serverless-redis-http
```

Verify: `curl -s -o /dev/null -w "%{http_code}" http://localhost:9500` → 200

---

## API Reference

### Auto-Edit — `:9545`

**Start a job**

```bash
curl -X POST http://192.168.0.19:9545/auto \
  -H 'Content-Type: application/json' \
  -d '{
    "video_path": "/opt/studio/incoming/recording.mp4",
    "character": "narrator",
    "percent": 2,
    "clip_seconds": 8,
    "max_clips": 8,
    "tts": true,
    "narrate": true,
    "vertical": true
  }'
```

Returns: `{"job_id": "20260901-014958-c848", "status_url": "/auto/20260901-014958-c848"}`

**Poll status**

```bash
curl http://192.168.0.19:9545/auto/20260901-014958-c848
```

**Status fields:**

```json
{
  "id": "20260901-014958-c848",
  "name": "recording.mp4",
  "character": "Narrator",
  "stage": "done",
  "progress": 100,
  "duration": 1243.0,
  "segments": 187,
  "dead_air_seconds": 214.3,
  "bad_takes": [ {"start": 41.2, "end": 48.7, "reasons": ["filler overload"]} ],
  "kept_seconds": 24.8,
  "kept_percent": 2.0,
  "narration_seconds": 9.4,
  "chapters": [ {"start": 0, "title": "Introduction"}, {"start": 312, "title": "Main point"} ],
  "thumbnails": ["thumb_001.jpg", "thumb_002.jpg", "thumb_003.jpg"],
  "reel": "reel.mp4",
  "vertical": "vertical.mp4",
  "timeline": "timeline.json",
  "warnings": []
}
```

**Download outputs**

```bash
curl -o timeline.json \
  http://192.168.0.19:9545/auto/20260901-014958-c848/file/timeline.json

curl -o narration.m4a \
  http://192.168.0.19:9545/auto/20260901-014958-c848/file/narration_clean.m4a
```

**Characters**

```bash
# List all characters
curl http://192.168.0.19:9545/characters

# Create or update a character
curl -X POST http://192.168.0.19:9545/characters/myvoice \
  -H 'Content-Type: application/json' \
  -d '{"label": "My Voice", "voice": "...", "tone": "...", "caption_style": {...}}'
```

**Recent jobs**

```bash
curl http://192.168.0.19:9545/jobs
```

---

### Highlight Extractor — `:9544`

```bash
# Start
curl -X POST http://192.168.0.19:9544/highlight \
  -H 'Content-Type: application/json' \
  -d '{"video_path": "/opt/studio/incoming/recording.mp4", "percent": 2}'

# Poll
curl http://192.168.0.19:9544/highlight/<id>

# Swap clip 2 for its alternate
curl -X POST http://192.168.0.19:9544/highlight/<id>/swap \
  -H 'Content-Type: application/json' \
  -d '{"index": 2}'

# Browse available videos
curl http://192.168.0.19:9544/browse
```

---

### Script Polish — `:9542`

```bash
curl -X POST http://192.168.0.19:9542/polish \
  -H 'Content-Type: application/json' \
  -d '{"raw_script": "um so today we are gonna talk about um budgets", "duration_seconds": 8.0}'

# Returns: {"polished_text": "Today -- budgets."}

# Check if Gemma is loaded
curl http://192.168.0.19:9542/model/status

# Manually unload to free RAM
curl -X POST http://192.168.0.19:9542/model/stop
```

---

## Project Layout

```
/opt/mini-studio/
  autoedit.py          Orchestrator — the full 14-stage pipeline
  autoedit.html        Auto-Edit web UI
  highlight.py         Standalone highlight extractor
  highlight.html       Highlight extractor web UI
  mediakit.py          All ffmpeg operations (zero LLM calls)
  voiceover.py         Script polish endpoint — Gemma on demand
  stt_consensus.py     Dual-STT merge — Qwen on demand
  gemini_ingest.py     Video understanding — Gemini API
  ondemand.py          On-demand model lifecycle management
  characters.py        Character profile operations
  characters.json      Character definitions (jarvis / aria / narrator)
  channel_profiles.py  Per-channel editing dials — SQLite
  settings_server.py   Settings panel and service control
  .env                 API keys — NEVER commit this file
  .env.example         Safe template for .env
  .gitignore           Excludes .env, __pycache__, *.pyc, model files
  systemd/             Unit files for all nine services
  README.md

/opt/studio/
  projects/            One folder per Auto-Edit job
    channel_profiles.db
    <job_id>/
      timeline.json
      transcript.json
      script.txt
      bad_takes.json
      captions.srt
      captions.ass
      chapters.txt
      narration.wav
      narration_clean.m4a
      reel.mp4
      vertical.mp4
      thumbs/
      clip_01.mp4 …
      job.json
  incoming/            Drop video files here
  whisper_models/      faster-whisper model cache
  docker-compose.slim.yml   OpenCut Docker stack
  apps/web/            OpenCut Next.js source
```

---

## mediakit.py — The Mechanical Layer

Every repeatable ffmpeg and file operation lives here. Zero LLM calls. This is the 80% of the work that runs identically every time regardless of what the AI stages produce. If you want to test any individual operation without running a full pipeline job, you can import this module and call any function directly.

| Function | Parameters | Returns |
|---|---|---|
| `probe(path)` | path | dict: duration, width, height, bitrate, codec |
| `detect_silence(path, noise_db=-32, min_silence=0.55)` | path | list of (start, end) tuples |
| `speaking_ranges(duration, silences, pad=0.18, min_keep=0.35)` | duration, silences | list of (start, end) kept ranges |
| `find_bad_takes(segments)` | transcript segments | list of flagged takes with reasons |
| `clean_audio(src, dst, denoise=True)` | paths | dst path |
| `write_srt(segments, path)` | segments, path | path |
| `write_ass(segments, path, style)` | segments, path, caption_style dict | path |
| `chapters_from_segments(segments, min_gap=6.0, max_chapters=12)` | segments | list of chapter dicts |
| `write_chapter_file(chapters, path)` | chapters, path | path |
| `to_vertical(src, dst, mode='centre', width=1080, height=1920)` | paths, mode | dst path |
| `thumbnail_frames(src, outdir, count=6)` | path, dir | list of thumbnail paths |
| `zoom_plan(clips, every=2)` | clips, frequency | list of scale values |

---

## Performance Benchmarks

Measured on Node 4 (CPU-only, no GPU):

| Stage | 73 s source | 20 min source (estimated) |
|---|---|---|
| Probe | <1 s | <1 s |
| Transcribe (Whisper small) | ~15 s | ~4 min |
| Silence detect | ~3 s | ~45 s |
| Bad take scan | <1 s | ~5 s |
| Loudness curve | ~8 s | ~2 min |
| Scoring + selection | <1 s | <1 s |
| Script polish (Gemma cold) | ~30 s | ~30 s |
| TTS (Pocket TTS) | ~3 s per 10 s audio | ~3 s per 10 s audio |
| Audio cleanup | ~2 s | ~2 s |
| Captions + chapters | <1 s | <1 s |
| Thumbnails | ~5 s | ~5 s |
| Timeline JSON | <1 s | <1 s |
| Preview clips + reel | ~10 s | ~2 min |
| 9:16 conversion | ~8 s | ~1–2 min |
| **Total (full job)** | **~225 s (3.75 min)** | **~15–20 min** |

The transcription and loudness stages dominate on long recordings. Both run in parallel with no dependency on each other — this is an optimisation opportunity for future versions.

---

## Troubleshooting

**Job reports `warnings` but completes**

This is expected behaviour. Each warning is a stage that failed gracefully. The job ran everything it could. Check the warning text — it usually names the stage and the exception.

**No reel or vertical in the output**

Either the clip-cutting stage failed (check for ffmpeg errors in warnings) or `cut_preview()` returned None (which would mean a code error — should not happen after the current fixes). Also check disk space: `df -h /opt/studio`.

**Script talks about something that wasn't in the recording**

This should not happen after the grounding fix (temperature 0.1, strict rules). If it does, the raw transcript text fed to polish was corrupted or the model is running an older cached version. Restart voiceover.service and check `/opt/mini-studio/voiceover.py` for the `build_grounded_voiceover_prompt` function.

**Narration is much longer than the footage**

The word budget is derived from `total_clip_seconds`. If `kept_seconds` is very small (under 5 s) the budget can be as low as 9–10 words — the model may ignore this at low temperatures. In that case the timeline-stretch fix extends the last clip to cover it. If you want tighter control, increase `percent` to keep more footage, or decrease `clip_seconds` to pick shorter clips and get more of them.

**OpenCut shows a blank screen at :9500**

Check `docker ps` — all four containers (web, db, redis, serverless-redis-http) must be up. If web restarted due to an OOM, it may need manual restart: `docker compose -f /opt/studio/docker-compose.slim.yml restart web`.

**TTS request times out**

Pocket TTS queues requests. If a job is already running TTS and another job starts, the second one waits. Timeout is set to 900 s. On a very long script this might not be enough — if it fails the job continues without narration and logs a warning.

**Model doesn't come up within 240 s**

The on-demand system waits up to 240 s for `/health` to return 200. On first start after a reboot, the model must also load from disk. If the VM is under memory pressure from another process (check with `free -h`), loading can take longer. Kill competing processes or stop the other VM.

---

## Host Notes

- **Do not edit the Unraid VM config** — allocations are managed by hand in the Unraid WebUI. Autostart is off on every VM and only one VM runs at a time.
- Node 4 is deliberately isolated from Nodes 1–3. It must never become a dependency for live output.
- The DNS fix is in place at `/etc/systemd/resolved.conf.d/dns.conf` and `/etc/docker/daemon.json`. The host's systemd-resolved stub (`127.0.0.53`) was not forwarding queries on first boot — this caused docker pulls, pip installs, and apt to fail with "server misbehaving" rather than a DNS error.
- Disk: 98 GB total. Preview clips at 1080p use ~24 MB per 5 seconds. A 20-minute recording with 2% kept produces roughly 6 preview clips and a reel — call it 200–300 MB per job. Monitor with `df -h /opt/studio`.

---

## What's Not Built Yet

| Feature | Notes |
|---|---|
| Background music | No music library connected. The channel profile has a `music_style` field ready. |
| B-roll footage | No stock footage library. A Pexels/Pixabay integration would slot into the scoring phase. |
| Hook / title / description | Straightforward to add once script quality is confirmed reliable. |
| Transitions | OpenCut has these natively — add between clips manually in the editor. |
| Gaming highlight detection | Kill-feed OCR and mic-loudness detector exist on the Stream PC. This is a port job, not new research. |
| Watch-folder ingest | Drop-and-click is the current workflow. A folder watcher to auto-start jobs is a small addition. |
| Channel profile UI | Schema and API are live. A settings panel tab needs wiring in. |
| Shorts-specific detection | Hook in first 3 s, fast cuts, high energy throughout — different scoring weights needed. |

---

## Quick Reference

```bash
# Start a job
curl -s -X POST http://192.168.0.19:9545/auto \
  -H 'Content-Type: application/json' \
  -d '{"video_path":"/opt/studio/incoming/recording.mp4","character":"narrator"}' \
  | python3 -m json.tool

# Poll until done
watch -n 5 'curl -s http://192.168.0.19:9545/auto/JOB_ID | python3 -m json.tool | grep stage'

# Download the timeline
curl -o timeline.json http://192.168.0.19:9545/auto/JOB_ID/file/timeline.json

# Check RAM
free -h

# Free Gemma RAM manually (auto-frees after 10 min idle)
curl -X POST http://192.168.0.19:9542/model/stop

# Check model status
curl http://192.168.0.19:9542/model/status

# Recent jobs
curl -s http://192.168.0.19:9545/jobs | python3 -m json.tool

# Restart a service after editing
systemctl restart autoedit
```
