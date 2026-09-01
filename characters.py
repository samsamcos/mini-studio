"""Character profiles — voice, caption style and narration tone in one place.

A project says "edit this as an ARIA video" and the whole pipeline picks up the
right TTS voice, caption preset and script tone.
"""
import os, json

PROFILE_FILE = os.environ.get('CHARACTER_PROFILES', '/opt/mini-studio/characters.json')

DEFAULTS = {
    'jarvis': {
        'label': 'Jarvis',
        'voice': 'alba',
        'tone': ('Calm, precise, dry British wit. Understated. Never hypes. '
                 'Short declarative sentences.'),
        'caption_style': {
            'font': 'Arial', 'size': 52, 'primary': '&H00F0F0F0',
            'outline_col': '&H00202020', 'outline': 3, 'shadow': 1,
            'bold': 0, 'align': 2, 'margin_v': 90,
        },
        'zoom_every': 3,
        'vertical_mode': 'center',
    },
    'aria': {
        'label': 'Aria',
        'voice': 'alba',
        'tone': ('Warm, upbeat, conversational. Friendly and energetic without '
                 'being shrill. Uses contractions and direct address.'),
        'caption_style': {
            'font': 'Arial', 'size': 58, 'primary': '&H00FFFFFF',
            'outline_col': '&H00C04080', 'outline': 4, 'shadow': 2,
            'bold': -1, 'align': 2, 'margin_v': 110,
        },
        'zoom_every': 2,
        'vertical_mode': 'blur',
    },
    'narrator': {
        'label': 'Documentary Narrator',
        'voice': 'alba',
        'tone': ('Documentary narration. Measured pacing, natural pauses with ... '
                 'and cadence breaks with --. Observational, not salesy.'),
        'caption_style': {
            'font': 'Georgia', 'size': 50, 'primary': '&H00FFFFFF',
            'outline_col': '&H00000000', 'outline': 3, 'shadow': 1,
            'bold': 0, 'align': 2, 'margin_v': 80,
        },
        'zoom_every': 4,
        'vertical_mode': 'center',
    },
}


def _load():
    if os.path.exists(PROFILE_FILE):
        try:
            with open(PROFILE_FILE) as f:
                data = json.load(f)
            merged = dict(DEFAULTS)
            merged.update(data)
            return merged
        except Exception:
            pass
    return dict(DEFAULTS)


def all_profiles():
    return _load()


def get(name):
    profiles = _load()
    key = (name or 'narrator').lower()
    return profiles.get(key, profiles['narrator'])


def save(name, profile):
    profiles = _load()
    profiles[name.lower()] = profile
    with open(PROFILE_FILE, 'w') as f:
        json.dump(profiles, f, indent=1)
    return profiles


def ensure_file():
    if not os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, 'w') as f:
            json.dump(DEFAULTS, f, indent=1)
