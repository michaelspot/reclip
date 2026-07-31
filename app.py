import os
import uuid
import glob
import json
import re
import shutil
import subprocess
import threading
import zipfile
from flask import Flask, request, jsonify, send_file, render_template
from mutagen.id3 import (
    APIC,
    ID3,
    ID3NoHeaderError,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
)
from mutagen.mp3 import MP3

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

SINGLE_DOWNLOAD_TIMEOUT = 300
PLAYLIST_INFO_TIMEOUT = 90
ALBUM_DOWNLOAD_TIMEOUT = 60 * 60


def sanitize_filename(value, fallback="download", limit=120):
    """Return a cross-platform-safe filename while keeping it readable."""
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    return text[:limit].strip() or fallback


def strip_topic_suffix(value):
    return re.sub(r"\s+-\s+Topic$", "", value or "", flags=re.IGNORECASE).strip()


def text_value(value):
    """Normalize yt-dlp metadata values into a player-friendly string."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for item in value:
            item_text = text_value(item)
            if item_text and item_text not in parts:
                parts.append(item_text)
        return ", ".join(parts)
    if isinstance(value, dict):
        return text_value(value.get("name") or value.get("title") or value.get("id"))
    return str(value).strip()


def first_text(*values):
    for value in values:
        text = text_value(value)
        if text:
            return text
    return ""


def normalize_date(info):
    value = first_text(
        info.get("release_date"),
        info.get("upload_date"),
        info.get("date"),
        info.get("release_year"),
        info.get("year"),
    )
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value


def playlist_entry_url(entry):
    url = first_text(entry.get("webpage_url"), entry.get("url"))
    if url.startswith("http"):
        return url
    if url.startswith("/"):
        return f"https://music.youtube.com{url}"
    video_id = first_text(entry.get("id"), url)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return ""


def fetch_playlist_info(url):
    cmd = ["yt-dlp", "--flat-playlist", "-J", url]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=PLAYLIST_INFO_TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().split("\n")[-1])
    return json.loads(result.stdout)


def playlist_summary(info):
    entries = info.get("entries") or []
    first_entry = next((entry for entry in entries if entry), {})
    thumbnail = first_text(info.get("thumbnail"), first_entry.get("thumbnail"))
    if not thumbnail:
        thumbnails = info.get("thumbnails") or first_entry.get("thumbnails") or []
        if thumbnails:
            thumbnail = first_text(thumbnails[-1].get("url"))

    album_title = first_text(
        info.get("album"),
        info.get("playlist_title"),
        info.get("title"),
        first_entry.get("album"),
        "Album",
    )
    album_artist = strip_topic_suffix(first_text(
        info.get("album_artist"),
        info.get("artist"),
        info.get("artists"),
        info.get("creator"),
        info.get("uploader"),
        info.get("channel"),
        first_entry.get("album_artist"),
        first_entry.get("artist"),
        first_entry.get("artists"),
        first_entry.get("creator"),
        first_entry.get("uploader"),
        first_entry.get("channel"),
        "Unknown Artist",
    ))

    urls = []
    for entry in entries:
        entry_url = playlist_entry_url(entry)
        if entry_url:
            urls.append(entry_url)
    return {
        "title": album_title,
        "uploader": album_artist,
        "thumbnail": thumbnail,
        "track_count": len(entries),
        "urls": urls,
    }


def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output.

    With ``-j`` yt-dlp prints one JSON object per line. Some extractors
    emit multiple videos even with ``--no-playlist``, so stdout contains
    several objects and a plain ``json.loads`` raises "Extra data".
    Return the first valid object.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise ValueError("yt-dlp returned no data")


def set_text_frame(tags, frame_id, frame_cls, value):
    value = text_value(value)
    if not value:
        return
    tags.delall(frame_id)
    tags.add(frame_cls(encoding=3, text=value))


def find_sidecar_file(mp3_path, extensions):
    stem = os.path.splitext(mp3_path)[0]
    for extension in extensions:
        candidate = f"{stem}.{extension}"
        if os.path.exists(candidate):
            return candidate
    return None


def load_track_info(mp3_path):
    info_path = find_sidecar_file(mp3_path, ["info.json"])
    if not info_path:
        return {}
    try:
        with open(info_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}


def track_number_from_filename(mp3_path):
    match = re.match(r"^(\d+)", os.path.basename(mp3_path))
    return match.group(1) if match else ""


def embed_id3_metadata(mp3_path, album_title="", album_artist=""):
    """Write core ID3 tags after yt-dlp finishes, with Android-friendly ID3v2.3."""
    info = load_track_info(mp3_path)
    track_number = first_text(info.get("track_number"), info.get("playlist_index"), track_number_from_filename(mp3_path))
    disc_number = first_text(info.get("disc_number"), info.get("disc"))
    track_title = first_text(info.get("track"), info.get("title"), os.path.splitext(os.path.basename(mp3_path))[0])
    track_artist = strip_topic_suffix(first_text(
        info.get("artist"),
        info.get("artists"),
        info.get("creator"),
        info.get("uploader"),
        info.get("channel"),
        album_artist,
    ))
    track_album = first_text(info.get("album"), info.get("playlist_title"), album_title)
    track_album_artist = strip_topic_suffix(first_text(
        info.get("album_artist"),
        info.get("album_artists"),
        album_artist,
        track_artist,
    ))
    track_date = normalize_date(info)

    try:
        audio = MP3(mp3_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
    except ID3NoHeaderError:
        audio = MP3(mp3_path)
        audio.add_tags()
        tags = audio.tags

    set_text_frame(tags, "TIT2", TIT2, track_title)
    set_text_frame(tags, "TPE1", TPE1, track_artist)
    set_text_frame(tags, "TPE2", TPE2, track_album_artist)
    set_text_frame(tags, "TALB", TALB, track_album)
    set_text_frame(tags, "TRCK", TRCK, track_number)
    set_text_frame(tags, "TPOS", TPOS, disc_number)
    set_text_frame(tags, "TDRC", TDRC, track_date)

    cover_path = find_sidecar_file(mp3_path, ["jpg", "jpeg", "png"])
    if cover_path:
        with open(cover_path, "rb") as cover:
            cover_data = cover.read()
        mime = "image/png" if cover_path.lower().endswith(".png") else "image/jpeg"
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime=mime,
            type=3,
            desc="Cover",
            data=cover_data,
        ))

    tags.save(mp3_path, v2_version=3)


def remove_album_sidecars(album_dir):
    for path in glob.glob(os.path.join(album_dir, "**", "*"), recursive=True):
        if os.path.isfile(path) and not path.lower().endswith(".mp3"):
            try:
                os.remove(path)
            except OSError:
                pass


def zip_album(album_dir, zip_path, folder_name):
    mp3_files = sorted(glob.glob(os.path.join(album_dir, "*.mp3")))
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for mp3_path in mp3_files:
            archive.write(mp3_path, os.path.join(folder_name, os.path.basename(mp3_path)))
    return mp3_files


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += [
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
        ]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SINGLE_DOWNLOAD_TIMEOUT)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = result.stderr.strip().split("\n")[-1]
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:100].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def update_album_progress(job, completed, total, message=""):
    job["completed"] = max(0, completed)
    job["total"] = max(0, total)
    if total:
        job["progress"] = min(100, int((job["completed"] / total) * 100))
    if message:
        job["message"] = message


def count_mp3_files(album_dir):
    return len(glob.glob(os.path.join(album_dir, "*.mp3")))


def run_album_download(job_id, url):
    job = jobs[job_id]
    base_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(base_dir, exist_ok=True)

    try:
        info = fetch_playlist_info(url)
        summary = playlist_summary(info)
        album_title = summary["title"]
        album_artist = summary["uploader"]
        total = summary["track_count"]
        folder_label = f"{album_artist} - {album_title}" if album_artist else album_title
        folder_name = sanitize_filename(folder_label, fallback=job_id, limit=140)
        album_dir = os.path.join(base_dir, folder_name)
        os.makedirs(album_dir, exist_ok=True)

        job.update({
            "album": album_title,
            "artist": album_artist,
            "filename": f"{folder_name}.zip",
            "total": total,
            "completed": 0,
            "progress": 0,
            "message": f"Preparing {total or 'album'} track(s)...",
        })

        out_template = os.path.join(album_dir, "%(playlist_index)02d - %(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--yes-playlist",
            "--ignore-errors",
            "--newline",
            "-x",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails", "jpg",
            "--write-info-json",
            "--write-thumbnail",
            "--parse-metadata", "%(playlist_title)s:%(meta_album)s",
            "--parse-metadata", "%(playlist_index)s:%(meta_track)s",
            "-o", out_template,
            url,
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        last_lines = []

        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                last_lines.append(line)
                last_lines = last_lines[-6:]

                item_match = re.search(r"\[download\]\s+Downloading item\s+(\d+)\s+of\s+(\d+)", line)
                if item_match:
                    current = int(item_match.group(1))
                    total = int(item_match.group(2))
                    update_album_progress(
                        job,
                        max(count_mp3_files(album_dir), current - 1),
                        total,
                        f"Track {current}/{total}",
                    )
                    continue

                if line.startswith("[ExtractAudio] Destination:") or line.startswith("[Metadata]"):
                    completed = count_mp3_files(album_dir)
                    update_album_progress(job, completed, total, f"{completed}/{total or '?'} track(s) converted")
                    continue

                if line.startswith("ERROR:"):
                    job["warning"] = "Some tracks could not be downloaded; continuing with the rest."

            return_code = process.wait(timeout=ALBUM_DOWNLOAD_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            job["status"] = "error"
            job["error"] = "Album download timed out (60 min limit)"
            return

        mp3_files = sorted(glob.glob(os.path.join(album_dir, "*.mp3")))
        if not mp3_files:
            job["status"] = "error"
            job["error"] = last_lines[-1] if last_lines else "Album download completed but no MP3 files were found"
            return

        metadata_errors = 0
        for mp3_path in mp3_files:
            try:
                embed_id3_metadata(mp3_path, album_title=album_title, album_artist=album_artist)
            except Exception:
                metadata_errors += 1

        remove_album_sidecars(album_dir)

        zip_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.zip")
        zipped_files = zip_album(album_dir, zip_path, folder_name)
        shutil.rmtree(base_dir, ignore_errors=True)

        completed = len(zipped_files)
        update_album_progress(job, completed, total or completed, f"{completed}/{total or completed} track(s) ready")

        if return_code != 0 or completed < total:
            job["warning"] = f"{completed}/{total or completed} track(s) were added to the ZIP."
        if metadata_errors:
            job["warning"] = f"{metadata_errors} track(s) could not be fully tagged."

        job["status"] = "done"
        job["file"] = zip_path
        job["filename"] = f"{folder_name}.zip"
    except Exception as e:
        shutil.rmtree(base_dir, ignore_errors=True)
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        info = fetch_playlist_info(url)
        return jsonify(playlist_summary(info))
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    is_album = bool(data.get("album")) and format_choice == "audio"

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "kind": "album" if is_album else "single",
    }

    if is_album:
        thread = threading.Thread(target=run_album_download, args=(job_id, url))
    else:
        thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "kind": job.get("kind"),
        "progress": job.get("progress"),
        "completed": job.get("completed"),
        "total": job.get("total"),
        "message": job.get("message"),
        "warning": job.get("warning"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
