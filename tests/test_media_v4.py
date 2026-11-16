"""Unit tests for OmniSync-Downloader (OSDD) v4.0:
- Universal Media Downloader engine (media_downloader.py)
- Platform detection (YouTube, TikTok, Instagram, Twitter/X, Facebook)
- 1-Click Auto Installer (install.bat)
"""

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from media_downloader import (
    MediaDownloadManager,
    detect_media_platform,
    format_duration,
    sanitize_filename,
)


class TestMediaV4(unittest.TestCase):
    def test_platform_detection(self):
        # YouTube standard
        yt = detect_media_platform("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(yt["id"], "youtube")
        self.assertIn("YouTube", yt["name"])

        # YouTube Shorts
        yt_shorts = detect_media_platform("https://www.youtube.com/shorts/abcdef123")
        self.assertEqual(yt_shorts["id"], "youtube_shorts")

        # YouTube Playlist
        yt_pl = detect_media_platform("https://www.youtube.com/playlist?list=PL123456789")
        self.assertEqual(yt_pl["id"], "youtube_playlist")

        # TikTok
        tiktok = detect_media_platform("https://www.tiktok.com/@user/video/123456789")
        self.assertEqual(tiktok["id"], "tiktok")

        # Instagram
        insta = detect_media_platform("https://www.instagram.com/reel/C123456789/")
        self.assertEqual(insta["id"], "instagram")

        # X / Twitter
        twitter = detect_media_platform("https://twitter.com/user/status/123456789")
        self.assertEqual(twitter["id"], "twitter")

        # Facebook
        fb = detect_media_platform("https://www.facebook.com/watch/?v=123456789")
        self.assertEqual(fb["id"], "facebook")

    def test_format_duration(self):
        self.assertEqual(format_duration(0), "--:--")
        self.assertEqual(format_duration(None), "--:--")
        self.assertEqual(format_duration(45), "00:45")
        self.assertEqual(format_duration(75), "01:15")
        self.assertEqual(format_duration(3665), "01:01:05")

    def test_sanitize_filename(self):
        unsafe = 'Video: Test? <Special> *File* | "Name" / 123'
        safe = sanitize_filename(unsafe)
        for char in [":", "?", "<", ">", "*", "|", '"', "/"]:
            self.assertNotIn(char, safe)

    def test_media_download_manager_lifecycle(self):
        mgr = MediaDownloadManager()
        snap = mgr.get_snapshot()
        self.assertEqual(snap["mode"], "media")
        self.assertEqual(snap["status"], "idle")
        self.assertEqual(snap["total_files"], 0)
        self.assertEqual(snap["downloaded_bytes"], 0)

        # Test subscribe / unsubscribe
        q = mgr.subscribe()
        self.assertIsNotNone(q)
        mgr.unsubscribe(q)

        # Test cancel when idle does not crash
        mgr.cancel()
        self.assertEqual(mgr.get_snapshot()["status"], "idle")

    def test_install_script_exists_and_configured(self):
        install_bat = BASE_DIR / "install.bat"
        self.assertTrue(install_bat.is_file(), "install.bat must exist in project root")

        content = install_bat.read_text(encoding="utf-8")
        self.assertIn("requirements.txt", content)
        self.assertIn("playwright", content)
        self.assertIn("start.bat", content)
        self.assertIn("The application is ready to use!", content, "English completion message must be present")


if __name__ == "__main__":
    unittest.main()
