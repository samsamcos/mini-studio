"""
Vlog Builder — port 9534
Fetches the Vlog Builder UI from the Edit Director at 9533.
All API calls go directly to the director — this service only serves the page.
"""
import os
import requests
from flask import Flask, Response

app = Flask(__name__)
DIRECTOR = os.environ.get("DIRECTOR_URL", "http://127.0.0.1:9533")
PORT     = int(os.environ.get("VLOG_PORT", "9534"))

@app.route("/")
@app.route("/vlog")
def index():
    try:
        r = requests.get(f"{DIRECTOR}/vlog", timeout=10)
        return Response(r.content, content_type="text/html; charset=utf-8")
    except Exception as e:
        return Response(
            f"<h1 style='font-family:sans-serif;padding:40px;color:#e0e0e0;"
            f"background:#080b0f;min-height:100vh'>🎥 Vlog Builder</h1>"
            f"<p style='font-family:sans-serif;padding:0 40px;color:#f87171'>"
            f"Cannot reach Edit Director at {DIRECTOR}: {e}<br><br>"
            f"Make sure the <b>edit-director</b> service is running.</p>",
            content_type="text/html"
        )

if __name__ == "__main__":
    print(f"[vlog-service] Vlog Builder starting on :{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
