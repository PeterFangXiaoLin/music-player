import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app import BilibiliRedirects, ImportFailure, ResolvedVideo, create_app, load_youtube_cookies, parse_link, unlink_audio


VIDEO = "https://www.bilibili.com/video/BV1B7411m7LV/?p=1"
YOUTUBE_ID = "BaW_jenozKc"
YOUTUBE_VIDEO = f"https://www.youtube.com/watch?v={YOUTUBE_ID}"


class LinkTests(unittest.TestCase):
    def test_normalizes_tracking_parameters_and_keeps_page(self):
        link = "https://m.bilibili.com/video/BV1B7411m7LV?p=2&from=share#start"
        self.assertEqual(parse_link(link), (VIDEO.replace("p=1", "p=2"), False))
        self.assertEqual(parse_link("http://b23.tv/Example"), ("https://b23.tv/Example", True))

    def test_rejects_non_video_urls(self):
        for link in ("https://bilibili.com.example.net/video/BV1B7411m7LV",
                     "https://user@bilibili.com/video/BV1B7411m7LV",
                     "https://www.bilibili.com/video/BV1B7411m7LV?p=0",
                     f"https://youtube.com.example.net/watch?v={YOUTUBE_ID}",
                     "https://www.youtube.com/playlist?list=someplaylist",
                     "https://youtu.be/too_short"):
            with self.subTest(link=link), self.assertRaises(ImportFailure):
                parse_link(link)

    def test_normalizes_youtube_links_to_a_single_video(self):
        for link in (
            f"{YOUTUBE_VIDEO}&list=someplaylist&t=15",
            f"https://m.youtube.com/watch?v={YOUTUBE_ID}",
            f"https://music.youtube.com/watch?v={YOUTUBE_ID}",
            f"https://youtu.be/{YOUTUBE_ID}?si=example",
            f"https://www.youtube.com/shorts/{YOUTUBE_ID}/",
            f"https://www.youtube.com/live/{YOUTUBE_ID}",
            f"https://www.youtube.com/embed/{YOUTUBE_ID}",
        ):
            with self.subTest(link=link):
                self.assertEqual(parse_link(link), (YOUTUBE_VIDEO, False))

    def test_resolves_only_bilibili_redirects(self):
        handler = BilibiliRedirects()
        request = Request("https://b23.tv/Example")
        with tempfile.TemporaryFile() as response, self.assertRaises(ResolvedVideo) as redirect:
            handler.redirect_request(request, response, 302, "Found", {}, VIDEO)
        self.assertEqual(str(redirect.exception), VIDEO)
        with tempfile.TemporaryFile() as response, self.assertRaises(ImportFailure):
            handler.redirect_request(request, response, 302, "Found", {}, "http://127.0.0.1/")
        with tempfile.TemporaryFile() as response, self.assertRaises(ImportFailure):
            handler.redirect_request(request, response, 302, "Found", {}, YOUTUBE_VIDEO)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.client = create_app(self.folder).test_client()

    def wait_for_import(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.client.get("/api/import").json
            if job["status"] != "running":
                return job
            time.sleep(0.01)
        self.fail("Import did not finish")

    def test_import_persists_and_serves_ranges_and_deduplicates(self):
        def download(url, destination, progress, *, cookie_path):
            path = destination / "track.m4a"
            path.write_bytes(b"audio fixture")
            progress(50)
            return dict(source_id="example", source=url, title="示例", artist="测试", duration=2, cover=""), path

        with patch("app.download_track", side_effect=download) as mock_download:
            self.assertEqual(self.client.post("/api/import", json={"url": VIDEO}).status_code, 202)
            job = self.wait_for_import()
            self.assertEqual(job["status"], "done")
            self.assertFalse(job["duplicate"])
            self.assertEqual(self.client.post("/api/import", json={"url": VIDEO}).status_code, 202)
            self.assertTrue(self.wait_for_import()["duplicate"])
            mock_download.assert_called_once()

        tracks = create_app(self.folder).test_client().get("/api/tracks").json
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["id"], job["trackId"])
        with self.client.get(tracks[0]["audio"], headers={"Range": "bytes=0-4"}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.mimetype, "audio/mp4")
            self.assertEqual(response.data, b"audio")

        self.assertEqual(self.client.delete(f"/api/tracks/{job['trackId']}").status_code, 204)
        self.assertEqual(self.client.get(tracks[0]["audio"]).status_code, 404)
        self.assertEqual(list((self.folder / "audio").iterdir()), [])

    def test_failed_import_removes_partial_file_and_does_not_add_track(self):
        def download(url, destination, progress, *, cookie_path):
            (destination / "track.m4a.part").write_bytes(b"partial")
            raise ImportFailure("视频无法访问")

        with patch("app.download_track", side_effect=download), self.assertLogs("app", level="WARNING"):
            self.assertEqual(self.client.post("/api/import", json={"url": VIDEO}).status_code, 202)
            self.assertEqual(self.wait_for_import()["error"], "视频无法访问")
        self.assertEqual(self.client.get("/api/tracks").json, [])
        self.assertEqual(list(self.folder.glob("import-*")), [])

    def test_youtube_import_routes_to_extractor_and_deduplicates_link_variants(self):
        with patch("app.YoutubeDL") as downloader, patch("app.find_spec"), patch("app.which"):
            extractor = downloader.return_value.__enter__.return_value

            def extract_info(url, *, ie_key, download):
                options = downloader.call_args.args[0]
                self.assertEqual(ie_key, "Youtube")
                self.assertTrue(download and options["noplaylist"])
                self.assertIn("node", options["js_runtimes"])
                self.assertNotIn("cookiefile", options)
                with self.assertRaises(ImportFailure):
                    options["match_filter"]({"live_status": "is_live"}, incomplete=False)
                self.assertIsNone(options["match_filter"]({"live_status": "was_live"}, incomplete=False))
                Path(options["outtmpl"] % {"ext": "webm"}).write_bytes(b"youtube audio")
                return dict(id=YOUTUBE_ID, ext="webm", title="演示歌曲", channel="演示频道", duration=10)

            extractor.extract_info.side_effect = extract_info
            response = self.client.post("/api/import", json={"url": f"https://youtu.be/{YOUTUBE_ID}"})
            self.assertEqual(response.status_code, 202)
            job = self.wait_for_import()
            self.assertEqual(job["status"], "done", job)
            self.assertEqual(self.client.post("/api/import", json={"url": YOUTUBE_VIDEO}).status_code, 202)
            self.assertTrue(self.wait_for_import()["duplicate"])
            extractor.extract_info.assert_called_once_with(YOUTUBE_VIDEO, ie_key="Youtube", download=True)

        track, = self.client.get("/api/tracks").json
        self.assertEqual(track["source"], YOUTUBE_VIDEO)
        self.assertEqual(track["artist"], "演示频道")
        with self.client.get(track["audio"]) as response:
            self.assertEqual(response.mimetype, "audio/webm")
            self.assertEqual(response.data, b"youtube audio")

    def test_youtube_login_required_can_be_retried_with_local_cookie_file(self):
        cookie_path = self.folder / "youtube-cookies.txt"
        fake_cookie = "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tTEST_SESSION\tsynthetic"
        cookie_file = "\n".join((
            "# Netscape HTTP Cookie File", fake_cookie,
            ".example.org\tTRUE\t/\tTRUE\t0\tTEST_SESSION\tother-site", "malformed row", "",
        ))

        with patch("app.YoutubeDL") as downloader, patch("app.find_spec"), patch("app.which"):
            extractor = downloader.return_value.__enter__.return_value

            def extract_info(url, *, ie_key, download):
                options = downloader.call_args.args[0]
                cookies = options.get("cookiefile")
                if cookies is None:
                    raise DownloadError("Sign in to confirm you’re not a bot")
                with YoutubeDL({"cookiefile": cookies, "quiet": True}) as real_downloader:
                    self.assertEqual(real_downloader.cookiejar.get_cookie_header(url), "TEST_SESSION=synthetic")
                    self.assertIsNone(real_downloader.cookiejar.get_cookie_header("https://example.org/"))
                Path(options["outtmpl"] % {"ext": "webm"}).write_bytes(b"youtube audio")
                return dict(id=YOUTUBE_ID, ext="webm")

            extractor.extract_info.side_effect = extract_info
            with self.assertLogs("app", level="WARNING"):
                self.assertEqual(self.client.post("/api/import", json={"url": YOUTUBE_VIDEO}).status_code, 202)
                self.assertIn("data/youtube-cookies.txt", self.wait_for_import()["error"])

            cookie_path.write_text(cookie_file, encoding="utf-8")
            self.assertEqual(self.client.post("/api/import", json={"url": YOUTUBE_VIDEO}).status_code, 202)
            self.assertEqual(self.wait_for_import()["status"], "done")
            self.assertEqual(cookie_path.read_text(encoding="utf-8"), cookie_file)

    def test_cookie_file_without_valid_youtube_entry_is_rejected(self):
        cookie_path = self.folder / "youtube-cookies.txt"
        cookie_path.write_text(".notyoutube.com\tTRUE\t/\tTRUE\t0\tTEST_SESSION\tsynthetic\n", encoding="utf-8")
        with self.assertRaisesRegex(ImportFailure, "没有可用的 YouTube Cookie"):
            load_youtube_cookies(cookie_path)

    def test_deletion_retries_windows_sharing_violation(self):
        audio = self.folder / "audio" / "test.m4a"
        audio.write_bytes(b"audio fixture")
        busy = PermissionError("The file is still in use")
        busy.winerror = 32
        with patch.object(type(audio), "unlink", side_effect=[busy, None]) as unlink:
            unlink_audio(audio)
        self.assertEqual(unlink.call_count, 2)

    def test_reorder_preserves_existing_library_and_appends_imports(self):
        with closing(sqlite3.connect(self.folder / "library.sqlite3")) as connection, connection:
            # Recreate the schema used before saved ordering was supported.
            connection.execute("DROP TABLE tracks")
            connection.execute("""
                CREATE TABLE tracks (
                    id TEXT PRIMARY KEY, source_id TEXT UNIQUE, source TEXT UNIQUE,
                    title TEXT, artist TEXT, duration REAL, cover TEXT, file TEXT
                )
            """)
            connection.executemany("INSERT INTO tracks VALUES (?, ?, ?, ?, '', 0, '', ?)", (
                (song_id, song_id, f"test:{song_id}", song_id, f"{song_id}.m4a")
                for song_id in ("third", "first", "second")
            ))
        self.client = create_app(self.folder).test_client()

        def track_ids():
            return [track["id"] for track in self.client.get("/api/tracks").json]

        def download(url, destination, progress, *, cookie_path):
            path = destination / "track.m4a"
            path.write_bytes(b"audio fixture")
            return dict(source_id="new", source=url, title="新歌", artist="测试", duration=2, cover=""), path

        self.assertEqual(track_ids(), ["third", "first", "second"])
        self.assertEqual(self.client.put("/api/tracks/second/position", json={"position": 1}).status_code, 204)
        self.assertEqual(self.client.put("/api/tracks/third/position", json={"position": 3}).status_code, 204)
        self.client = create_app(self.folder).test_client()
        self.assertEqual(track_ids(), ["second", "first", "third"])
        self.assertEqual(self.client.put("/api/tracks/missing/position", json={"position": 1}).status_code, 404)
        for invalid_position in (True, 0, 4, "2"):
            with self.subTest(position=invalid_position):
                response = self.client.put("/api/tracks/first/position", json={"position": invalid_position})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(track_ids(), ["second", "first", "third"])

        self.assertEqual(self.client.delete("/api/tracks/first").status_code, 204)
        with patch("app.download_track", side_effect=download):
            self.assertEqual(self.client.post("/api/import", json={"url": VIDEO}).status_code, 202)
            job = self.wait_for_import()
        self.assertEqual(job["status"], "done")
        self.assertEqual(track_ids(), ["second", "third", job["trackId"]])


if __name__ == "__main__":
    unittest.main()
