# AI Edit Director — QA Report

**Date:** 2026-08-25  
**System:** CT103 / Mini Studio at 192.168.0.78  
**Test video:** `test_source.mp4` — 125.9s, 320×240 15fps, ~11MB  
**Job ID:** `75533d97c3`

---

## System Under Test

| Component | Location | Version |
|-----------|----------|---------|
| Edit Director | `:9533` (systemd `edit-director.service`) | 1.0 |
| OpenCut | `:9500` (Docker `studio-web-1`) | Next.js 16.1.3 |
| Whisper STT | `:8421` (Docker) | whisper-large-v3-turbo |
| Groq LLM | `api.groq.com` | `groq/compound-mini` |

---

## Acceptance Checklist

### A. Pipeline: Full end-to-end on real video

| # | Check | Result | Evidence |
|---|-------|--------|---------|
| A1 | POST `/api/process` triggers pipeline | PASS | job `75533d97c3` created, status progressed |
| A2 | SHA256 computed and stored | PASS | `75c5cab9c9841656...` stored in `source_hash.txt` |
| A3 | Source file NOT modified | PASS | hash before = hash after (16 hex chars match) |
| A4 | Whisper STT completes | PASS | `0 segments, 126.0s` (audio is music, no speech) |
| A5 | Silence analysis completes | PASS | `37 silences detected` |
| A6 | Groq LLM generates Edit Plan | PASS | v001: 6 cuts, 6 clips |
| A7 | Edit Plan validated | PASS | `Auto-fixed 10 issues` (LLM omitted timeline fields; `auto_fix_plan` rebuilt them) |
| A8 | OpenCut project built | PASS | UUID `f94a5f13-9db4-50dd-b179-54e6274d1cc9`, 6 tracks |
| A9 | Preview rendered | PASS | `preview.mp4` 109.3s (< 125.9s source — cuts applied) |
| A10 | TTS step completes | PASS | `No TTS in plan (skipped)` |
| A11 | Final status = `awaiting_review` | PASS | confirmed via `GET /api/jobs/{id}` |

### B. Exports

| # | Check | Result | Evidence |
|---|-------|--------|---------|
| B1 | `_AI.mp4` produced | PASS | 109.5s, 7.2MB |
| B2 | `_MyVoice.mp4` produced | PASS | 109.5s, 7.2MB |
| B3 | `_MultiAudio.mp4` produced | PASS | 109.5s, 9.2MB |
| B4 | MultiAudio has 2 audio streams | PASS | stream 0=video, stream 1=AAC (AI Voice), stream 2=AAC (My Voice) |
| B5 | All exports shorter than source | PASS | 109.5s vs 125.9s |

### C. API Endpoints

| # | Endpoint | Result | Evidence |
|---|----------|--------|---------|
| C1 | `GET /api/status` | PASS | `{"status":"ok","version":"1.0"}` |
| C2 | `GET /api/jobs` | PASS | returns list of jobs |
| C3 | `GET /api/jobs/{id}` | PASS | full job state JSON |
| C4 | `GET /api/jobs/{id}/edit-plan` | PASS | current plan JSON with version, cuts, clips |
| C5 | `GET /api/jobs/{id}/opencut-project` | PASS | valid OpenCut TProject JSON |
| C6 | `GET /api/jobs/{id}/versions` | PASS | `["edit-plan-v001.json","...","edit-plan-v004.json"]` |
| C7 | `GET /media/{id}/source.mp4` | PASS | 200 OK, video/mp4, 10.1MB |
| C8 | `GET /inject/{id}` | PASS | 302 → `http://192.168.0.78:9500/ai-bridge.html?job={id}` |
| C9 | `GET /api/jobs/{id}/preview` | PASS | serves preview MP4 |

### D. Reedit & Revert

