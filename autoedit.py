"""Auto-Edit orchestrator — raw recording in, editable OpenCut project out.

Chains the pieces that already work into one job:

  transcribe (word-level) -> highlight scoring -> dead air -> bad takes
  -> script polish (Tier 2) -> TTS -> audio cleanup -> captions -> chapters
  -> thumbnails -> 9:16 -> OpenCut timeline.json

Every stage is individually wrapped: one failure degrades the result, it does
not kill the run. Whatever succeeded still lands in the project folder.

Clips reference the ORIGINAL media with trimStart/trimEnd rather than pre-cut
files, so you can drag a cut longer in the editor instead of re-running anything.
"""
import os, json, time, uuid, shutil, logging, threading, subprocess
import requests
from flask import Flask, request, jsonify, send_file, render_template_string

import mediakit
import characters
# pure scoring / ffmpeg helpers — no model is loaded by importing these
from highlight import (ffprobe_duration, loudness_curve, score_windows,
                       pick_non_overlapping, cut_clip, DEFAULT_KEYWORDS)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)

PROJECTS   = os.environ.get('AUTOEDIT_PROJECTS', '/opt/studio/projects')
WHISPER_DIR  = os.environ.get('WHISPER_MODELS', '/opt/studio/whisper_models')
WHISPER_SIZE = os.environ.get('WHISPER_SIZE', 'small')
POLISH_URL = os.environ.get('POLISH_URL', 'http://127.0.0.1:9542/polish')
TTS_URL    = os.environ.get('TTS_URL', 'http://127.0.0.1:5020/tts')
INSPECT_URL = os.environ.get('GEMINI_INGEST_URL', 'http://127.0.0.1:9543/inspect')

JOBS = {}
_model = None
_model_lock = threading.Lock()

os.makedirs(PROJECTS, exist_ok=True)
characters.ensure_file()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            log.info(f'Loading faster-whisper {WHISPER_SIZE}')
            _model = WhisperModel(WHISPER_SIZE, device='cpu', compute_type='int8',
                                  download_root=WHISPER_DIR)
        return _model


def transcribe_words(path):
    """Transcript with word-level timings — needed for karaoke captions."""
    segments, _ = get_model().transcribe(
        path, beam_size=1, vad_filter=True, word_timestamps=True,
        condition_on_previous_text=False)
    out = []
    for s in segments:
        words = [{'word': w.word.strip(), 'start': w.start, 'end': w.end}
                 for w in (s.words or []) if w.start is not None]
        out.append({'start': s.start, 'end': s.end,
                    'text': s.text.strip(), 'words': words})
    return out


# ------------------------------------------------------- OpenCut timeline

def _transform(scale=1.0):
    return {'scale': scale, 'position': {'x': 0, 'y': 0}, 'rotate': 0}


