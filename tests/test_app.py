import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from app import BilibiliRedirects, ImportFailure, ResolvedVideo, create_app, parse_link, unlink_audio


VIDEO = "https://www.bilibili.com/video/BV1B7411m7LV/?p=1"


class LinkTests(unittest.TestCase):
    def test_normalizes_tracking_parameters_and_keeps_page(self):
        link = "https://m.bilibili.com/video/BV1B7411m7LV?p=2&from=share#start"
        self.assertEqual(parse_link(link), (VIDEO.replace("p=1", "p=2"), False))
        self.assertEqual(parse_link("http://b23.tv/Example"), ("https://b23.tv/Example", True))

    def test_rejects_non_video_urls(self):
        for link in ("https://bilibili.com.example.net/video/BV1B7411m7LV",
                     "https://user@bilibili.com/video/BV1B7411m7LV",
                     "https://www.bilibili.com/video/BV1B7411m7LV?p=0"):
            with self.subTest(link=link), self.assertRaises(ImportFailure):
                parse_link(link)

    def test_resolves_only_bilibili_redirects(self):
        handler = BilibiliRedirects()
        request = Request("https://b23.tv/Example")
        with tempfile.TemporaryFile() as response, self.assertRaises(ResolvedVideo) as redirect:
            handler.redirect_request(request, response, 302, "Found", {}, VIDEO)
        self.assertEqual(str(redirect.exception), VIDEO)
        with tempfile.TemporaryFile() as response, self.assertRaises(ImportFailure):
            handler.redirect_request(request, response, 302, "Found", {}, "http://127.0.0.1/")


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
        def download(url, destination, progress):
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
        def download(url, destination, progress):
            (destination / "track.m4a.part").write_bytes(b"partial")
            raise ImportFailure("视频无法访问")

        with patch("app.download_track", side_effect=download), self.assertLogs("app", level="WARNING"):
            self.assertEqual(self.client.post("/api/import", json={"url": VIDEO}).status_code, 202)
            self.assertEqual(self.wait_for_import()["error"], "视频无法访问")
        self.assertEqual(self.client.get("/api/tracks").json, [])
        self.assertEqual(list(self.folder.glob("import-*")), [])

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

        def download(url, destination, progress):
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
