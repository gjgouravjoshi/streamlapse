import os
import re
import shutil
import threading
import uuid
import base64
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string, send_from_directory

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", BASE_DIR / "Downloads")).resolve()
COOKIES_PATH = Path(os.environ.get("YTDLP_COOKIES_FILE", "/tmp/yt-dlp-cookies.txt"))


def find_ffmpeg_dir():
    configured = os.environ.get("FFMPEG_LOCATION", "").strip()
    if configured:
        p = Path(configured).expanduser()
        return p.parent if p.is_file() else p

    found = shutil.which("ffmpeg")
    if found:
        return Path(found).resolve().parent

    winget_root = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in winget_root.glob("Gyan.FFmpeg_*/*/bin/ffmpeg.exe"):
        return exe.parent
    return None


def get_cookiefile():
    cookie_b64 = os.environ.get("YTDLP_COOKIES_B64", "").strip()
    if cookie_b64:
        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_PATH.write_bytes(base64.b64decode(cookie_b64))
        return str(COOKIES_PATH)

    cookie_file = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookie_file and Path(cookie_file).exists():
        return cookie_file
    return None


def apply_yt_opts(opts):
    opts.setdefault("quiet", True)
    opts.setdefault("no_warnings", True)
    opts.setdefault("extractor_args", {"youtube": {"player_client": ["android", "web"]}})
    cookiefile = get_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    return opts


def friendly_error(exc):
    msg = str(exc)
    if "Sign in to confirm" in msg or "not a bot" in msg or "cookies" in msg:
        return "YouTube is asking for verification on Render. Add YTDLP_COOKIES_B64 in Render environment variables, then redeploy."
    if "ffmpeg" in msg.lower():
        return "FFmpeg is missing or unavailable. Redeploy the Docker service or check Render build logs."
    return msg[:320]

# --- Global download queue ---
download_queue = []  # list of job dicts
queue_lock = threading.Lock()

def find_job(job_id):
    with queue_lock:
        for j in download_queue:
            if j["id"] == job_id:
                return j
    return None

def make_hook(job_id):
    def hook(d):
        job = find_job(job_id)
        if not job:
            return
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            pct = round((downloaded / total) * 100, 1) if total > 0 else 0
            job.update({
                "status": "downloading",
                "percent": pct,
                "speed": d.get("_speed_str", "N/A"),
                "eta": d.get("_eta_str", "N/A"),
            })
        elif d["status"] == "finished":
            job.update({"status": "merging", "percent": 100})
    return hook