def build_timeline(name, source, source_dur, clips, zooms,
                   narration=None, narration_dur=0.0,
                   caption_segments=None, markers=None):
    """Emit a timeline matching apps/web/src/types/timeline.ts."""
    media = [{'id': 'src', 'name': os.path.basename(source),
              'type': 'video', 'path': source, 'duration': source_dur}]

    video_elements, playhead = [], 0.0
    for i, c in enumerate(clips):
        dur = round(c['end'] - c['start'], 3)
        video_elements.append({
            'id': f'v{i+1}', 'type': 'video', 'name': f'Clip {i+1}',
            'mediaId': 'src',
            'startTime': round(playhead, 3),
            'duration': dur,
            'trimStart': round(c['start'], 3),
            'trimEnd': round(max(0.0, source_dur - c['end']), 3),
            'sourceDuration': round(source_dur, 3),
            'transform': _transform(zooms[i] if i < len(zooms) else 1.0),
            'opacity': 1,
            'muted': bool(narration),          # duck source under AI narration
        })
        playhead += dur

    tracks = [{
        'id': 'track-video', 'name': 'Video', 'type': 'video', 'isMain': True,
        'muted': False, 'hidden': False, 'elements': video_elements,
    }]

    if narration:
        media.append({'id': 'narration', 'name': os.path.basename(narration),
                      'type': 'audio', 'path': narration, 'duration': narration_dur})
        tracks.append({
            'id': 'track-narration', 'name': 'AI Narration', 'type': 'audio',
            'muted': False, 'volume': 1.0,
            'elements': [{
                'id': 'a1', 'type': 'audio', 'name': 'Narration',
                'sourceType': 'upload', 'mediaId': 'narration',
                'startTime': 0.0, 'duration': round(narration_dur, 3),
                'trimStart': 0.0, 'trimEnd': 0.0,
                'sourceDuration': round(narration_dur, 3),
                'volume': 1.0,
            }],
        })

    if caption_segments:
        style = caption_segments['style']
        text_elements = []
        for i, seg in enumerate(caption_segments['segments']):
            local = seg['start']
            text_elements.append({
                'id': f't{i+1}', 'type': 'text', 'name': f'Caption {i+1}',
                'content': seg['text'],
                'startTime': round(seg['start'], 3),
                'duration': round(max(0.4, seg['end'] - seg['start']), 3),
                'trimStart': 0.0, 'trimEnd': 0.0,
                'fontSize': style.get('size', 54),
                'fontFamily': style.get('font', 'Arial'),
                'color': '#FFFFFF',
                'highlightColor': '#FFD400',
                'wordPopScale': 1.15,
                'wordTimings': [{'word': w['word'],
                                 'start': round(w['start'] - local, 3),
                                 'end':   round(w['end'] - local, 3)}
                                for w in seg.get('words', [])],
                'background': {'enabled': False, 'color': '#000000'},
                'textAlign': 'center',
                'fontWeight': 'bold' if style.get('bold') else 'normal',
                'fontStyle': 'normal', 'textDecoration': 'none',
                'transform': _transform(), 'opacity': 1,
            })
        tracks.append({'id': 'track-captions', 'name': 'Captions',
                       'type': 'text', 'hidden': False, 'elements': text_elements})

    now = time.time() * 1000
    return {
        'version': 1,
        'name': name,
        'media': media,
        'scenes': [{
            'id': 'scene-main', 'name': 'Main', 'isMain': True,
            'tracks': tracks,
            'bookmarks': [],
            'markers': markers or [],
            'createdAt': now, 'updatedAt': now,
        }],
    }


# ------------------------------------------------------------------ the job

