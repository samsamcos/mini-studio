"""Deterministic ffmpeg/transcript operations for the Mini Studio auto-edit.

Nothing in here uses an LLM. Every function is repeatable and cheap, which is
exactly what you want for the mechanical 80% of an edit — silence trimming,
loudness, captions, chapters, reframing. The AI judgement lives in autoedit.py.
"""
import os, re, json, math, subprocess, logging

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ probing

def probe(path):
    r = subprocess.run([
        'ffprobe', '-v', 'error', '-print_format', 'json',
        '-show_format', '-show_streams', path
    ], capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next((s for s in d['streams'] if s['codec_type'] == 'video'), None)
    a = next((s for s in d['streams'] if s['codec_type'] == 'audio'), None)
    return {
        'duration': float(d['format']['duration']),
        'width':  int(v['width']) if v else 0,
        'height': int(v['height']) if v else 0,
        'fps': eval_fps(v['r_frame_rate']) if v else 0,
        'has_audio': a is not None,
    }


def eval_fps(rate):
    try:
        n, d = rate.split('/')
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        return 0.0


# ------------------------------------------------------- dead air / silence

SILENCE_START = re.compile(r'silence_start:\s*(-?[\d.]+)')
SILENCE_END   = re.compile(r'silence_end:\s*([\d.]+)')


def detect_silence(path, noise_db=-32, min_silence=0.55):
    """Return [(start, end), ...] of silent stretches."""
    p = subprocess.run([
        'ffmpeg', '-nostats', '-i', path,
        '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
        '-f', 'null', '-'
    ], capture_output=True, text=True)
    starts = [float(m.group(1)) for m in SILENCE_START.finditer(p.stderr)]
    ends   = [float(m.group(1)) for m in SILENCE_END.finditer(p.stderr)]
    out = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        if e is not None and e > s:
            out.append((max(0.0, s), e))
    return out


def speaking_ranges(duration, silences, pad=0.18, min_keep=0.35):
    """Invert silences into the ranges worth keeping, padded so words aren't clipped."""
    keep, cursor = [], 0.0
    for s, e in silences:
        start, end = cursor, min(duration, s + pad)
        if end - start >= min_keep:
            keep.append((round(max(0.0, start - pad if start > 0 else 0.0), 3), round(end, 3)))
        cursor = max(cursor, e - pad)
    if duration - cursor >= min_keep:
        keep.append((round(max(0.0, cursor), 3), round(duration, 3)))
    return merge_ranges(keep)


def merge_ranges(ranges, gap=0.12):
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s - out[-1][1] <= gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(a, 3), round(b, 3)) for a, b in out]


# ---------------------------------------------------------- bad-take detect

FILLERS = {'um', 'uh', 'erm', 'ah', 'like', 'basically', 'literally', 'sort', 'kinda'}


def find_bad_takes(segments, repeat_window=3):
    """Flag stutters, immediate word repeats, restarts and filler-heavy segments.

    Purely textual — no model. Returns segments with a `reasons` list, so the
    caller can decide whether to drop them or just surface them for review.
    """
    flagged = []
    prev_norm = None
    for i, s in enumerate(segments):
        text = s.get('text', '').strip()
        words = re.findall(r"[a-z']+", text.lower())
        reasons = []

        # immediate word repetition: "the the", "I I I"
        rep = sum(1 for a, b in zip(words, words[1:]) if a == b and len(a) > 1)
        if rep:
            reasons.append(f'{rep} repeated word(s)')

        # stutter: "b-b-but"
        if re.search(r'\b(\w)-\1', text.lower()):
            reasons.append('stutter')

        # filler density
        if words:
            fill = sum(1 for w in words if w in FILLERS) / len(words)
            if fill > 0.28 and len(words) >= 5:
                reasons.append(f'{int(fill*100)}% filler')

        # restart: this segment repeats the opening of the previous one
        norm = ' '.join(words[:5])
        if prev_norm and norm and norm == prev_norm:
            reasons.append('restarted take')
        prev_norm = norm

        # very short fragment between two real segments
        if 0 < len(words) <= 2 and 0 < i < len(segments) - 1:
            reasons.append('fragment')

        if reasons:
            flagged.append({**s, 'reasons': reasons})
    return flagged


# ------------------------------------------------------------------ audio