def dl_task(job):
    import yt_dlp
    job_id = job["id"]
    url    = job["url"]
    start  = job["start"]
    end    = job["end"]
    fmt    = job["format"]
    qual   = job["quality"]
    fps    = job["fps"]
    title  = job["title"]
    ffmpeg_dir = find_ffmpeg_dir()
    if not ffmpeg_dir:
        job.update({
            "status": "error",
            "error_msg": "FFmpeg is not installed or not available. Install FFmpeg and restart StreamLapse.",
        })
        return

    dest = DOWNLOAD_DIR
    dest.mkdir(parents=True, exist_ok=True)

    clean = re.sub(r'[^\w\s]', '', title).strip()
    clean = re.sub(r'\s+', ' ', clean)[:28]

    def ts(t):
        p = t.strip().split(":")
        if len(p) == 3:
            return f"{p[0]}.{p[1]}.{p[2]}" if int(p[0]) > 0 else f"{p[1]}.{p[2]}"
        return t.replace(":", ".")

    slug = f"{ts(start)}-{ts(end)}"
    if fmt == "mp3":
        base = f"{clean} {slug} Audio"
    else:
        base = f"{clean} {slug} {qual}p {fps}fps"

    final_name = f"{base}.{fmt}"
    full_path = dest / final_name
    counter = 1
    while full_path.exists():
        final_name = f"{base} ({counter}).{fmt}"
        full_path = dest / final_name
        counter   += 1

    out_tmpl = str(dest / final_name.replace(f".{fmt}", ".%(ext)s"))

    if fmt == "mp3":
        fs_candidates = ["bestaudio/best"]
    else:
        # Try strict H.264/MP4 first, then loosen the constraints if YouTube
        # does not expose that exact combination for the requested clip.
        if qual == "best":
            fs_candidates = [
                f"bestvideo[vcodec^=avc1][fps<={fps}]+bestaudio[acodec^=mp4a]/best[ext=mp4]/bestvideo[fps<={fps}]+bestaudio/best",
                f"bestvideo[fps<={fps}]+bestaudio/best",
                "best",
            ]
        else:
            fs_candidates = [
                f"bestvideo[vcodec^=avc1][height<={qual}][fps<={fps}]+bestaudio[acodec^=mp4a]/bestvideo[height<={qual}][fps<={fps}]+bestaudio/best",
                f"bestvideo[height<={qual}][fps<={fps}]+bestaudio/best",
                f"bestvideo[height<={qual}]+bestaudio/best",
                "best",
            ]

    postproc = []
    if fmt == "mp3":
        postproc = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        # Re-encode to H.264/AAC for universal compatibility
        postproc = [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": fmt,
        }]

    opts = apply_yt_opts({
        "format": fs_candidates[0],
        "force_keyframes_at_cuts": True,
        "external_downloader": "ffmpeg",
        "external_downloader_args": {
            "ffmpeg_i": ["-ss", start, "-to", end],
            "ffmpeg_o": [
                "-map", "0:v:0?",
                "-map", "0:a:0?",
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-b:a", "160k",
                "-movflags", "+faststart",
            ] if fmt != "mp3" else [],
        },
        "outtmpl": out_tmpl,
        "progress_hooks": [make_hook(job_id)],
        "quiet": True,
        "ffmpeg_location": str(ffmpeg_dir),
        "merge_output_format": fmt if fmt != "mp3" else None,
        # Force H.264 + AAC via postprocessor args
        "postprocessor_args": {
            "ffmpeg": [
                "-vcodec", "libx264",
                "-acodec", "aac",
                "-crf", "23",
                "-preset", "veryfast",
                "-movflags", "+faststart"
            ] if fmt != "mp3" else []
        },
    })
    if fmt == "mp3":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        del opts["postprocessor_args"]

    try:
        with __import__("yt_dlp").YoutubeDL(opts) as ydl:
            last_exc = None
            for candidate in fs_candidates:
                opts["format"] = candidate
                try:
                    ydl.download([url])
                    last_exc = None
                    break
                except Exception as inner_exc:
                    last_exc = inner_exc
                    if "Requested format is not available" not in str(inner_exc):
                        raise
            if last_exc is not None:
                raise last_exc
        job.update({
            "status": "complete",
            "downloaded_file": final_name,
            "download_url": f"/download/{job_id}",
            "percent": 100,
        })
    except Exception as e:
        job.update({"status": "error", "error_msg": friendly_error(e)})


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StreamLapse Pro</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
  :root {
    --bg:       #080b10;
    --surface:  #0e1420;
    --surface2: #131927;
    --border:   #1e2a3a;
    --border2:  #263245;
    --accent:   #e63535;
    --blue:     #3d9bff;
    --green:    #2dcc87;
    --text:     #d4dbe8;
    --muted:    #5a6a80;
    --mono:     'Space Mono', monospace;
    --sans:     'DM Sans', sans-serif;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100vh;
    min-height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px 16px;
  }

  /* subtle grid bg */
  body::before {
    content: '';
    position: fixed; inset: 0;
    background-image:
      linear-gradient(var(--border) 1px, transparent 1px),
      linear-gradient(90deg, var(--border) 1px, transparent 1px);
    background-size: 48px 48px;
    opacity: 0.18;
    pointer-events: none;
    z-index: 0;
  }

  .wrap {
    position: relative; z-index: 1;
    width: 100%; max-width: 560px;
    display: flex; flex-direction: column; gap: 14px;
    margin: auto;
  }

  /* ── LOGO ── */
  .logo {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    padding: 6px 0 2px;
  }
  .logo-icon {
    width: 40px; height: 40px;
    background: linear-gradient(135deg, #ff4b4b 0%, #e63535 48%, #7a1cf0 100%);
    border: 1px solid rgba(255,255,255,.14);
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-family: var(--mono);
    font-size: 13px; font-weight: 700; color: #fff;
    box-shadow: 0 0 22px rgba(230,53,53,.35);
    position: relative;
    overflow: hidden;
  }
  .logo-icon::after {
    content: '';
    position: absolute;
    left: 7px; right: 7px; bottom: 8px;
    height: 3px;
    border-radius: 99px;
    background: rgba(255,255,255,.72);
  }
  .logo-text {
    font-family: var(--mono);
    font-size: 17px; font-weight: 700;
    letter-spacing: -0.5px;
    color: #fff;
  }
  .logo-text span { color: var(--accent); }
  .logo-badge {
    margin-left: 4px;
    font-family: var(--mono);
    font-size: 9px; font-weight: 700;
    color: var(--blue);
    border: 1px solid var(--blue);
    padding: 1px 5px; border-radius: 4px;
    letter-spacing: 1px;
  }

  /* ── CARD ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 20px;
    display: flex; flex-direction: column; gap: 16px;
  }

  /* ── INPUT ROW ── */
  .input-row {
    display: flex; gap: 8px; align-items: center;
  }
  .url-wrap {
    flex: 1; position: relative;
  }
  .url-wrap i {
    position: absolute; left: 12px; top: 50%;
    transform: translateY(-50%);
    color: var(--muted); font-size: 11px;
    pointer-events: none;
  }
  input[type="url"], input[type="text"] {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    color: var(--text);
    font-family: var(--sans);
    font-size: 13px;
    padding: 10px 12px;
    transition: border-color .2s;
    outline: none;
  }
  input[type="url"] { padding-left: 34px; }
  input:focus { border-color: var(--blue); }
  input::placeholder { color: var(--muted); }

  .btn-analyze {
    background: var(--accent);
    color: #fff;
    border: none; border-radius: 10px;
    padding: 10px 16px;
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    cursor: pointer; white-space: nowrap;
    transition: background .2s, box-shadow .2s;
    display: flex; align-items: center; gap: 6px;
  }
  .btn-analyze:hover { background: #ff4444; box-shadow: 0 0 16px rgba(230,53,53,.4); }
  .btn-analyze:active { transform: scale(0.97); }

  /* ── SPINNER ── */
  .spinner-wrap {
    display: none; align-items: center; justify-content: center;
    gap: 10px; padding: 12px 0;
  }
  .spinner {
    width: 20px; height: 20px;
    border: 2px solid var(--border2);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner-wrap p { font-size: 12px; color: var(--muted); }

  /* ── DIVIDER ── */
  .divider { border: none; border-top: 1px solid var(--border); }

  /* ── THUMB ROW ── */
  .thumb-row {
    display: flex; gap: 12px; align-items: center;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px; padding: 10px;
  }
  .thumb-row img {
    width: 88px; height: 54px;
    object-fit: cover; border-radius: 6px;
    flex-shrink: 0; background: #111;
  }
  .thumb-meta { min-width: 0; flex: 1; }
  .thumb-title {
    font-size: 12px; font-weight: 600;
    color: #fff; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
  }
  .thumb-dur {
    font-family: var(--mono);
    font-size: 10px; color: var(--muted); margin-top: 3px;
  }

  /* ── GRID SELECTS ── */
  .grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 8px; }
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }

  label.lbl {
    display: block; font-size: 10px;
    font-weight: 600; letter-spacing: .6px;
    color: var(--muted); text-transform: uppercase; margin-bottom: 5px;
  }
  select {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-family: var(--sans); font-size: 12px;
    padding: 8px 10px;
    outline: none; cursor: pointer;
    transition: border-color .2s;
    appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%235a6a80'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 10px center;
    padding-right: 26px;
  }
  select:focus { border-color: var(--blue); }

  /* ── TIME INPUTS ── */
  .time-box {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px; padding: 12px;
  }
  .time-box input {
    background: var(--bg);
    text-align: center;
    font-family: var(--mono);
    font-size: 14px; font-weight: 700;
    letter-spacing: 1px;
  }

  /* ── SEGMENT QUEUE ── */
  .seg-header {
    display: flex; align-items: center; justify-content: space-between;
  }
  .seg-header h3 {
    font-size: 11px; font-weight: 600;
    text-transform: uppercase; letter-spacing: .8px;
    color: var(--muted);
  }
  .seg-list {
    display: flex; flex-direction: column; gap: 6px;
    max-height: 180px; overflow-y: auto;
  }
  .seg-list:empty::after {
    content: 'No segments added yet';
    font-size: 11px; color: var(--muted);
    text-align: center; display: block;
    padding: 8px 0;
  }
  .seg-item {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    display: flex; align-items: center; gap: 8px;
    font-size: 11px;
    transition: border-color .2s;
  }
  .seg-item.done   { border-color: var(--green); }
  .seg-item.error  { border-color: var(--accent); }
  .seg-item.active { border-color: var(--blue); }
  .seg-tag {
    font-family: var(--mono); font-size: 10px;
    color: var(--blue); background: rgba(61,155,255,.1);
    border: 1px solid rgba(61,155,255,.2);
    padding: 2px 6px; border-radius: 4px; white-space: nowrap; flex-shrink: 0;
  }
  .seg-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
  .seg-status { font-size: 10px; flex-shrink: 0; }
  .seg-bar-wrap { flex-basis: 60px; flex-shrink: 0; height: 3px; background: var(--border); border-radius: 99px; overflow: hidden; }
  .seg-bar { height: 100%; background: var(--blue); border-radius: 99px; transition: width .3s; }
  .seg-del { color: var(--muted); cursor: pointer; font-size: 10px; padding: 2px 4px; }
  .seg-del:hover { color: var(--accent); }

  /* ── BUTTONS ── */
  .btn-row { display: flex; gap: 8px; }
  .btn-add {
    flex: 1;
    background: transparent;
    border: 1px solid var(--border2);
    color: var(--text);
    border-radius: 10px; padding: 10px;
    font-family: var(--sans); font-size: 13px; font-weight: 500;
    cursor: pointer; transition: border-color .2s, background .2s;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }
  .btn-add:hover { border-color: var(--blue); background: rgba(61,155,255,.06); color: var(--blue); }
  .btn-dl {
    flex: 2;
    background: var(--blue);
    color: #fff;
    border: none; border-radius: 10px; padding: 10px;
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    cursor: pointer; transition: background .2s, box-shadow .2s;
    display: flex; align-items: center; justify-content: center; gap: 6px;
  }
  .btn-dl:hover { background: #5aaaff; box-shadow: 0 0 16px rgba(61,155,255,.35); }
  .btn-dl:active { transform: scale(.97); }
  .btn-dl:disabled { background: var(--border2); color: var(--muted); cursor: not-allowed; box-shadow: none; }

  /* ── RECENT CLIPS ── */
  .recent-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
  }
  .recent-hdr {
    background: var(--surface2);
    border-bottom: 1px solid var(--border);
    padding: 10px 16px;
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px; font-weight: 600; color: var(--muted);
    text-transform: uppercase; letter-spacing: .7px;
  }
  .recent-list {
    max-height: 200px; overflow-y: auto;
  }
  .recent-item {
    display: flex; align-items: center; gap: 10px;
    padding: 9px 14px;
    border-bottom: 1px solid var(--border);
    transition: background .15s;
  }
  .recent-item:last-child { border-bottom: none; }
  .recent-item:hover { background: var(--surface2); }
  .ri-icon { color: var(--accent); font-size: 12px; flex-shrink: 0; }
  .ri-name {
    flex: 1; min-width: 0;
    font-family: var(--mono); font-size: 10px;
    color: var(--text);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .ri-badge {
    font-size: 9px; font-weight: 700;
    color: var(--green);
    background: rgba(45,204,135,.1);
    border: 1px solid rgba(45,204,135,.25);
    padding: 2px 7px; border-radius: 99px; flex-shrink: 0;
  }
  .ri-badge.err {
    color: var(--accent);
    background: rgba(230,53,53,.1);
    border-color: rgba(230,53,53,.25);
  }
  .ri-download {
    width: 28px; height: 28px;
    border: 1px solid var(--border2);
    border-radius: 8px;
    color: var(--blue);
    display: inline-flex; align-items: center; justify-content: center;
    text-decoration: none;
    flex-shrink: 0;
  }
  .ri-download:hover { border-color: var(--blue); background: rgba(61,155,255,.08); }
  .empty-state {
    padding: 22px; text-align: center;
    font-size: 12px; color: var(--muted);
  }

  .contact {
    text-align: center;
    font-size: 11px;
    color: var(--muted);
    padding: 2px 0 0;
  }
  .contact a {
    color: var(--blue);
    text-decoration: none;
  }
  .contact a:hover { text-decoration: underline; }

  /* ── TOAST ── */
  #toast {
    position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(20px);
    background: var(--surface); border: 1px solid var(--border2);
    border-radius: 10px; padding: 10px 18px;
    font-size: 12px; color: var(--text);
    opacity: 0; transition: opacity .3s, transform .3s;
    pointer-events: none; z-index: 99; white-space: nowrap;
    box-shadow: 0 8px 32px rgba(0,0,0,.5);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  /* scrollbar */
  ::-webkit-scrollbar { width: 4px; height: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 99px; }

  .hidden { display: none !important; }

  @media (max-width: 560px) {
    body {
      align-items: flex-start;
      padding: 14px 10px 24px;
    }
    .wrap {
      max-width: none;
      gap: 12px;
    }
    .logo {
      padding-top: 2px;
    }
    .card {
      border-radius: 12px;
      padding: 14px;
      gap: 14px;
    }
    .input-row,
    .btn-row {
      flex-direction: column;
      align-items: stretch;
    }
    .btn-analyze,
    .btn-add,
    .btn-dl {
      width: 100%;
      justify-content: center;
      min-height: 42px;
    }
    .grid3 {
      grid-template-columns: 1fr;
    }
    .grid2 {
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .thumb-row {
      align-items: flex-start;
    }
    .thumb-row img {
      width: 96px;
      height: 58px;
    }
    .seg-item {
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 7px;
    }
    .seg-name {
      grid-column: 1 / -1;
      white-space: normal;
      overflow-wrap: anywhere;
    }
    .seg-bar-wrap {
      grid-column: 1 / -1;
      flex-basis: auto;
      width: 100%;
    }
    .recent-item {
      gap: 8px;
      padding: 10px 12px;
    }
    .ri-name {
      white-space: normal;
      overflow-wrap: anywhere;
    }
    #toast {
      width: calc(100% - 24px);
      max-width: 420px;
      white-space: normal;
      text-align: center;
    }
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- LOGO -->
  <div class="logo">
    <div class="logo-icon">SL</div>
    <div>
      <div class="logo-text">Stream<span>Lapse</span> <span class="logo-badge">PRO</span></div>
    </div>
  </div>

  <!-- MAIN CARD -->
  <div class="card">

    <!-- URL input -->
    <div class="input-row">
      <div class="url-wrap">
        <i class="fa-solid fa-link"></i>
        <input type="url" id="url" placeholder="Paste YouTube / video URL here…">
      </div>
      <button class="btn-analyze" onclick="analyze()">
        <i class="fa-solid fa-bolt"></i> Analyze
      </button>
    </div>

    <!-- Spinner -->
    <div class="spinner-wrap" id="loading">
      <div class="spinner"></div>
      <p>Fetching stream info…</p>
    </div>

    <!-- Config section -->
    <div id="config" class="hidden" style="display:flex;flex-direction:column;gap:14px;">

      <hr class="divider">

      <!-- Thumbnail -->
      <div class="thumb-row">
        <img id="thumb" src="" alt="thumb">
        <div class="thumb-meta">
          <div class="thumb-title" id="title">—</div>
          <div class="thumb-dur" id="dur">—</div>
        </div>
      </div>

      <!-- Format / Quality / FPS -->
      <div class="grid3">
        <div>
          <label class="lbl">Format</label>
          <select id="fmt" onchange="updateQual()">
            <option value="mp4">MP4</option>
            <option value="mkv">MKV</option>
            <option value="mp3">MP3</option>
          </select>
        </div>
        <div>
          <label class="lbl">Quality</label>
          <select id="qual"></select>
        </div>
        <div>
          <label class="lbl">Max FPS</label>
          <select id="fps">
            <option value="30">30 fps</option>
            <option value="60" selected>60 fps</option>
          </select>
        </div>
      </div>

      <!-- Time range -->
      <div class="time-box">
        <div class="grid2">
          <div>
            <label class="lbl" style="text-align:center;">▶ Start</label>
            <input type="text" id="start" value="00:04:30">
          </div>
          <div>
            <label class="lbl" style="text-align:center;">⏹ End</label>
            <input type="text" id="end" value="00:05:00">
          </div>
        </div>
      </div>

      <!-- Segment queue -->
      <div>
        <div class="seg-header" style="margin-bottom:8px;">
          <h3><i class="fa-solid fa-list-check" style="margin-right:5px;"></i>Download Queue</h3>
          <span id="queue-count" style="font-family:var(--mono);font-size:10px;color:var(--muted);">0 segments</span>
        </div>
        <div class="seg-list" id="seg-list"></div>
      </div>

      <!-- Buttons -->
      <div class="btn-row">
        <button class="btn-add" onclick="addSegment()">
          <i class="fa-solid fa-plus"></i> Add Segment
        </button>
        <button class="btn-dl" id="dl-btn" onclick="startQueue()">
          <i class="fa-solid fa-download"></i> Download All
        </button>
      </div>

    </div>
  </div>

  <!-- RECENT CLIPS -->
  <div class="recent-card">
    <div class="recent-hdr">
      <span><i class="fa-solid fa-film" style="margin-right:6px;opacity:.6;"></i>Recent Clips</span>
      <span id="clip-count" style="font-size:9px;font-weight:700;letter-spacing:.8px;">SESSION</span>
    </div>
    <div class="recent-list" id="recent-list">
      <div class="empty-state" id="recent-empty">
        <i class="fa-regular fa-folder-open" style="font-size:20px;display:block;margin-bottom:6px;opacity:.4;"></i>
        No clips yet this session
      </div>
    </div>
  </div>

  <div class="contact">
    Contact: <a href="mailto:gjgourav708@gmail.com">gjgourav708@gmail.com</a>
  </div>

</div>

<div id="toast"></div>

<script>
  let vid = null;
  let segments = [];   // {id, start, end, label}
  let recentClips = [];
  let isRunning = false;

  document.addEventListener('DOMContentLoaded', loadRecent);

  // ── ANALYZE ──────────────────────────────────────────────────────────────
  async function analyze() {
    const u = document.getElementById('url').value.trim();
    if (!u) { toast('⚠️ Paste a URL first', 'warn'); return; }

    show('loading', true); show('config', false);
    try {
      const r = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url: u})
      });
      const data = await r.json();
      show('loading', false);

      if (data.success) {
        vid = data;
        document.getElementById('thumb').src = data.thumbnail || '';
        document.getElementById('title').textContent = data.title || 'Untitled';
        const dur = data.duration;
        document.getElementById('dur').textContent = dur
          ? 'Duration: ' + new Date(dur * 1000).toISOString().substr(11, 8)
          : 'Live / Unknown';
        updateQual();
        show('config', true);
        segments = [];
        renderSegs();
      } else {
        toast('❌ ' + (data.error || 'Failed to analyze URL'), 'error');
      }
    } catch(e) {
      show('loading', false);
      toast('❌ Network error', 'error');
    }
  }

  // ── UPDATE QUALITY DROPDOWN ───────────────────────────────────────────────
  function updateQual() {
    const f = document.getElementById('fmt').value;
    const q = document.getElementById('qual');
    const fps = document.getElementById('fps');
    q.innerHTML = '';
    if (f === 'mp3') {
      q.innerHTML = '<option value="bestaudio">Best Audio</option>';
      q.disabled = true; fps.disabled = true;
    } else {
      q.disabled = false; fps.disabled = false;
      const res = (vid && vid.resolutions) || [];
      if (res.length) {
        res.forEach(r => {
          const o = document.createElement('option');
          o.value = r; o.textContent = r + 'p';
          if (r == 1080) o.selected = true;
          q.appendChild(o);
        });
      } else {
        q.innerHTML = '<option value="best">Best Available</option>';
      }
    }
  }

  // ── ADD SEGMENT ───────────────────────────────────────────────────────────
  function addSegment() {
    const start = document.getElementById('start').value.trim();
    const end   = document.getElementById('end').value.trim();
    if (!start || !end) { toast('⚠️ Set start and end time', 'warn'); return; }

    const id = Date.now().toString(36);
    segments.push({ id, start, end, status: 'queued', percent: 0 });
    renderSegs();
    toast(`✅ Segment ${start} → ${end} added`);

    // Auto-advance end time by 30s for convenience
    const endParts = end.split(':').map(Number);
    let secs = endParts[0]*3600 + endParts[1]*60 + endParts[2];
    secs += 30;
    const newStart = end;
    const newEnd = [
      String(Math.floor(secs/3600)).padStart(2,'0'),
      String(Math.floor((secs%3600)/60)).padStart(2,'0'),
      String(secs%60).padStart(2,'0')
    ].join(':');
    document.getElementById('start').value = newStart;
    document.getElementById('end').value = newEnd;
  }

  // ── RENDER SEGMENT LIST ───────────────────────────────────────────────────
  function renderSegs() {
    const list = document.getElementById('seg-list');
    document.getElementById('queue-count').textContent = segments.length + ' segment' + (segments.length !== 1 ? 's' : '');
    if (!segments.length) { list.innerHTML = ''; return; }

    list.innerHTML = segments.map(s => {
      const statusIcon =
        s.status === 'done'       ? `<i class="fa-solid fa-check" style="color:var(--green);font-size:10px;"></i>` :
        s.status === 'error'      ? `<i class="fa-solid fa-xmark" style="color:var(--accent);font-size:10px;"></i>` :
        s.status === 'downloading'? `<div class="spinner" style="width:12px;height:12px;border-width:1.5px;"></div>` :
        s.status === 'merging'    ? `<i class="fa-solid fa-gear fa-spin" style="color:var(--blue);font-size:10px;"></i>` :
        `<i class="fa-regular fa-clock" style="color:var(--muted);font-size:10px;"></i>`;

      const barColor = s.status === 'done' ? 'var(--green)' : s.status === 'error' ? 'var(--accent)' : 'var(--blue)';
      const cls = s.status === 'done' ? 'done' : s.status === 'error' ? 'error' : s.status !== 'queued' ? 'active' : '';
      const canDel = (s.status === 'queued');

      return `<div class="seg-item ${cls}" data-id="${s.id}">
        <span class="seg-tag">${s.start} → ${s.end}</span>
        <span class="seg-name">${s.filename || s.status.charAt(0).toUpperCase()+s.status.slice(1)}</span>
        <div class="seg-bar-wrap"><div class="seg-bar" style="width:${s.percent}%;background:${barColor};"></div></div>
        ${statusIcon}
        ${canDel ? `<span class="seg-del" onclick="removeSeg('${s.id}')"><i class="fa-solid fa-xmark"></i></span>` : ''}
      </div>`;
    }).join('');
  }

  function removeSeg(id) {
    segments = segments.filter(s => s.id !== id);
    renderSegs();
  }

  // ── START QUEUE ───────────────────────────────────────────────────────────
  async function startQueue() {
    if (isRunning) { toast('⏳ Already running…', 'warn'); return; }
    const queued = segments.filter(s => s.status === 'queued');
    if (!queued.length) { toast('⚠️ Add at least one segment', 'warn'); return; }

    isRunning = true;
    document.getElementById('dl-btn').disabled = true;

    const fmt  = document.getElementById('fmt').value;
    const qual = document.getElementById('qual').value;
    const fps  = document.getElementById('fps').value;

    for (const seg of queued) {
      seg.status = 'downloading'; seg.percent = 0;
      renderSegs();

      const payload = {
        url:     vid.url,
        start:   seg.start,
        end:     seg.end,
        format:  fmt,
        quality: qual,
        fps:     fps,
        title:   vid.title
      };

      const startRes = await fetch('/api/download', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
      });
      const started = await startRes.json();
      if (!started.success) {
        seg.status = 'error'; seg.percent = 0;
        renderSegs();
        addToRecent(seg.start + ' -> ' + seg.end, true);
        continue;
      }

      // Poll progress
      await new Promise(resolve => {
        const iv = setInterval(async () => {
          const res = await fetch('/api/progress?job_id=' + encodeURIComponent(started.job_id));
          const d = await res.json();

          if (d.status === 'downloading') {
            seg.percent = d.percent;
            seg.status = 'downloading';
            renderSegs();
          }
          if (d.status === 'merging') {
            seg.percent = 95; seg.status = 'merging';
            renderSegs();
          }
          if (d.status === 'complete') {
            clearInterval(iv);
            seg.status = 'done'; seg.percent = 100;
            seg.filename = d.downloaded_file;
            seg.downloadUrl = d.download_url;
            renderSegs();
            addToRecent(d.downloaded_file, false, d.download_url);
            triggerBrowserDownload(d.download_url);
            resolve();
          }
          if (d.status === 'error') {
            clearInterval(iv);
            seg.status = 'error'; seg.percent = 0;
            seg.filename = d.error_msg || 'Download failed';
            renderSegs();
            addToRecent(d.error_msg || (seg.start + ' -> ' + seg.end), true);
            toast(d.error_msg || 'Download failed', 'error');
            resolve();
          }
        }, 900);
      });
    }

    isRunning = false;
    document.getElementById('dl-btn').disabled = false;
    toast('🎉 All done!', 'success');
  }

  // ── RECENT CLIPS ──────────────────────────────────────────────────────────
  function addToRecent(name, isError, downloadUrl) {
    recentClips.unshift({ name, isError, downloadUrl });
    renderRecent();
  }

  function renderRecent() {
    const list = document.getElementById('recent-list');
    const empty = document.getElementById('recent-empty');
    document.getElementById('clip-count').textContent = recentClips.length + ' CLIP' + (recentClips.length !== 1 ? 'S' : '');

    if (!recentClips.length) {
      empty.style.display = '';
      list.innerHTML = '';
      list.appendChild(empty);
      return;
    }
    empty.style.display = 'none';
    list.innerHTML = recentClips.map(c => `
      <div class="recent-item">
        <i class="ri-icon fa-solid ${c.isError ? 'fa-triangle-exclamation' : 'fa-file-video'}" style="color:${c.isError ? 'var(--accent)' : 'var(--accent)'}"></i>
        <span class="ri-name">${c.name}</span>
        <span class="ri-badge ${c.isError ? 'err' : ''}">${c.isError ? 'ERROR' : 'SAVED'}</span>
        ${!c.isError && c.downloadUrl ? `<a class="ri-download" href="${c.downloadUrl}" download title="Download"><i class="fa-solid fa-download"></i></a>` : ''}
      </div>`
    ).join('');
  }

  function triggerBrowserDownload(url) {
    if (!url) return;
    const a = document.createElement('a');
    a.href = url;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function loadRecent() {
    try {
      const res = await fetch('/api/recent');
      const items = await res.json();
      recentClips = items.map(x => ({ name: x.name, isError: false, downloadUrl: x.download_url }));
      renderRecent();
    } catch(e) {
      renderRecent();
    }
  }

  // ── HELPERS ───────────────────────────────────────────────────────────────
  function show(id, visible) {
    const el = document.getElementById(id);
    if (visible) {
      el.classList.remove('hidden');
      el.style.display = 'flex';
    } else {
      el.classList.add('hidden');
      el.style.display = 'none';
    }
  }

  let toastTimer;
  function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.borderColor = type === 'error' ? 'rgba(230,53,53,.4)'
      : type === 'success' ? 'rgba(45,204,135,.4)'
      : type === 'warn' ? 'rgba(255,190,0,.4)'
      : 'var(--border2)';
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 3000);
  }