def run(job_id, opts):
    job = JOBS[job_id]
    proj = os.path.join(PROJECTS, job_id)
    os.makedirs(proj, exist_ok=True)
    src = opts['video_path']
    prof = characters.get(opts.get('character'))
    job['character'] = prof['label']

    def stage(n, pct):
        job['stage'] = n; job['progress'] = pct; save(job_id); log.info(f'[{job_id}] {n}')

    def soft(name, fn, *a, **kw):
        """Run a stage; record the failure and carry on rather than aborting."""
        try:
            return fn(*a, **kw)
        except Exception as e:
            log.warning(f'[{job_id}] {name} failed: {e}')
            job.setdefault('warnings', []).append(f'{name}: {e}')
            return None

    try:
        stage('probing', 3)
        info = mediakit.probe(src)
        dur = info['duration']
        job['duration'] = round(dur, 1)
        job['source'] = info

        stage('transcribing (word-level)', 12)
        segments = transcribe_words(src)
        job['segments'] = len(segments)
        json.dump(segments, open(os.path.join(proj, 'transcript.json'), 'w'), indent=1)

        stage('removing dead air', 38)
        silences = soft('silence', mediakit.detect_silence, src) or []
        speech = mediakit.speaking_ranges(dur, silences) if silences else []
        job['silence_count'] = len(silences)
        job['dead_air_seconds'] = round(sum(e - s for s, e in silences), 1)

        stage('checking for bad takes', 44)
        bad = soft('bad takes', mediakit.find_bad_takes, segments) or []
        job['bad_takes'] = bad[:40]
        json.dump(bad, open(os.path.join(proj, 'bad_takes.json'), 'w'), indent=1)

        stage('measuring loudness', 55)
        times, vals = soft('loudness', loudness_curve, src) or ([], [])

        stage('selecting highlights', 62)
        clip_len = float(opts.get('clip_seconds', 8))
        cands = score_windows(dur, segments, times, vals, clip_len,
                              float(opts.get('stride_seconds', 2)),
                              opts.get('keywords') or DEFAULT_KEYWORDS)
        # drop windows dominated by a flagged bad take
        bad_spans = [(b['start'], b['end']) for b in bad]
        def clean(c):
            for bs, be in bad_spans:
                if min(c['end'], be) - max(c['start'], bs) > (c['end'] - c['start']) * 0.5:
                    return False
            return True
        cands = [c for c in cands if clean(c)] or cands
        budget = dur * float(opts.get('percent', 2)) / 100.0
        clips = pick_non_overlapping(cands, budget, limit=opts.get('max_clips'))
        job['clips'] = clips
        job['kept_seconds'] = round(sum(c['end'] - c['start'] for c in clips), 1)
        job['kept_percent'] = round(100.0 * job['kept_seconds'] / dur, 2)

        stage('writing narration script', 70)
        raw = ' '.join(c['text'] for c in clips).strip()
        polished = None
        if raw and opts.get('narrate', True):
            r = soft('polish', lambda: requests.post(
                POLISH_URL, json={'raw_script': raw}, timeout=600).json())
            polished = (r or {}).get('polished_text')
        job['script'] = polished or raw
        open(os.path.join(proj, 'script.txt'), 'w').write(job['script'] or '')

        narration, narration_dur = None, 0.0
        if polished and opts.get('tts', True):
            stage('generating narration (TTS)', 78)
            wav = os.path.join(proj, 'narration.wav')
            def do_tts():
                resp = requests.post(TTS_URL, files={}, data={
                    'text': polished, 'voice_url': prof['voice']}, timeout=900)
                resp.raise_for_status()
                open(wav, 'wb').write(resp.content)
                return wav
            if soft('tts', do_tts):
                narration = wav
                narration_dur = mediakit.probe(wav)['duration']   # WAV header lies
                job['narration_seconds'] = round(narration_dur, 1)
                stage('cleaning narration audio', 82)
                cleaned = os.path.join(proj, 'narration_clean.m4a')
                if soft('audio cleanup', mediakit.clean_audio, wav, cleaned):
                    narration, narration_dur = cleaned, mediakit.probe(cleaned)['duration']

        stage('captions', 86)
        # caption timings are rebased onto the cut timeline, not the source
        cap_segments, playhead = [], 0.0
        for c in clips:
            for s in segments:
                if s['end'] <= c['start'] or s['start'] >= c['end']:
                    continue
                ns = max(s['start'], c['start']) - c['start'] + playhead
                ne = min(s['end'], c['end']) - c['start'] + playhead
                cap_segments.append({
                    'start': ns, 'end': ne, 'text': s['text'],
                    'words': [{'word': w['word'],
                               'start': w['start'] - c['start'] + playhead,
                               'end':   w['end'] - c['start'] + playhead}
                              for w in s.get('words', [])
                              if c['start'] <= w['start'] < c['end']],
                })
            playhead += c['end'] - c['start']
        soft('srt', mediakit.write_srt, cap_segments, os.path.join(proj, 'captions.srt'))
        soft('ass', mediakit.write_ass, cap_segments, os.path.join(proj, 'captions.ass'),
             prof['caption_style'])

        stage('chapters', 89)
        chapters = soft('chapters', mediakit.chapters_from_segments, segments) or []
        job['chapters'] = chapters
        soft('chapter file', mediakit.write_chapter_file, chapters,
             os.path.join(proj, 'chapters.txt'))

        stage('thumbnail candidates', 91)
        thumbs = soft('thumbnails', mediakit.thumbnail_frames, src,
                      os.path.join(proj, 'thumbs')) or []
        job['thumbnails'] = [os.path.basename(t) for t in thumbs]

        stage('building timeline', 94)
        zooms = mediakit.zoom_plan(clips, prof.get('zoom_every', 2))
        markers = [{'id': f'm{i}', 'time': round(b['start'], 2),
                    'note': 'bad take: ' + ', '.join(b['reasons']), 'color': 'red',
                    'createdAt': int(time.time() * 1000)}
                   for i, b in enumerate(bad[:25])]
        tl = build_timeline(os.path.basename(src), src, dur, clips, zooms,
                            narration, narration_dur,
                            {'segments': cap_segments, 'style': prof['caption_style']},
                            markers)
        json.dump(tl, open(os.path.join(proj, 'timeline.json'), 'w'), indent=1)
        job['timeline'] = 'timeline.json'

        if opts.get('vertical', True) and clips:
            stage('9:16 version', 97)
            reel = os.path.join(proj, 'reel.mp4')
            paths = []
            for i, c in enumerate(clips, 1):
                p = os.path.join(proj, f'clip_{i:02d}.mp4')
                if soft(f'cut {i}', cut_clip, src, c['start'], c['end'] - c['start'], p):
                    paths.append(p)
            if paths:
                lst = os.path.join(proj, 'concat.txt')
                open(lst, 'w').write(''.join(f"file '{p}'\n" for p in paths))
                if soft('reel', lambda: subprocess.run(
                        ['ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', lst,
                         '-c', 'copy', reel], check=True, capture_output=True)):
                    job['reel'] = 'reel.mp4'
                    if soft('vertical', mediakit.to_vertical, reel,
                            os.path.join(proj, 'vertical.mp4'),
                            prof.get('vertical_mode', 'center')):
                        job['vertical'] = 'vertical.mp4'

        job['stage'] = 'done'; job['progress'] = 100; job['finished'] = time.time()
    except Exception as e:
        log.exception('job failed')
        job['stage'] = 'error'; job['error'] = str(e)
    finally:
        save(job_id)