def clean_audio(src, dst, denoise=True):
    """Two-pass-ish loudness normalisation to -16 LUFS with optional denoise."""
    af = 'highpass=f=80,'
    if denoise:
        af += 'afftdn=nr=12:nf=-28,'
    af += 'loudnorm=I=-16:TP=-1.5:LRA=11'
    subprocess.run(['ffmpeg', '-y', '-i', src, '-af', af,
                    '-c:a', 'aac', '-b:a', '192k', dst],
                   check=True, capture_output=True)
    return dst


# ---------------------------------------------------------------- captions

def srt_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f'{h:02d}:{m:02d}:{s:06.3f}'.replace('.', ',')


def write_srt(segments, path):
    with open(path, 'w') as f:
        for i, s in enumerate(segments, 1):
            f.write(f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['text'].strip()}\n\n")
    return path


def ass_time(t):
    h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
    return f'{h:d}:{m:02d}:{s:05.2f}'


def write_ass(segments, path, style=None):
    """Burn-ready subtitles using a character's caption preset."""
    st = {
        'font': 'Arial', 'size': 54, 'primary': '&H00FFFFFF',
        'outline_col': '&H00000000', 'outline': 3, 'shadow': 1,
        'bold': -1, 'align': 2, 'margin_v': 90,
    }
    if style:
        st.update(style)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Italic,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Main,{st['font']},{st['size']},{st['primary']},{st['outline_col']},&H00000000,{st['bold']},0,1,{st['outline']},{st['shadow']},{st['align']},80,80,{st['margin_v']},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    with open(path, 'w') as f:
        f.write(header)
        for s in segments:
            txt = s['text'].strip().replace('\n', '\\N')
            f.write(f"Dialogue: 0,{ass_time(s['start'])},{ass_time(s['end'])},Main,,0,0,0,,{txt}\n")
    return path


# ---------------------------------------------------------------- chapters

def chapters_from_segments(segments, min_gap=6.0, max_chapters=12):
    """Chapter boundaries at the biggest speech gaps — deterministic, no model.

    autoedit can optionally send these titles to the LLM for nicer wording.
    """
    if not segments:
        return []
    gaps = []
    for a, b in zip(segments, segments[1:]):
        gaps.append((b['start'] - a['end'], b['start'], b['text']))
    gaps = [g for g in gaps if g[0] >= min_gap]
    gaps.sort(reverse=True)
    marks = sorted({0.0} | {round(g[1], 2) for g in gaps[:max_chapters - 1]})

    chapters = []
    for i, t in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        first = next((s['text'].strip() for s in segments if s['start'] >= t), '')
        title = ' '.join(first.split()[:7]) or f'Part {i+1}'
        chapters.append({'start': t, 'end': nxt, 'title': title})
    return chapters


def write_chapter_file(chapters, path):
    with open(path, 'w') as f:
        for c in chapters:
            t = int(c['start'])
            f.write(f'{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d} {c["title"]}\n')
    return path


# ------------------------------------------------------------- reframing

def to_vertical(src, dst, mode='center', width=1080, height=1920):
    """9:16 conversion. 'center' crops; 'blur' pillarboxes onto a blurred fill."""
    if mode == 'blur':
        vf = (f'[0:v]scale={width}:-2,boxblur=24:2,crop={width}:{height}[bg];'
              f'[0:v]scale={width}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2')
        cmd = ['ffmpeg', '-y', '-i', src, '-filter_complex', vf]
    else:
        vf = f'crop=ih*9/16:ih,scale={width}:{height}'
        cmd = ['ffmpeg', '-y', '-i', src, '-vf', vf]
    cmd += ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21',
            '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', dst]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


def thumbnail_frames(src, outdir, count=6):
    """Pull the most visually distinct frames as thumbnail candidates."""
    os.makedirs(outdir, exist_ok=True)
    pattern = os.path.join(outdir, 'thumb_%02d.jpg')
    subprocess.run([
        'ffmpeg', '-y', '-i', src,
        '-vf', f"thumbnail=n=100,scale=1280:-2",
        '-frames:v', str(count), '-vsync', 'vfr', pattern
    ], check=True, capture_output=True)
    return sorted(os.path.join(outdir, f) for f in os.listdir(outdir)
                  if f.startswith('thumb_'))


def zoom_plan(clips, every=2):
    """Alternate gentle punch-in / rest so cuts don't feel static.

    Returns a scale factor per clip; the timeline applies it as a transform,
    so nothing is re-rendered and you can undo it in the editor.
    """
    plan = []
    for i, c in enumerate(clips):
        plan.append(1.08 if i % every == 0 else 1.0)
    return plan