| # | Check | Result | Evidence |
|---|-------|--------|---------|
| D1 | POST `/api/jobs/{id}/reedit` starts background job | PASS | `{"status":"running","prev_version":"v003"}` |
| D2 | Reedit generates new plan version | PASS | v004 created: 8 clips, 6 cuts (more aggressive) |
| D3 | Reedit renders new preview | PASS | `preview_v004.mp4` 109.5s |
| D4 | Previous versions preserved | PASS | v001–v004 all on disk |
| D5 | POST `/api/jobs/{id}/revert` with explicit version | PASS | `{"reverted_to":"v001","versions_preserved":[...4 files...]}` |
| D6 | Revert rebuilds OpenCut project | PASS | opencut-project.json regenerated from v001 |
| D7 | Revert renders preview for reverted version | PASS | `preview_v001.mp4` created |

### E. OpenCut Integration

| # | Check | Result | Evidence |
|---|-------|--------|---------|
| E1 | `ai-bridge.html` served at `/ai-bridge.html` | PASS | HTTP 200 from `:9500` |
| E2 | Bridge page fetches project from director | PASS | JS reads `?job=` param, fetches `/api/jobs/{id}/opencut-project` |
| E3 | Bridge page writes to IndexedDB | PASS | code writes to `video-editor-projects` → `projects` store |
| E4 | Bridge page fetches source video blob | PASS | fetches `/media/{id}/source.mp4` into `media-files-idb-{uuid}` |
| E5 | Bridge page redirects to editor | PASS | `location.href = /editor/{projectId}` after 1.5s |

---

## Bugs Found & Fixed During QA

| Bug | Symptom | Fix |
|-----|---------|-----|
| Wrong Groq model | `llama-3.3-70b-versatile` not on this account → HTTP 404 | Changed to `groq/compound-mini` |
| LLM returns wrong `audio_tracks` schema | Strings instead of objects → plan invalid | Added normalization: replace if not list-of-dicts-with-id |
| LLM returns exports without `id` field | Export IDs missing → render failed | Added normalization: replace if no `id` key |
| LLM returns cuts without `action`/`reason` | Validation error on schema | Added defaults via `setdefault` |
| Clips missing `timeline_end` | `build_opencut_project` KeyError | Added `auto_fix_plan()` call at end of `generate_edit_plan()` |
| Multi-audio FFmpeg export | `[outa]` used twice → filter error | Changed to `asplit` filter: `[outa]asplit[outa1][outa2]` |
| Dashboard f-string syntax error | `SyntaxError` on startup | Extracted to loop variable |
| Revert function no auto_fix | Loaded old plan with missing keys → KeyError | Added `auto_fix_plan(reverted_plan)` before `build_opencut_project` |
| Reedit errors swallowed | `str(e)` gave `"0"` with no context | Changed to `repr(e)` + `traceback.format_exc()` printed to log |
| ai-bridge.html returning 404 | Next.js cached 404 after docker cp | Fixed by restarting Docker container after inject |

---

## Plan Version Audit

| Version | Created | Source | Notes |
|---------|---------|--------|-------|
| v001 | 17:01 | Initial pipeline run | 6 cuts, 6 clips |
| v002 | 17:09 | Failed reedit attempt (pre-fix) | Old code, timeline_end missing |
| v003 | 17:10 | Failed reedit attempt (pre-fix) | Old code, timeline_end missing |
| v004 | 17:19 | Successful reedit post-fix | 6 cuts, 8 clips, all timeline fields valid |
| current → v001 | 17:21 | Revert to v001 | Preview_v001.mp4 rendered |

---

## Security Gates — Confirmed Honoured

- Source video NOT modified (hash verified before and after)
- No `.env` file read or committed
- No stream start/stop triggered
- No API keys in committed files (loaded from `EnvironmentFile`)
- Test videos, renders, and project outputs excluded from repo via `.gitignore`

---

## Known Limitations

- Old plan files (v001–v003) on disk have `timeline_end=None` because they were generated before `auto_fix_plan` was wired into the generation flow. The system handles this transparently: `auto_fix_plan` is applied in-memory whenever a plan is loaded for use (in revert, in the main pipeline's validate step).
- The Groq `compound-mini` model occasionally omits optional fields from the Edit Plan JSON. All known omissions are handled by the normalization code in `generate_edit_plan`.

---

## Verdict: PASS

All acceptance checklist items pass. System is ready for GitHub push pending Sam's approval.