def save(job_id):
    d = os.path.join(PROJECTS, job_id)
    os.makedirs(d, exist_ok=True)
    json.dump(JOBS[job_id], open(os.path.join(d, 'job.json'), 'w'), indent=1, default=str)


# ---------------------------------------------------------------- routes

@app.route('/auto', methods=['POST'])
def auto():
    data = request.json or {}
    v = data.get('video_path')
    if not v or not os.path.exists(v):
        return jsonify({'error': f'video_path not found: {v}'}), 400
    job_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4]
    JOBS[job_id] = {'id': job_id, 'video_path': v, 'name': os.path.basename(v),
                    'stage': 'queued', 'progress': 0, 'started': time.time()}
    save(job_id)
    threading.Thread(target=run, args=(job_id, data), daemon=True).start()
    return jsonify({'job_id': job_id, 'status_url': f'/auto/{job_id}'})


@app.route('/auto/<job_id>')
def status(job_id):
    if job_id not in JOBS:
        p = os.path.join(PROJECTS, job_id, 'job.json')
        if os.path.exists(p):
            JOBS[job_id] = json.load(open(p))
        else:
            return jsonify({'error': 'unknown job'}), 404
    return jsonify(JOBS[job_id])


@app.route('/auto/<job_id>/file/<path:fname>')
def jobfile(job_id, fname):
    p = os.path.join(PROJECTS, job_id, fname)
    if not os.path.exists(p):
        return jsonify({'error': 'not found'}), 404
    return send_file(p)


@app.route('/characters')
def chars():
    return jsonify(characters.all_profiles())


@app.route('/characters/<name>', methods=['POST'])
def set_char(name):
    return jsonify(characters.save(name, request.json))


@app.route('/jobs')
def jobs():
    out = []
    for d in sorted(os.listdir(PROJECTS), reverse=True)[:60]:
        p = os.path.join(PROJECTS, d, 'job.json')
        if os.path.exists(p):
            try:
                j = json.load(open(p))
                out.append({k: j.get(k) for k in
                            ('id', 'name', 'stage', 'progress', 'duration',
                             'kept_seconds', 'kept_percent', 'character', 'started')})
            except Exception:
                pass
    return jsonify(out)


@app.route('/')
def index():
    return render_template_string(open('/opt/mini-studio/autoedit.html').read())


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9545, threaded=True)
