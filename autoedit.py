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
import channel_profiles
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

    # Timeline-stretch: if the AI narration runs longer than the selected clips,
    # extend the last clip's trimEnd forward so the footage covers the voiceover
    # rather than leaving the audio playing over a frozen/black frame.
    if narration and narration_dur > 0 and video_elements and playhead < narration_dur:
        shortfall = narration_dur - playhead + 0.5   # +0.5s tail pad
        last = video_elements[-1]
        available = last['trimEnd']   # how much source is still after this clip
        extend = round(min(shortfall, available), 3)
        if extend > 0:
            last['trimEnd'] = round(last['trimEnd'] - extend, 3)
            last['duration'] = round(last['duration'] + extend, 3)

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
        'id': str(uuid.uuid4()),
        'name': name,
        'duration': round(max(playhead, narration_dur), 3),
        'fps': 30,
        'width': 1920,
        'height': 1080,
        'media': media,
        'tracks': tracks,
        'markers': markers or [],
        'createdAt': now,
        'updatedAt': now,
    }


# ------------------------------------------------------------------ the job

def run(job_id, opts):
    job = JOBS[job_id]
    proj = os.path.join(PROJECTS, job_id)
    os.makedirs(proj, exist_ok=True)
    src = opts['video_path']
    prof = characters.get(opts.get('character'))
    job['character'] = prof['label']

    # Load channel profile dials if a channel was specified
    chan_prof = {}
    if opts.get('channel'):
        chan_prof = channel_profiles.get(opts['channel']) or {}
        job['channel'] = opts['channel']

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
        silence_db  = chan_prof.get('silence_threshold_db', -30.0)
        silence_dur = chan_prof.get('min_silence_duration', 0.5)
        silences = soft('silence', mediakit.detect_silence, src,
                        threshold_db=silence_db, min_duration=silence_dur) or []
        speech = mediakit.speaking_ranges(dur, silences) if silences else []
        job['silence_count'] = len(silences)
        job['dead_air_seconds'] = round(sum(e - s for s, e in silences), 1)

        stage('checking for bad takes', 44)
        bad = soft('bad takes', mediakit.find_bad_takes, segments) or []
        job['bad_takes'] = bad[:40]
        json.dump(bad, open(os.path.join(proj, 'bad_takes.json'), 'w'), indent=1)

        stage('measuring loudness', 55)
        curve = soft('loudness', loudness_curve, src) or []

        stage('selecting highlights', 62)
        pct = float(opts.get('percent', 2))
        clip_sec = float(opts.get('clip_seconds', 8))
        max_clips = int(opts.get('max_clips', 8))
        max_hl = chan_prof.get('max_highlight_seconds')
        if max_hl:
            clip_sec = min(clip_sec, float(max_hl))
        windows = score_windows(segments, curve, speech, bad,
                                window=clip_sec, keywords=DEFAULT_KEYWORDS)
        keep_n = max(1, int(dur * pct / 100 / clip_sec))
        clips = pick_non_overlapping(windows, keep_n)[:max_clips]
        job['clips'] = clips
        job['kept_seconds'] = round(sum(c['end'] - c['start'] for c in clips), 1)
        job['kept_percent'] = round(job['kept_seconds'] / dur * 100, 1)

        stage('writing narration script', 70)
        raw = ' '.join(c['text'] for c in clips).strip()
        total_clip_dur = sum(c['end'] - c['start'] for c in clips)
        polished = None
        if raw and opts.get('narrate', True):
            r = soft('polish', lambda: requests.post(
                POLISH_URL,
                json={'raw_script': raw, 'duration_seconds': total_clip_dur},
                timeout=600).json())
            polished = (r or {}).get('polished_text')
        job['script'] = polished or raw
        open(os.path.join(proj, 'script.txt'), 'w').write(job['script'] or '')

        stage('generating narration', 75)
        narration_path = None
        narration_dur = 0.0
        if opts.get('tts', True) and job['script']:
            voice_url = prof.get('voice_url') or ''
            tts_files = {'text': (None, job['script'])}
            if voice_url:
                tts_files['voice_url'] = (None, voice_url)
            tts_r = soft('tts', lambda: requests.post(
                TTS_URL, files=tts_files, timeout=300))
            if tts_r and tts_r.ok:
                narration_path = os.path.join(proj, 'narration.wav')
                open(narration_path, 'wb').write(tts_r.content)
                r = subprocess.run(['ffprobe', '-v', 'quiet', '-show_entries',
                                    'format=duration', '-of', 'json', narration_path],
                                   capture_output=True, text=True)
                narration_dur = float(json.loads(r.stdout)['format']['duration'])
                job['narration_seconds'] = round(narration_dur, 1)

        stage('cleaning narration', 78)
        narration_clean = None
        if narration_path:
            narration_clean = os.path.join(proj, 'narration_clean.m4a')
            r = subprocess.run(['ffmpeg', '-y', '-i', narration_path,
                                '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11',
                                '-c:a', 'aac', '-b:a', '128k', narration_clean],
                               capture_output=True)
            if r.returncode != 0:
                narration_clean = narration_path

        stage('captions', 82)
        caption_style = {
            'font': chan_prof.get('caption_font', prof.get('caption_font', 'Montserrat-Bold')),
            'color': chan_prof.get('caption_color', prof.get('caption_color', '#FFFFFF')),
            'size': 54, 'bold': True,
        }
        caption_segs = None
        if segments:
            caption_segs = {'style': caption_style, 'segments': []}
            for s in segments:
                if any(c['start'] <= s['start'] < c['end'] for c in clips):
                    caption_segs['segments'].append(s)
            srt_lines, ass_lines = [], [
                '[Script Info]\nScriptType: v4.00+\n[V4+ Styles]\n'
                'Format: Name,Fontname,Fontsize,PrimaryColour,Bold,Alignment\n'
                f'Style: Default,{caption_style["font"]},54,&H00FFFFFF,1,2\n'
                '[Events]\nFormat: Layer,Start,End,Style,Text']
            for i, s in enumerate(caption_segs['segments'], 1):
                def tc(sec, semi=False):
                    h, m, sv = int(sec//3600), int(sec%3600//60), sec%60
                    return f'{h:02}:{m:02}:{sv:06.3f}'.replace('.',',') if not semi \
                           else f'{h}:{m:02}:{sv:05.2f}'.replace('.',':')
                srt_lines.append(f'{i}\n{tc(s["start"])} --> {tc(s["end"])}\n{s["text"]}\n')
                ass_lines.append(f'Dialogue: 0,{tc(s["start"],True)},{tc(s["end"],True)},Default,,{s["text"]}')
            open(os.path.join(proj, 'captions.srt'), 'w').write('\n'.join(srt_lines))
            open(os.path.join(proj, 'captions.ass'), 'w').write('\n'.join(ass_lines))

        stage('chapters', 85)
        chapters = []
        if clips:
            for i, c in enumerate(clips):
                text = c.get('text', '')[:60]
                chapters.append({'start': round(c['start'], 1), 'title': text or f'Clip {i+1}'})
            job['chapters'] = chapters
            open(os.path.join(proj, 'chapters.txt'), 'w').write(
                '\n'.join(f"{int(c['start']//60):02}:{int(c['start']%60):02} {c['title']}"
                          for c in chapters))

        stage('thumbnail', 88)
        thumb = os.path.join(proj, 'thumbnail.jpg')
        if clips:
            t = clips[0]['start'] + (clips[0]['end'] - clips[0]['start']) * 0.3
            soft('thumbnail', lambda: subprocess.run(
                ['ffmpeg', '-y', '-ss', str(t), '-i', src,
                 '-vframes', '1', '-vf', 'scale=1280:720', thumb],
                capture_output=True))

        stage('building timeline', 92)
        source_dur = dur
        zooms = [1.05 if i % 3 == 0 else 1.0 for i in range(len(clips))]
        tl = build_timeline(
            name=os.path.splitext(os.path.basename(src))[0],
            source=src, source_dur=source_dur, clips=clips, zooms=zooms,
            narration=narration_clean, narration_dur=narration_dur,
            caption_segments=caption_segs,
            markers=[{'id': f'bt{i}', 'time': round(b['start'], 3),
                      'label': 'bad take', 'color': '#f85149'}
                     for i, b in enumerate(bad[:10])],
        )
        json.dump(tl, open(os.path.join(proj, 'timeline.json'), 'w'), indent=1)

        stage('9:16', 96)
        if opts.get('vertical', True) and clips:
            reel = os.path.join(proj, 'reel_preview.mp4')
            v916 = os.path.join(proj, 'vertical_916.mp4')
            first = clips[0]
            soft('preview cut', lambda: subprocess.run(
                ['ffmpeg', '-y', '-ss', str(first['start']),
                 '-i', src, '-t', str(first['end'] - first['start']),
                 '-vf', 'scale=1920:-2,crop=1080:1920',
                 '-c:v', 'libx264', '-crf', '28', '-preset', 'fast',
                 '-c:a', 'aac', reel], capture_output=True))
            soft('9:16', lambda: subprocess.run(
                ['ffmpeg', '-y', '-ss', str(first['start']),
                 '-i', src, '-t', str(min(60, first['end'] - first['start'])),
                 '-vf', ('scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih)'
                         ':force_original_aspect_ratio=increase,crop=1080:1920'),
                 '-c:v', 'libx264', '-crf', '24', '-preset', 'medium',
                 '-c:a', 'aac', v916], capture_output=True))
            if os.path.exists(reel):  job['reel'] = 'reel_preview.mp4'
            if os.path.exists(v916):  job['vertical'] = 'vertical_916.mp4'

        stage('done', 100)

    except Exception as e:
        log.exception(f'[{job_id}] fatal: {e}')
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


@app.route('/channels')
def get_channels():
    return jsonify(channel_profiles.all_profiles())


@app.route('/channels/<name>', methods=['GET'])
def get_channel(name):
    p = channel_profiles.get(name)
    if not p:
        return jsonify({'error': 'not found'}), 404
    return jsonify(p)


@app.route('/channels/<name>', methods=['POST'])
def set_channel(name):
    return jsonify(channel_profiles.upsert(name, request.json))


@app.route('/channels/<name>', methods=['DELETE'])
def del_channel(name):
    channel_profiles.delete(name)
    return jsonify({'ok': True})


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
                             'kept_seconds', 'kept_percent', 'character',
                             'channel', 'started')})
            except Exception:
                pass
    return jsonify(out)


@app.route('/voices/<name>')
def serve_voice(name):
    p = os.path.join('/opt/studio/voices', os.path.basename(name))
    if not os.path.exists(p):
        return jsonify({'error': 'not found'}), 404
    return send_file(p, mimetype='audio/wav')


@app.route('/')
def index():
    return render_template_string(open('/opt/mini-studio/autoedit.html').read())


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9545, threaded=True)
