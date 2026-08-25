"""
Archive Browser — port 9532
Browse & restore everything that lives on 0.88 and Google Drive:
  • Proxmox backups (download / restore back to Proxmox storage)
  • Video asset packages (download / recall to studio)
"""
import json, subprocess, threading, uuid
from pathlib import Path
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

INDEX      = Path("/opt/studio/asset_index.json")
PC88_PROX  = "pc88:/srv/backup-staging/proxmox"
PC88_ARCH  = "pc88:/srv/backup-staging/studio-archive"
GD_PROX    = "gdrive:proxmox-backups"
GD_ARCH_ROOT = "gdrive:mini-studio/archive"

tasks = {}

def rc(args, timeout=7200):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def rlist(remote_path):
    r = rc(["rclone", "lsjson", remote_path, "--recursive", "--files-only"], timeout=120)
    try:
        return json.loads(r.stdout)
    except Exception:
        return []

def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"

# ── Proxmox backups ───────────────────────────────────────────────────────────
@app.route("/api/proxmox")
def proxmox_list():
    out = []
    for loc, base in (("0.88", PC88_PROX), ("gdrive", GD_PROX)):
        for f in rlist(base):
            if f["Name"].endswith((".nodelete",)):
                continue
            out.append({"name": f["Path"], "size": human(f["Size"]),
                        "bytes": f["Size"], "modified": f["ModTime"][:16],
                        "location": loc})
    out.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(out)

@app.route("/api/proxmox/restore", methods=["POST"])
def proxmox_restore():
    """Pull a backup from Drive back to 0.88 so Proxmox UI can restore it.
    Creates a .nodelete marker so the hourly uploader leaves it alone."""
    name = request.json.get("name", "")
    if not name or ".." in name:
        return jsonify({"error": "bad name"}), 400
    tid = uuid.uuid4().hex[:8]
    tasks[tid] = {"status": "running", "what": f"restore {name}"}
    def work():
        r = rc(["rclone", "copyto", f"{GD_PROX}/{name}", f"{PC88_PROX}/{name}"])
        if r.returncode == 0:
            rc(["rclone", "touch", f"{PC88_PROX}/{name}.nodelete"], timeout=60)
            tasks[tid] = {"status": "done",
                          "msg": f"{name} is back on 0.88 — restore it from Proxmox UI (storage backup88)"}
        else:
            tasks[tid] = {"status": "error", "msg": r.stderr[:300]}
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"task": tid})

# ── Video packages ────────────────────────────────────────────────────────────
@app.route("/api/videos")
def videos_list():
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    out = [{"jid": j, "package": r["package"], "topic": r.get("topic", ""),
            "date": r.get("date", ""), "channel": r.get("channel", ""),
            "summary": r.get("summary", "")[:150],
            "location": r.get("location", "local")}
           for j, r in idx.items()]
    out.sort(key=lambda x: x["date"], reverse=True)
    return jsonify(out)

@app.route("/api/videos/recall", methods=["POST"])
def videos_recall():
    """Pull a whole package back to CT500 (from 0.88 or Drive)."""
    jid = request.json.get("jid", "")
    idx = json.loads(INDEX.read_text()) if INDEX.exists() else {}
    rec = idx.get(jid)
    if not rec:
        return jsonify({"error": "not found"}), 404
    tid = uuid.uuid4().hex[:8]
    tasks[tid] = {"status": "running", "what": f"recall {rec['package']}"}
    def work():
        dest = Path("/opt/studio/media/recall") / rec["package"]
        dest.mkdir(parents=True, exist_ok=True)
        for src in (f"{PC88_ARCH}/{rec['package']}", f"gdrive:{rec['gdrive_path']}"):
            r = rc(["rclone", "copy", src, str(dest)])
            if r.returncode == 0 and any(dest.iterdir()):
                tasks[tid] = {"status": "done",
                              "msg": f"Package back on studio at media/recall/{rec['package']}"}
                return
        tasks[tid] = {"status": "error", "msg": "could not fetch from 0.88 or Drive"}
    threading.Thread(target=work, daemon=True).start()
    return jsonify({"task": tid})

