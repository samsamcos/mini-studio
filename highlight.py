"""Smart Highlight Extractor — Node 4 Mini Studio.

Turns a 20-minute recording into the ~2% worth watching.

Pipeline:
  1. faster-whisper (small, int8, CPU) -> full transcript with timings
  2. ffmpeg ebur128 -> momentary loudness curve at 0.1s resolution
  3. Score sliding windows on loudness + speech density + keyword hits,
     pick non-overlapping winners until the % budget is spent
  4. ffmpeg -> individual clips + one stitched reel
  5. Optional: tag each clip via Tier 3 Gemini ingest (:9543/inspect)

Everything is local and CPU-only except the optional step 5.
"""
import os, re, json, time, uuid, math, shutil, logging, subprocess, threading
from flask import Flask, request, jsonify, send_file, render_template_string

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)

OUT_ROOT      = os.environ.get('HIGHLIGHT_OUT', '/opt/studio/highlights')
WHISPER_DIR   = os.environ.get('WHISPER_MODELS', '/opt/studio/whisper_models')
WHISPER_SIZE  = os.environ.get('WHISPER_SIZE', 'small')
GEMINI_INGEST = os.environ.get('GEMINI_INGEST_URL', 'http://127.0.0.1:9543/inspect')

DEFAULT_KEYWORDS = [
    'wow', 'oh my', 'what the', 'look at', 'no way', 'holy', 'insane',
    'first time', 'never seen', 'best', 'worst', 'finally', 'careful',
    'watch out', 'stop', 'wait', 'unbelievable', 'incredible', 'listen',
    'here it is', 'this is it', 'got it', 'there it is',
]

JOBS = {}
_model = None
_model_lock = threading.Lock()

os.makedirs(OUT_ROOT, exist_ok=True)


# ---------------------------------------------------------------- helpers

def ffprobe_duration(path):
    r = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=nw=1:nk=1', path
    ], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            log.info(f'Loading faster-whisper {WHISPER_SIZE} (int8, cpu)')
            _model = WhisperModel(WHISPER_SIZE, device='cpu', compute_type='int8',
                                  download_root=WHISPER_DIR)
        return _model


def transcribe(path):
    model = get_model()
    segments, info = model.transcribe(path, beam_size=1, vad_filter=True,
                                      condition_on_previous_text=False)
    out = []
    for s in segments:
        out.append({'start': s.start, 'end': s.end, 'text': s.text.strip()})
    return out


LOUD_RE = re.compile(r't:\s*([\d.]+)\s+M:\s*(-?[\d.inf]+)')


def loudness_curve(path):
    """Return (times[], momentary_lufs[]) sampled every ~0.1s via ebur128."""
    p = subprocess.run([
        'ffmpeg', '-nostats', '-i', path, '-filter_complex', 'ebur128=peak=none',
        '-f', 'null', '-'
    ], capture_output=True, text=True)
    times, vals = [], []
    for m in LOUD_RE.finditer(p.stderr):
        try:
            t = float(m.group(1)); v = float(m.group(2))
        except ValueError:
            continue
        if math.isinf(v) or v < -70:
            v = -70.0
        times.append(t); vals.append(v)
    return times, vals


def zscore(xs):
    if not xs:
        return []
    mean = sum(xs) / len(xs)
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    sd = math.sqrt(var) or 1.0
    return [(x - mean) / sd for x in xs]


