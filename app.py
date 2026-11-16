import argparse
import logging
import re
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing
from importlib.util import find_spec
from io import StringIO
from pathlib import Path
from shutil import which
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
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v="
YOUTUBE_ID = re.compile(r"[0-9A-Za-z_-]{11}")
YOUTUBE_COOKIE_FILE = "youtube-cookies.txt"
YOUTUBE_COOKIE_ROW = re.compile(
    r"(?:#HttpOnly_)?(?P<domain>\.?(?:[A-Za-z0-9-]+\.)*youtube\.com)\t(?P<subdomains>TRUE|FALSE)"
    r"\t/[^\t]*\t(?:TRUE|FALSE)\t[0-9]{0,15}\t[^\t]+\t[^\t]*",
    re.ASCII | re.IGNORECASE,
)
MAX_AUDIO_SIZE = 200 * 1024 * 1024
AUDIO_TYPES = {".m4a": "audio/mp4", ".webm": "audio/webm"}


class ImportFailure(Exception):
    pass


def youtube_video_id(link):
    path = link.path.removesuffix("/")
    if link.hostname == "youtu.be":
        video_id = path.removeprefix("/")
    elif link.hostname in YOUTUBE_HOSTS:
        if path == "/watch":
            video_id = parse_qs(link.query).get("v", [""])[0]
        else:
            match = re.fullmatch(r"/(?:shorts|live|embed)/([^/]+)", path)
            video_id = match[1] if match else ""
    else:
        return None

    return video_id if YOUTUBE_ID.fullmatch(video_id) else None


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

            video_id = youtube_video_id(link)
            if video_id:
                return YOUTUBE_WATCH_URL + video_id, False
    except ValueError:
        pass

    raise ImportFailure("请输入 B 站或 YouTube 的单个视频链接。")


class ResolvedVideo(Exception):
    pass


class BilibiliRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Validate every redirect before opening it, since the original URL came from the user.
        try:
            target, is_short = parse_link(urljoin(req.full_url, newurl))
            if target.startswith(YOUTUBE_WATCH_URL):
                raise ImportFailure("该 B 站短链接没有指向 B 站视频，请粘贴原视频链接。")
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