@app.route("/api/download/<loc>/<path:name>")
def download(loc, name):
    """Stream a single file from 0.88 or Drive to the browser."""
    if ".." in name:
        return jsonify({"error": "bad path"}), 400
    base = {"prox88": PC88_PROX, "proxgd": GD_PROX,
            "arch88": PC88_ARCH}.get(loc)
    if loc == "archgd":
        base = GD_ARCH_ROOT
    if not base:
        return jsonify({"error": "bad location"}), 400
    p = subprocess.Popen(["rclone", "cat", f"{base}/{name}"],
                         stdout=subprocess.PIPE)
    fname = name.split("/")[-1]
    return Response(p.stdout,
                    mimetype="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})

@app.route("/api/task/<tid>")
def task_status(tid):
    return jsonify(tasks.get(tid, {"status": "unknown"}))

# ── UI ────────────────────────────────────────────────────────────────────────
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Archive Browser</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0f0f0f;color:#e0e0e0}
header{background:#1a1a1a;border-bottom:1px solid #2a2a2a;padding:14px 24px;display:flex;gap:12px;align-items:center}
header h1{font-size:1.1rem;color:#fff}header a{margin-left:auto;color:#555;font-size:.8rem;text-decoration:none}
.tabs{display:flex;gap:2px;padding:14px 24px 0;border-bottom:1px solid #222}
.tab{padding:8px 18px;font-size:.82rem;cursor:pointer;color:#888;border-radius:8px 8px 0 0;border:1px solid transparent;border-bottom:none}
.tab.active{background:#1a1a1a;border-color:#2a2a2a;color:#fff}
.pane{display:none;padding:18px 24px}.pane.active{display:block}
table{width:100%;border-collapse:collapse;font-size:.8rem}
th{color:#666;text-align:left;padding:8px;border-bottom:1px solid #2a2a2a;font-weight:500}
td{padding:8px;border-bottom:1px solid #1c1c1c}
.loc{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7rem}
.loc-local{background:#14331e;color:#4ade80}.loc-088,.loc-pc88{background:#1e2a4a;color:#93c5fd}
.loc-gdrive{background:#3a2a14;color:#fcd34d}
button{background:#3b82f6;color:#fff;border:none;border-radius:6px;padding:5px 12px;font-size:.75rem;cursor:pointer;margin-right:6px}
button:hover{background:#2563eb}button.sec{background:#2a2a2a}
a.dl{color:#93c5fd;font-size:.75rem;text-decoration:none}
.st{padding:8px 24px;font-size:.78rem;color:#6b7280;min-height:30px}
</style></head><body>
<header><span style="font-size:1.3rem">🗄️</span><h1>Archive Browser</h1>
<a href="http://192.168.0.78:85">← Control Panel</a></header>
<div class="tabs">
<div class="tab active" onclick="tab('videos')">🎬 Video Archive</div>
<div class="tab" onclick="tab('proxmox')">💾 Proxmox Backups</div>
</div>
<div class="st" id="st"></div>
<div class="pane active" id="pane-videos"><table id="vt"><thead><tr>
<th>Date</th><th>Topic</th><th>Channel</th><th>Where</th><th>Actions</th></tr></thead><tbody></tbody></table></div>
<div class="pane" id="pane-proxmox"><table id="pt"><thead><tr>
<th>Backup</th><th>Size</th><th>Date</th><th>Where</th><th>Actions</th></tr></thead><tbody></tbody></table></div>
<script>
function tab(n){document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',['videos','proxmox'][i]===n));
document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('active',p.id==='pane-'+n));}
const st=document.getElementById('st');
async function loadVideos(){
 const r=await fetch('/api/videos');const d=await r.json();
 document.querySelector('#vt tbody').innerHTML=d.map(v=>`<tr>
 <td>${v.date}</td><td><b>${v.topic||v.package}</b><br><span style="color:#555">${v.summary}</span></td>
 <td>${v.channel}</td><td><span class="loc loc-${v.location}">${v.location}</span></td>
 <td>${v.location!=='local'?`<button onclick="recall('${v.jid}')">⬇ Recall to Studio</button>`:''}
 <a class="dl" href="/api/download/${v.location==='gdrive'?'archgd':'arch88'}/${v.location==='gdrive'?v.package.replace(/^/,'')+'/raw_source.mp4':v.package+'/raw_source.mp4'}"
 onclick="return ${v.location!=='local'}">⬇ raw video</a></td></tr>`).join('')||'<tr><td colspan=5 style="color:#555">No videos archived yet</td></tr>';}
async function loadProx(){
 const r=await fetch('/api/proxmox');const d=await r.json();
 document.querySelector('#pt tbody').innerHTML=d.map(b=>`<tr>
 <td style="font-family:monospace;font-size:.72rem">${b.name}</td><td>${b.size}</td><td>${b.modified}</td>
 <td><span class="loc loc-${b.location==='0.88'?'088':'gdrive'}">${b.location}</span></td>
 <td>${b.location==='gdrive'?`<button onclick="restore('${b.name}')">↩ Restore to Proxmox</button>`:''}
 <a class="dl" href="/api/download/${b.location==='gdrive'?'proxgd':'prox88'}/${b.name}">⬇ download</a></td></tr>`).join('')||'<tr><td colspan=5 style="color:#555">No backups yet</td></tr>';}
async function poll(tid){
 const t=setInterval(async()=>{const r=await fetch('/api/task/'+tid);const d=await r.json();
 if(d.status==='done'){st.textContent='✓ '+d.msg;clearInterval(t);loadVideos();loadProx();}
 else if(d.status==='error'){st.textContent='✗ '+d.msg;clearInterval(t);}
 else st.textContent='⏳ '+(d.what||'working…');},3000);}
async function restore(name){st.textContent='Starting restore…';
 const r=await fetch('/api/proxmox/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
 const d=await r.json();if(d.task)poll(d.task);else st.textContent='✗ '+(d.error||'failed');}
async function recall(jid){st.textContent='Starting recall…';
 const r=await fetch('/api/videos/recall',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({jid})});
 const d=await r.json();if(d.task)poll(d.task);else st.textContent='✗ '+(d.error||'failed');}
loadVideos();loadProx();
</script></body></html>"""

@app.route("/")
def index():
    return PAGE

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9532, debug=False)