def window_loudness(times, vals, start, end):
    sel = [v for t, v in zip(times, vals) if start <= t < end]
    if not sel:
        return -70.0
    sel.sort(reverse=True)
    top = sel[:max(1, len(sel) // 2)]      # ignore the quiet half — pauses shouldn't punish
    return sum(top) / len(top)


def window_speech(segments, start, end, keywords):
    words = 0
    hits = 0
    text = []
    for s in segments:
        if s['end'] <= start or s['start'] >= end:
            continue
        overlap = min(end, s['end']) - max(start, s['start'])
        seg_len = max(0.001, s['end'] - s['start'])
        frac = overlap / seg_len
        w = len(s['text'].split())
        words += w * frac
        text.append(s['text'])
    joined = ' '.join(text).lower()
    for k in keywords:
        if k in joined:
            hits += 1
    return words / max(1.0, end - start), min(hits, 4), ' '.join(text).strip()


# ---------------------------------------------------------------- core

def score_windows(duration, segments, times, vals, clip_len, stride, keywords):
    starts = []
    t = 0.0
    while t + clip_len <= duration:
        starts.append(round(t, 2))
        t += stride
    if not starts:
        starts = [0.0]
        clip_len = min(clip_len, duration)

    loud, wps, hits, texts = [], [], [], []
    for s in starts:
        e = s + clip_len
        loud.append(window_loudness(times, vals, s, e))
        d, h, txt = window_speech(segments, s, e, keywords)
        wps.append(d); hits.append(h); texts.append(txt)

    zl, zw = zscore(loud), zscore(wps)
    cands = []
    for i, s in enumerate(starts):
        score = 0.45 * zl[i] + 0.35 * zw[i] + 0.20 * hits[i]
        cands.append({
            'start': s, 'end': round(s + clip_len, 2), 'score': round(score, 4),
            'loudness_lufs': round(loud[i], 1), 'words_per_sec': round(wps[i], 2),
            'keyword_hits': hits[i], 'text': texts[i][:400],
        })
    cands.sort(key=lambda c: c['score'], reverse=True)
    return cands


def pick_non_overlapping(cands, budget_seconds, gap=1.0, limit=None):
    chosen = []
    used = 0.0
    for c in cands:
        if any(not (c['end'] + gap <= p['start'] or c['start'] >= p['end'] + gap) for p in chosen):
            continue
        chosen.append(c)
        used += c['end'] - c['start']
        if used >= budget_seconds or (limit and len(chosen) >= limit):
            break
    chosen.sort(key=lambda c: c['start'])
    return chosen


def cut_clip(src, start, dur, dst):
    subprocess.run([
        'ffmpeg', '-y', '-ss', str(start), '-i', src, '-t', str(dur),
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
        '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dst
    ], check=True, capture_output=True)


def build_reel(clips, workdir, dst):
    listfile = os.path.join(workdir, 'concat.txt')
    with open(listfile, 'w') as f:
        for c in clips:
            f.write("file '" + c + "'\n")
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0', '-i', listfile,
        '-c', 'copy', '-movflags', '+faststart', dst
    ], check=True, capture_output=True)


def tag_with_gemini(clip_path):
    import requests
    try:
        r = requests.post(GEMINI_INGEST, json={'video_path': clip_path}, timeout=300)
        return r.json().get('result')
    except Exception as e:
        log.warning(f'Gemini tag failed: {e}')
        return None


def run_job(job_id, opts):
    job = JOBS[job_id]
    workdir = os.path.join(OUT_ROOT, job_id)
    os.makedirs(workdir, exist_ok=True)
    src = opts['video_path']

    def stage(name, pct):
        job['stage'] = name
        job['progress'] = pct
        save_job(job_id)
        log.info(f'[{job_id}] {name}')

    try:
        stage('probing', 5)
        duration = ffprobe_duration(src)
        job['duration'] = round(duration, 1)

        stage('transcribing (faster-whisper)', 15)
        segments = transcribe(src)
        job['segments'] = len(segments)
        with open(os.path.join(workdir, 'transcript.json'), 'w') as f:
            json.dump(segments, f, indent=1)
        with open(os.path.join(workdir, 'transcript.txt'), 'w') as f:
            for s in segments:
                f.write(f"[{int(s['start']//60):02d}:{int(s['start']%60):02d}] {s['text']}\n")

        stage('measuring loudness', 55)
        times, vals = loudness_curve(src)

        stage('scoring', 70)
        clip_len = float(opts.get('clip_seconds', 8))
        stride   = float(opts.get('stride_seconds', 2))
        keywords = opts.get('keywords') or DEFAULT_KEYWORDS
        cands = score_windows(duration, segments, times, vals, clip_len, stride, keywords)

        budget = duration * float(opts.get('percent', 2)) / 100.0
        job['budget_seconds'] = round(budget, 1)
        selected = pick_non_overlapping(cands, budget, limit=opts.get('max_clips'))

        # keep spares so a bad pick can be swapped without re-running anything
        taken = {(c['start'], c['end']) for c in selected}
        alternates = pick_non_overlapping(
            [c for c in cands if (c['start'], c['end']) not in taken],
            budget * 3, limit=8)

        stage('cutting clips', 80)
        paths = []
        for i, c in enumerate(selected, 1):
            dst = os.path.join(workdir, f'clip_{i:02d}.mp4')
            cut_clip(src, c['start'], c['end'] - c['start'], dst)
            c['file'] = os.path.basename(dst)
            paths.append(dst)

        if len(paths) > 1:
            stage('stitching reel', 92)
            reel = os.path.join(workdir, 'reel.mp4')
            build_reel(paths, workdir, reel)
            job['reel'] = 'reel.mp4'
        elif paths:
            job['reel'] = os.path.basename(paths[0])

        if opts.get('use_gemini'):
            stage('tagging with Gemini', 96)
            for c in selected:
                c['gemini'] = tag_with_gemini(os.path.join(workdir, c['file']))

        job['selected'] = selected
        job['alternates'] = alternates
        job['kept_seconds'] = round(sum(c['end'] - c['start'] for c in selected), 1)
        job['kept_percent'] = round(100.0 * job['kept_seconds'] / duration, 2)
        job['stage'] = 'done'
        job['progress'] = 100
        job['finished'] = time.time()
    except Exception as e:
        log.exception('job failed')
        job['stage'] = 'error'
        job['error'] = str(e)
    finally:
        save_job(job_id)


