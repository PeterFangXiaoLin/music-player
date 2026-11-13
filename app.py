import argparse
import logging
import re
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.serving import make_server
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


ROOT = Path(__file__).resolve().parent
VIDEO_PATH = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?")
VIDEO_HOSTS = {"www.bilibili.com", "bilibili.com", "m.bilibili.com"}
SHORT_PATH = re.compile(r"/[0-9A-Za-z]+/?")
MAX_AUDIO_SIZE = 200 * 1024 * 1024
AUDIO_TYPES = {".m4a": "audio/mp4", ".webm": "audio/webm"}


class ImportFailure(Exception):
    pass


def parse_link(value):
    try:
        link = urlsplit(value)
        if link.scheme in {"https", "http"} and link.hostname == link.netloc.lower():
            if link.hostname == "b23.tv" and SHORT_PATH.fullmatch(link.path):
                return f"https://b23.tv{link.path}", True

            video = VIDEO_PATH.fullmatch(link.path)
            if link.hostname in VIDEO_HOSTS and video:
                page = parse_qs(link.query, keep_blank_values=True).get("p", ["1"])[0]
                if re.fullmatch(r"[1-9][0-9]{0,5}", page):
                    return f"https://www.bilibili.com/video/{video[1]}/?p={page}", False
                raise ImportFailure("视频分 P 参数无效，请重新复制视频链接。")
    except ValueError:
        pass

    raise ImportFailure("请输入 B 站视频链接（bilibili.com/video/… 或 b23.tv 短链接）。")


class ResolvedVideo(Exception):
    pass


class BilibiliRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate every redirect before opening it, since the original URL came from the user.
        try:
            target, is_short = parse_link(urljoin(req.full_url, newurl))
            if not is_short:
                raise ResolvedVideo(target)
        except (ImportFailure, ResolvedVideo):
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, target)


def resolve_short_link(url):
    try:
        with build_opener(BilibiliRedirects()).open(Request(url, method="HEAD"), timeout=10):
            raise ImportFailure("短链接没有指向视频，请粘贴浏览器地址栏里的完整视频链接。")
    except ResolvedVideo as result:
        return str(result)
    except URLError as error:
        raise ImportFailure("无法打开短链接，请检查网络或粘贴完整视频链接。") from error


def download_track(url, destination, report):
    def progress(event):
        if event.get("status") == "downloading":
            total = event.get("total_bytes") or event.get("total_bytes_estimate")
            downloaded = event.get("downloaded_bytes", 0)
            if downloaded > MAX_AUDIO_SIZE:
                raise ImportFailure("音频超过 200 MB，请选择较短的视频。")
            report(min(99, int(downloaded * 100 / total)) if total else None)

    options = {
        "format": "bestaudio[ext=m4a][acodec^=mp4a]/bestaudio[ext=webm]",
        "outtmpl": str(destination / "track.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "logger": logging.getLogger("music_player.extractor"),
        "socket_timeout": 15,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 1,
        "max_filesize": MAX_AUDIO_SIZE,
        "progress_hooks": [progress],
    }
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, ie_key="BiliBili", download=True)
    except DownloadError as error:
        if "Requested format is not available" in str(error):
            raise ImportFailure("此视频没有可直接播放的音轨，请尝试其他视频。") from error
        raise ImportFailure("B 站导入失败，请确认视频可在未登录时播放，并检查网络后重试。") from error

    audio = destination / f"track.{info['ext']}"
    if audio.suffix not in AUDIO_TYPES or not audio.is_file() or not audio.stat().st_size:
        raise ImportFailure("没有下载到音频，请确认视频可播放且音频小于 200 MB。")

    # B站返回的 ID 会带上分 P 后缀，也能将 AV 和 BV 链接归到同一首歌。
    return {
        "source_id": info["id"],
        "source": url,
        "title": info.get("title") or "未命名音轨",
        "artist": info.get("uploader") or "B 站",
        "duration": info.get("duration") or 0,
        "cover": (info.get("thumbnail") or "").replace("http://", "https://", 1),
    }, audio


