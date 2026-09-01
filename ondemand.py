"""On-demand llama-server lifecycle.

Big models stay stopped until a request needs them, then idle-stop again.
Node 4 only has 11Gi until the 32GB rebuild, and Gemma 2 9B alone is ~5.5Gi.
"""
import os, time, subprocess, threading, logging, requests

log = logging.getLogger(__name__)

IDLE_SECONDS = int(os.environ.get('MODEL_IDLE_SECONDS', '600'))
_last_use = {}
_lock = threading.Lock()
_reaper_started = False


def _is_active(unit):
    r = subprocess.run(['systemctl', 'is-active', unit], capture_output=True, text=True)
    return r.stdout.strip() == 'active'


def _healthy(port):
    try:
        return requests.get(f'http://127.0.0.1:{port}/health', timeout=2).status_code == 200
    except Exception:
        return False


def ensure(unit, port, timeout=240):
    """Start `unit` if needed and block until llama-server answers /health."""
    with _lock:
        _last_use[unit] = (time.time(), port)
    if _healthy(port):
        return True

    if not _is_active(unit):
        log.info(f'{unit}: cold — starting')
        subprocess.run(['systemctl', 'start', unit], check=False)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _healthy(port):
            log.info(f'{unit}: ready on :{port}')
            with _lock:
                _last_use[unit] = (time.time(), port)
            return True
        time.sleep(2)
    raise RuntimeError(f'{unit} did not become ready on :{port} within {timeout}s')


def touch(unit, port):
    with _lock:
        _last_use[unit] = (time.time(), port)


def _reaper():
    while True:
        time.sleep(30)
        now = time.time()
        with _lock:
            items = list(_last_use.items())
        for unit, (ts, port) in items:
            if now - ts > IDLE_SECONDS and _is_active(unit):
                log.info(f'{unit}: idle {int(now - ts)}s — stopping to free RAM')
                subprocess.run(['systemctl', 'stop', unit], check=False)


def start_reaper():
    global _reaper_started
    if not _reaper_started:
        _reaper_started = True
        threading.Thread(target=_reaper, daemon=True).start()