def load_youtube_cookies(path):
    max_size = 1024 * 1024
    try:
        with path.open("rb") as file:
            contents = file.read(max_size + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ImportFailure("无法读取 YouTube 登录文件，请检查文件权限。") from None

    if len(contents) > max_size:
        raise ImportFailure("YouTube 登录文件过大，请只导出 youtube.com 的 Cookie。")
    try:
        lines = contents.decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise ImportFailure("YouTube 登录文件格式有误，请重新导出 Netscape 格式的 Cookie。") from None

    # Pass only valid YouTube entries to yt-dlp so malformed lines cannot log session values.
    cookies = []
    for line in lines:
        match = YOUTUBE_COOKIE_ROW.fullmatch(line)
        if match and match["domain"].startswith(".") == (match["subdomains"] == "TRUE"):
            cookies.append(line)
    if not cookies:
        raise ImportFailure("登录文件中没有可用的 YouTube Cookie，请重新导出 Netscape 格式的 Cookie。")

    # yt-dlp saves its updated jar on exit. A memory stream keeps the user's file intact.
    return StringIO("# Netscape HTTP Cookie File\n" + "\n".join(cookies) + "\n")


def youtube_import_error(error, using_cookies):
    message = str(error).lower()
    if "private video" in message or "video is private" in message or "members-only" in message:
        return "此 YouTube 视频需要账号访问权限。请确认当前账号能播放，再按 README 配置或更新登录文件。"
    if "sign in" in message or "login required" in message:
        if using_cookies:
            return "YouTube 仍要求登录验证。请在浏览器中确认当前账号能播放该视频，再重新导出登录文件。"
        return "YouTube 要求登录验证。请在浏览器中完成登录，按 README 将登录文件保存到 data/youtube-cookies.txt，再重新导入。"
    if "429" in message or "too many requests" in message:
        return "YouTube 限制了当前网络的请求（HTTP 429），请稍后重试。"
    return "YouTube 导入失败，请在浏览器中确认该视频能正常播放，并检查播放器的网络连接。"


def download_track(url, destination, report, *, cookie_path):
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
    is_youtube = url.startswith(YOUTUBE_WATCH_URL)
    extractor = "Youtube" if is_youtube else "BiliBili"
    platform = "YouTube" if is_youtube else "B 站"
    if is_youtube:
        if find_spec("yt_dlp_ejs") is None:
            raise ImportFailure("缺少 YouTube 解析组件，请通过 start.bat 重启以安装依赖。")
        if not (which("deno") or which("node")):
            raise ImportFailure("解析 YouTube 需要 Node.js 22+ 或 Deno 2.3+，安装后请重启播放器。")

        def check_live(info, *, incomplete):
            if info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
                raise ImportFailure("请等 YouTube 直播回放处理完毕后再导入。")

        options.update(js_runtimes={"deno": {}, "node": {}}, match_filter=check_live)
        cookies = load_youtube_cookies(cookie_path)
        if cookies is not None:
            options["cookiefile"] = cookies

    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, ie_key=extractor, download=True)
    except DownloadError as error:
        if "Requested format is not available" in str(error):
            raise ImportFailure("此视频没有可直接播放的音轨，请尝试其他视频。") from error
        message = youtube_import_error(error, "cookiefile" in options) if is_youtube else (
            "B 站导入失败，请确认应用可访问该平台，且视频无需登录即可播放。"
        )
        raise ImportFailure(message) from error

    suffix = f".{(info or {}).get('ext', '')}"
    audio = destination / f"track{suffix}"
    if suffix not in AUDIO_TYPES or not audio.is_file() or not audio.stat().st_size:
        raise ImportFailure("没有下载到音频，请确认视频可播放且音频小于 200 MB。")

    # Keep existing Bilibili IDs for deduplication across AV/BV links and distinguish YouTube IDs.
    return {
        "source_id": f"youtube:{info['id']}" if is_youtube else info["id"],
        "source": url,
        "title": info.get("title") or "未命名音轨",
        "artist": info.get("uploader") or info.get("channel") or platform,
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
        # Check and upgrade older libraries under the same write transaction.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id TEXT PRIMARY KEY,
                source_id TEXT UNIQUE NOT NULL,
                source TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                duration REAL NOT NULL,
                cover TEXT NOT NULL,
                file TEXT NOT NULL,
                sort_position INTEGER
            )
        """)
        columns = {column["name"] for column in connection.execute("PRAGMA table_info(tracks)")}
        if "sort_position" not in columns:
            connection.execute("ALTER TABLE tracks ADD COLUMN sort_position INTEGER")

    def ordered_tracks(connection):
        # Tracks without a saved position follow the sorted tracks in import order.
        return connection.execute("SELECT * FROM tracks ORDER BY sort_position NULLS LAST, rowid")

    def public_track(row):
        track = dict(row)
        track.pop("file")
        track.pop("source_id")
        track.pop("sort_position")
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
                    track, audio = download_track(
                        url, Path(staging), lambda percent: set_job(percent=percent),
                        cookie_path=data_dir / YOUTUBE_COOKIE_FILE,
                    )
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
            return jsonify([public_track(row) for row in ordered_tracks(connection)])

    @app.put("/api/tracks/<track_id>/position")
    def set_track_position(track_id):
        payload = request.get_json(silent=True)
        position = payload.get("position") if isinstance(payload, dict) else None
        if type(position) is not int or position < 1:
            return jsonify(error="歌曲位置必须为正整数。"), 400

        with database() as connection, connection:
            # Serialize reordering with imports and deletions so positions use the latest library.
            connection.execute("BEGIN IMMEDIATE")
            track_ids = [row["id"] for row in ordered_tracks(connection)]
            if track_id not in track_ids:
                return jsonify(error="歌曲已不在播放列表中。"), 404
            if position > len(track_ids):
                return jsonify(error="歌曲位置超出播放列表范围。"), 400

            current_index = track_ids.index(track_id)
            if current_index != position - 1:
                track_ids.pop(current_index)
                track_ids.insert(position - 1, track_id)
                connection.executemany(
                    "UPDATE tracks SET sort_position = ? WHERE id = ? AND sort_position IS NOT ?",
                    ((index, song_id, index) for index, song_id in enumerate(track_ids)),
                )
        return "", 204

    @app.get("/api/import")
    def import_status():
        with job_lock:
            return jsonify(job)

    @app.post("/api/import")
    def start_import():
        payload = request.get_json(silent=True)
        value = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value.strip():
            return jsonify(error="请先粘贴 B 站或 YouTube 视频链接。"), 400
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
