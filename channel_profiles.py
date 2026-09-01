"""
Channel style profiles — per-channel editing dials that persist across jobs.
Stored in a local SQLite DB so the studio can adapt to each channel's style
without touching characters.json (which carries character voice/caption config).
"""
import os, sqlite3, json
from datetime import datetime

DB = os.environ.get('CHANNEL_PROFILES_DB', '/opt/studio/projects/channel_profiles.db')


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS channel_style_profiles (
                channel_name          TEXT PRIMARY KEY,
                silence_threshold_db  REAL DEFAULT -30.0,
                min_silence_duration  REAL DEFAULT 0.5,
                max_highlight_seconds REAL DEFAULT 30.0,
                words_per_second      REAL DEFAULT 2.3,
                zoom_frequency_hz     REAL DEFAULT 0.2,
                caption_font          TEXT DEFAULT 'Montserrat-Bold',
                caption_color         TEXT DEFAULT '#FFFFFF',
                tts_voice_id          TEXT DEFAULT 'narrator',
                intro_hook_style      TEXT DEFAULT 'question',
                cut_frequency         TEXT DEFAULT 'medium',
                music_style           TEXT DEFAULT 'none',
                thumbnail_style       TEXT DEFAULT 'face',
                notes                 TEXT DEFAULT '',
                updated_at            TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')


def get(channel_name: str) -> dict:
    init()
    with _conn() as c:
        row = c.execute('SELECT * FROM channel_style_profiles WHERE channel_name = ?',
                        (channel_name,)).fetchone()
    if row:
        return dict(row)
    return {}


def upsert(channel_name: str, dials: dict) -> dict:
    init()
    dials = {k: v for k, v in dials.items()
             if k not in ('channel_name', 'updated_at')}
    dials['channel_name'] = channel_name
    dials['updated_at'] = datetime.utcnow().isoformat()
    cols = ', '.join(dials.keys())
    placeholders = ', '.join('?' for _ in dials)
    updates = ', '.join(f'{k}=excluded.{k}' for k in dials if k != 'channel_name')
    with _conn() as c:
        c.execute(
            f'INSERT INTO channel_style_profiles ({cols}) VALUES ({placeholders}) '
            f'ON CONFLICT(channel_name) DO UPDATE SET {updates}',
            list(dials.values())
        )
    return get(channel_name)


def delete(channel_name: str):
    init()
    with _conn() as c:
        c.execute('DELETE FROM channel_style_profiles WHERE channel_name = ?',
                  (channel_name,))


def all_profiles() -> list:
    init()
    with _conn() as c:
        return [dict(r) for r in c.execute(
            'SELECT * FROM channel_style_profiles ORDER BY channel_name').fetchall()]