def save_job(job_id):
    d = os.path.join(OUT_ROOT, job_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'job.json'), 'w') as f:
        json.dump(JOBS[job_id], f, indent=1)


def load_jobs():
    for name in sorted(os.listdir(OUT_ROOT)):
        p = os.path.join(OUT_ROOT, name, 'job.json')
        if os.path.exists(p):
            try:
                JOBS[name] = json.load(open(p))
            except Exception:
                pass


# ---------------------------------------------------------------- routes

@app.route('/highlight', methods=['POST'])
def highlight():
    data = request.json or {}
    video_path = data.get('video_path')
    if not video_path or not os.path.exists(video_path):
        return jsonify({'error': f'video_path not found: {video_path}'}), 400

    job_id = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4]
    JOBS[job_id] = {
        'id': job_id, 'video_path': video_path, 'stage': 'queued', 'progress': 0,
        'percent': float(data.get('percent', 2)), 'started': time.time(),
        'name': os.path.basename(video_path),
    }
    save_job(job_id)
    threading.Thread(target=run_job, args=(job_id, data), daemon=True).start()

    if data.get('wait'):
        while JOBS[job_id]['stage'] not in ('done', 'error'):
            time.sleep(2)
        return jsonify(JOBS[job_id])
    return jsonify({'job_id': job_id, 'status_url': f'/highlight/{job_id}'})


@app.route('/highlight/<job_id>')
def job_status(job_id):
    if job_id not in JOBS:
        return jsonify({'error': 'unknown job'}), 404
    return jsonify(JOBS[job_id])


@app.route('/highlight/<job_id>/swap', methods=['POST'])
def swap(job_id):
    """Replace clip index N with the next-ranked alternate, cut it on the fly."""
    job = JOBS.get(job_id)
    if not job or job.get('stage') != 'done':
        return jsonify({'error': 'job not ready'}), 400
    idx = int((request.json or {}).get('index', 0))
    if idx >= len(job['selected']) or not job.get('alternates'):
        return jsonify({'error': 'nothing to swap'}), 400

    alt = job['alternates'].pop(0)
    workdir = os.path.join(OUT_ROOT, job_id)
    dst = os.path.join(workdir, f'clip_{idx+1:02d}_alt{uuid.uuid4().hex[:3]}.mp4')
    cut_clip(job['video_path'], alt['start'], alt['end'] - alt['start'], dst)
    alt['file'] = os.path.basename(dst)
    job['alternates'].append(job['selected'][idx])
    job['selected'][idx] = alt
    job['selected'].sort(key=lambda c: c['start'])
    save_job(job_id)
    return jsonify(job)


@app.route('/highlight/<job_id>/file/<path:fname>')
def job_file(job_id, fname):
    p = os.path.join(OUT_ROOT, job_id, os.path.basename(fname))
    if not os.path.exists(p):
        return jsonify({'error': 'not found'}), 404
    return send_file(p)


@app.route('/jobs')
def jobs():
    return jsonify(sorted(
        [{k: v for k, v in j.items() if k not in ('selected', 'alternates')} for j in JOBS.values()],
        key=lambda j: j.get('started', 0), reverse=True))


@app.route('/browse')
def browse():
    root = request.args.get('dir', '/opt/studio')
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.') and d != 'highlights']
        for f in filenames:
            if f.lower().endswith(('.mp4', '.mkv', '.mov', '.ts', '.webm', '.avi')):
                p = os.path.join(dirpath, f)
                try:
                    out.append({'path': p, 'size_mb': round(os.path.getsize(p) / 1048576, 1)})
                except OSError:
                    pass
        if len(out) > 300:
            break
    return jsonify(sorted(out, key=lambda x: -x['size_mb']))


@app.route('/')
def index():
    return render_template_string(open('/opt/mini-studio/highlight.html').read())


if __name__ == '__main__':
    load_jobs()
    app.run(host='0.0.0.0', port=9544, threaded=True)