</script>
</body>
</html>"""


# ── Flask routes ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template_string(HTML_TEMPLATE)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ffmpeg": str(find_ffmpeg_dir() or "")})


@app.route("/api/analyze", methods=["POST"])
def analyze_api():
    import yt_dlp
    url = request.json.get("url", "").strip()
    try:
        opts = apply_yt_opts({})
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            fmts = info.get("formats", [])
            # Only H.264 resolutions preferred, but list all video heights
            res = sorted(
                list({f.get("height") for f in fmts
                      if f.get("height") and f.get("vcodec") not in (None, "none")}),
                reverse=True
            )
            return jsonify({
                "success": True,
                "url": url,
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "duration": info.get("duration"),
                "resolutions": res,
            })
    except Exception as e:
        return jsonify({"success": False, "error": friendly_error(e)})


@app.route("/api/progress")
def progress_api():
    job_id = request.args.get("job_id", "").strip()
    with queue_lock:
        if job_id:
            job = next((j for j in download_queue if j["id"] == job_id), None)
            return jsonify(job or {"status": "missing"})
        if not download_queue:
            return jsonify({"status": "idle"})
        return jsonify(download_queue[-1])


@app.route("/api/recent")
def recent_api():
    with queue_lock:
        completed = [
            {
                "id": j["id"],
                "name": j.get("downloaded_file", ""),
                "download_url": j.get("download_url", f"/download/{j['id']}"),
            }
            for j in reversed(download_queue)
            if j.get("status") == "complete" and j.get("downloaded_file")
        ]
    return jsonify(completed[:20])


@app.route("/download/<job_id>")
def download_file(job_id):
    job = find_job(job_id)
    if not job or job.get("status") != "complete" or not job.get("downloaded_file"):
        return jsonify({"success": False, "error": "File not ready"}), 404
    return send_from_directory(DOWNLOAD_DIR, job["downloaded_file"], as_attachment=True)


@app.route("/api/download", methods=["POST"])
def download_api():
    d = request.json
    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "url":     d["url"],
        "start":   d["start"],
        "end":     d["end"],
        "format":  d["format"],
        "quality": d["quality"],
        "fps":     d["fps"],
        "title":   d["title"],
        "status":  "idle",
        "percent": 0,
        "speed":   "0",
        "eta":     "0",
        "error_msg": "",
        "downloaded_file": "",
    }
    with queue_lock:
        download_queue.append(job)
    threading.Thread(target=dl_task, args=(job,), daemon=True).start()
    return jsonify({"success": True, "job_id": job_id})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