def unlink_audio(path):
    # Windows can keep the streaming file open briefly after the browser unloads the audio.
    for delay in (0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 32:
                raise
            time.sleep(delay)
    path.unlink(missing_ok=True)


def create_app(data_dir=ROOT / "data"):
    app = Flask(__name__, static_folder="static")
    app.config.update(TRUSTED_HOSTS=["127.0.0.1", "localhost"], MAX_CONTENT_LENGTH=8192)
    data_dir = Path(data_dir).resolve()
    audio_dir = data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    job_lock = threading.Lock()
    job = {}

    def database():
        connection = sqlite3.connect(data_dir / "library.sqlite3")
        connection.row_factory = sqlite3.Row
        return closing(connection)

    with database() as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                source_id TEXT UNIQUE NOT NULL,
                source TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                duration REAL NOT NULL,
                cover TEXT NOT NULL,
                file TEXT NOT NULL
            )
        """)

    def public_track(row):
        track = dict(row)
        track.pop("file")
        track.pop("source_id")
        track["audio"] = f"/audio/{quote(track['id'])}"
        return track

    def set_job(**updates):
        with job_lock:
            job.update(updates)

    def run_import(url, is_short):
        try:
            if is_short:
                url = resolve_short_link(url)
            with database() as connection:
                existing = connection.execute("SELECT id FROM tracks WHERE source = ?", (url,)).fetchone()
                if existing:
                    set_job(status="done", trackId=existing["id"], duplicate=True)
                    return

                # Only move the completed audio into the library, and undo the move if saving fails.
                with TemporaryDirectory(prefix="import-", dir=data_dir) as staging:
                    track, audio = download_track(url, Path(staging), lambda percent: set_job(percent=percent))
                    existing = connection.execute("SELECT id FROM tracks WHERE source_id = ?", (track["source_id"],)).fetchone()
                    if existing:
                        set_job(status="done", trackId=existing["id"], duplicate=True)
                        return

                    track["id"] = uuid4().hex
                    track["file"] = track["id"] + audio.suffix
                    saved = audio.replace(audio_dir / track["file"])
                    try:
                        with connection:
                            connection.execute("""
                                INSERT INTO tracks (id, source_id, source, title, artist, duration, cover, file)
                                VALUES (:id, :source_id, :source, :title, :artist, :duration, :cover, :file)
                            """, track)
                    except Exception:
                        saved.unlink(missing_ok=True)
                        raise
            set_job(status="done", trackId=track["id"], duplicate=False)
        except ImportFailure as error:
            app.logger.warning("Import failed: %s", error, exc_info=True)
            set_job(status="failed", error=str(error))
        except Exception:
            app.logger.exception("Unexpected import failure")
            set_job(status="failed", error="无法保存音频，请检查磁盘空间和项目目录权限后重试。")

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/tracks")
    def tracks():
        with database() as connection:
            return jsonify([public_track(row) for row in connection.execute("SELECT * FROM tracks ORDER BY rowid")])

    @app.get("/api/import")
    def import_status():
        with job_lock:
            return jsonify(job)

    @app.post("/api/import")
    def start_import():
        payload = request.get_json(silent=True)
        value = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            return jsonify(error="请先粘贴 B 站视频链接。"), 400
        try:
            url, is_short = parse_link(value.strip())
        except ImportFailure as error:
            return jsonify(error=str(error)), 400

        with job_lock:
            if job.get("status") == "running":
                return jsonify(error="正在导入一首歌，请稍等。"), 409
            job.clear()
            job.update(id=uuid4().hex, status="running", percent=None)
            started = dict(job)
            threading.Thread(target=run_import, args=(url, is_short), daemon=True).start()
        return jsonify(started), 202

    @app.get("/audio/<track_id>")
    def stream(track_id):
        with database() as connection:
            row = connection.execute("SELECT file FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            abort(404)
        path = audio_dir / row["file"]
        if not path.is_file():
            abort(404)
        return send_file(path, mimetype=AUDIO_TYPES[path.suffix], conditional=True)

    @app.delete("/api/tracks/<track_id>")
    def remove_track(track_id):
        with database() as connection, connection:
            # Lock writes before reading the row so parallel deletions see a consistent library.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT file FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if row is not None:
                try:
                    unlink_audio(audio_dir / row["file"])
                except OSError:
                    app.logger.exception("Unable to remove audio file")
                    return jsonify(error="无法删除音频文件，请稍后重试。"), 409
                connection.execute("DELETE FROM tracks WHERE id = ?", (track_id,))
        return "", 204

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="启动 B 面音乐播放器")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    with make_server("127.0.0.1", args.port, create_app(), threaded=True) as server:
        address = f"http://127.0.0.1:{server.server_port}"
        print(f"B 面音乐播放器：{address}", flush=True)
        if args.open:
            webbrowser.open(address)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
