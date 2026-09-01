import os, subprocess
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

ENV_FILE = "/opt/mini-studio/.env"

SERVICES = {
    "llama-qwen":       {"label": "Qwen 2.5 3B — STT Consensus LLM",  "port": 20131},
    "llama-gemma":      {"label": "Gemma 2 9B — Voiceover LLM",        "port": 8082},
    "stt-consensus":    {"label": "STT Consensus API",                  "port": 9541},
    "voiceover":        {"label": "Voiceover Polish API",               "port": 9542},
    "gemini-ingest":    {"label": "Gemini Video Ingest API",            "port": 9543},
    "studio-auto":      {"label": "Studio Auto Pipeline",               "port": 9530},
    "studio-watcher":   {"label": "Studio File Watcher",                "port": None},
    "studio-dashboard": {"label": "Studio Dashboard",                   "port": 85},
    "edit-director":    {"label": "AI Edit Director",                   "port": 9533},
    "shorts-builder":   {"label": "Shorts Builder",                     "port": 9535},
    "vlog-builder":     {"label": "Vlog Builder",                       "port": 9534},
    "pocket-tts":       {"label": "Pocket TTS",                        "port": 5020},
    "nginx":            {"label": "Nginx",                              "port": 80},
}

API_KEYS = [
    {"key": "GEMINI_API_KEY", "label": "Gemini API Key",
     "help": "console.cloud.google.com — required for Tier 3 video ingest & fact-check", "type": "password"},
    {"key": "GROQ_API_KEY",   "label": "Groq API Key",
     "help": "console.groq.com — fast STT and AI fallback", "type": "password"},
    {"key": "OMNIROUTE_URL",  "label": "OmniRoute URL",
     "help": "LAN gateway e.g. http://192.168.0.x:20128", "type": "text"},
    {"key": "POCKET_TTS_URL", "label": "Pocket TTS URL",
     "help": "Pocket TTS endpoint e.g. http://192.168.0.x:5020", "type": "text"},
]


def read_env():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def write_env(env):
    lines = [k + "=" + v for k, v in env.items()]
    open(ENV_FILE, "w").write("\n".join(lines) + "\n")


def svc_status(name):
    r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True)
    return r.stdout.strip()


HTML = open("/opt/mini-studio/settings.html").read()


@app.route("/")
def index():
    env = read_env()
    statuses = {}
    for name in SERVICES:
        st = svc_status(name)
        statuses[name] = "active" if st == "active" else ("failed" if st in ("failed", "error") else "inactive")
    return render_template_string(HTML, api_keys=API_KEYS, env=env,
        services=SERVICES, statuses=statuses, service_names=list(SERVICES.keys()))


@app.route("/api/env", methods=["POST"])
def api_env():
    data = request.json
    env = read_env()
    env[data["key"]] = data["value"]
    write_env(env)
    if data["key"] == "GEMINI_API_KEY":
        subprocess.run(["systemctl", "restart", "gemini-ingest"])
    return jsonify({"ok": True})


@app.route("/api/service", methods=["POST"])
def api_service():
    data = request.json
    name, action = data["name"], data["action"]
    if action in ("start", "stop", "restart") and name in SERVICES:
        subprocess.run(["systemctl", action, name])
    return jsonify({"ok": True})


@app.route("/api/service/<name>")
def api_service_status(name):
    st = svc_status(name)
    css = "active" if st == "active" else ("failed" if st in ("failed", "error") else "inactive")
    return jsonify({"status": css, "raw": st})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9540)
