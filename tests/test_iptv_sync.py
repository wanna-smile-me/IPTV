import importlib.util
import argparse
import functools
import http.server
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen


SCRIPT = Path(__file__).parents[1] / ".github" / "workflows" / "iptv_sync.py"
SPEC = importlib.util.spec_from_file_location("iptv_sync", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
# Dataclasses need the dynamically loaded module registered before execution.
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class IptvSyncTests(unittest.TestCase):
    def test_parse_preserves_chinese_metadata_and_deduplicates_exact_entries(self):
        text = """#EXTM3U
#EXTINF:-1 tvg-name="央视一套" tvg-logo="https://logo/央视.png" group-title="央视频道",央视一套
https://one.example/live.m3u8
#EXTINF:-1 tvg-name="央视一套" tvg-logo="https://logo/央视.png" group-title="央视频道",央视一套
https://one.example/live.m3u8
#EXTINF:-1 tvg-name="央视一套" tvg-logo="https://logo/央视.png" group-title="央视频道",央视一套
https://backup.example/live.m3u8
"""
        entries = MODULE.deduplicate(MODULE.parse_m3u(text))
        self.assertEqual(len(entries), 2)
        self.assertIn("央视一套", entries[0].extinf)
        self.assertEqual(entries[1].url, "https://backup.example/live.m3u8")

    def test_only_exact_blocked_urls_are_removed(self):
        entries = MODULE.parse_m3u("""#EXTM3U
#EXTINF:-1,Blocked
http://173.208.212.130:8181/1080p/cctv6.m3u8
#EXTINF:-1,SameHostAllowed
http://173.208.212.130:8181/1080p/cctv1.m3u8
#EXTINF:-1,OtherHost
http://example.test/live.m3u8
""")
        kept, blocked = MODULE.filter_blocked_urls(
            entries, {"http://173.208.212.130:8181/1080p/cctv6.m3u8"}
        )
        self.assertEqual(blocked, 1)
        self.assertEqual(
            [entry.extinf.split(",", 1)[1] for entry in kept],
            ["SameHostAllowed", "OtherHost"],
        )

    def test_cctv_fallback_keeps_channels_when_validation_fails(self):
        candidates = [
            MODULE.Entry(
                f'#EXTINF:-1 group-title="央视",CCTV-{number}',
                f"https://example.test/cctv{number}.m3u8",
            )
            for number in range(1, 16)
        ]
        entries, annotations, missing = MODULE.build_yw_entries(
            valid=[],
            candidates=candidates,
            previous=[],
            blocked_urls=set(),
        )
        self.assertEqual(missing, [])
        self.assertEqual(
            {MODULE.cctv_channel_number(entry) for entry in entries},
            set(range(1, 16)),
        )
        self.assertEqual(len(annotations), 15)

    def test_render_removes_quality_suffix_and_keeps_comment_annotation(self):
        entries = [
            MODULE.Entry(
                '#EXTINF:-1 group-title="央视",CCTV-1 (720p)',
                "https://example.test/cctv1.m3u8",
            ),
            MODULE.Entry(
                '#EXTINF:-1 group-title="央视",CCTV-6',
                "https://example.test/cctv6.m3u8",
            ),
        ]
        rendered = MODULE.render(entries)
        self.assertIn("# Original-Name: CCTV-1 (720p)", rendered)
        self.assertIn('group-title="央视",CCTV-1\n', rendered)
        self.assertNotIn("group-title=\"央视\",CCTV-1 (720p)", rendered)
        self.assertIn('group-title="央视",CCTV-6\n', rendered)
        self.assertEqual(len(MODULE.parse_m3u(rendered)), 2)

    def test_cctv_and_satellite_filter(self):
        entries = MODULE.parse_m3u("""#EXTM3U
#EXTINF:-1 group-title="央视频道",CCTV1
https://example.invalid/cctv
#EXTINF:-1 group-title="卫视频道",湖南台
https://example.invalid/hunan
#EXTINF:-1 group-title="地方频道",新闻综合
https://example.invalid/local
#EXTINF:-1 tvg-name="CGTN",国际频道
https://example.invalid/cgtn
        """)
        selected = MODULE.filter_cctv_and_satellite(entries)
        self.assertEqual([entry.extinf.split(",", 1)[-1] for entry in selected],
                         ["CCTV1", "湖南台"])

    def test_cctv_satellite_filter_excludes_requested_channels(self):
        entries = MODULE.parse_m3u("""#EXTM3U
#EXTINF:-1 group-title="央视频道",CGTN英语
https://example.invalid/cgtn
#EXTINF:-1 group-title="央视频道",上海外语
https://example.invalid/language
#EXTINF:-1 group-title="央视频道",游戏风云
https://example.invalid/game
#EXTINF:-1 group-title="央视频道",魅力足球
https://example.invalid/football
#EXTINF:-1 group-title="央视频道",熊猫直播
https://example.invalid/panda
#EXTINF:-1 group-title="央视频道",CCTV4 中文国际
https://example.invalid/cctv4
#EXTINF:-1 group-title="卫视频道",湖南卫视
https://example.invalid/hunan
""")
        selected = MODULE.filter_cctv_and_satellite(entries)
        self.assertEqual(
            [entry.extinf.split(",", 1)[-1] for entry in selected],
            ["CCTV4 中文国际", "湖南卫视"],
        )

    def test_yw_groups_are_normalized(self):
        entries = MODULE.parse_m3u("""#EXTM3U
#EXTINF:-1 group-title="卡通频道",CCTV14
https://example.invalid/cctv14
#EXTINF:-1 group-title="General",湖南卫视
https://example.invalid/hunan
""")
        normalized = [
            MODULE.normalize_yw_group(entry)
            for entry in MODULE.filter_cctv_and_satellite(entries)
        ]
        self.assertIn('group-title="央视"', normalized[0].extinf)
        self.assertIn('group-title="卫视"', normalized[1].extinf)
        self.assertNotIn("卡通频道", normalized[0].extinf)
        self.assertNotIn("General", normalized[1].extinf)

    def test_invalid_playlist_is_rejected(self):
        for text in (
            "#EXTM3U\nhttps://example.invalid/live.m3u8\n",
            "#EXTINF:-1,TV\nhttps://example.invalid/live",
            "#EXTM3U\n#EXTINF:-1,TV\n#EXTINF:-1,Other\nhttps://example.invalid/live",
            "#EXTM3U\n#EXTINF:-1,TV\n",
            "#EXTM3U\n#EXTINF:-1,TV\nfile:///etc/passwd",
            "#EXTM3U\n#EXTINF:-1,TV\n#EXTVLCOPT:http-referrer=secret\nhttps://example.invalid/live",
        ):
            with self.subTest(text=text), self.assertRaises(MODULE.SyncError):
                MODULE.parse_m3u(text)

    def test_retry_then_success(self):
        entry = MODULE.Entry("#EXTINF:-1,测试广播", "https://radio.example/live")
        with patch.object(MODULE, "run_ffmpeg", side_effect=[(False, "timeout"), (True, "ok")]) as check:
            self.assertEqual(MODULE.check_entry(entry, timeout=1, retries=1), (True, "ok"))
            self.assertEqual(check.call_count, 2)

    def test_retry_failure_is_not_permanently_blacklisted(self):
        entry = MODULE.Entry("#EXTINF:-1,TV", "https://example.invalid/live")
        with patch.object(MODULE, "run_ffmpeg", return_value=(False, "timeout")) as check:
            self.assertEqual(MODULE.check_entry(entry, 1, 1), (False, "timeout"))
            self.assertEqual(check.call_count, 2)
        with patch.object(MODULE, "run_ffmpeg", return_value=(True, "ok")):
            self.assertEqual(MODULE.check_entry(entry, 1, 1), (True, "ok"))

    def test_publish_guard_keeps_small_results_blocked(self):
        with self.assertRaises(MODULE.SyncError):
            MODULE.ensure_safe_to_publish(input_count=10, valid_count=4, old_count=10)
        MODULE.ensure_safe_to_publish(input_count=10, valid_count=5, old_count=10)
        with self.assertRaises(MODULE.SyncError):
            MODULE.ensure_safe_to_publish(input_count=10, valid_count=1, old_count=0)
        MODULE.ensure_safe_to_publish(input_count=10, valid_count=2, old_count=0)
        with self.assertRaises(MODULE.SyncError):
            MODULE.ensure_safe_to_publish(input_count=10, valid_count=0, old_count=0)

    def test_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "IPTV.m3u"
            self.assertTrue(MODULE.write_if_changed(path, "#EXTM3U\n"))
            self.assertFalse(MODULE.write_if_changed(path, "#EXTM3U\n"))

    def test_timeout_and_stderr_are_safe(self):
        with patch.object(MODULE.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 1)):
            self.assertEqual(MODULE.run_ffmpeg("https://example.invalid", 1), (False, "timeout"))
        result = subprocess.CompletedProcess([], 1, "", "403 https://example.invalid/?token=DO_NOT_LEAK")
        with patch.object(MODULE.subprocess, "run", return_value=result) as command:
            self.assertEqual(MODULE.run_ffmpeg("https://example.invalid", 1), (False, "access denied"))
            self.assertEqual(command.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(command.call_args.kwargs["errors"], "replace")

    def test_successful_exit_without_frames_is_rejected(self):
        result = subprocess.CompletedProcess([], 0, "#tb 0: 1/10\n", "")
        with patch.object(MODULE.subprocess, "run", return_value=result):
            self.assertEqual(MODULE.run_ffmpeg("https://example.invalid", 1), (False, "insufficient decoded media"))

    def test_decoded_duration_includes_final_frame_and_rejects_bad_progress(self):
        frames = "".join(f"0, {i}, {i}, 1, 6144, 0x12345678\n" for i in range(30))
        for output, expected in (
            ("#tb 0: 1/10\n" + frames, (True, "ok")),
            ("#tb 0: 1/10\n" + frames.splitlines()[0] + "\n",
             (False, "insufficient decoded media")),
            ("#tb 0: 1/0\n" + frames, (False, "invalid decode progress")),
            ("#tb 0: 1/10\n0, 0, 0, 30, 0, 0x00000000\n",
             (False, "invalid decode progress")),
            ("#tb 0: 1/10\n0, 0\n", (False, "invalid decode progress")),
        ):
            with self.subTest(output=output), patch.object(
                MODULE.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
            ):
                self.assertEqual(MODULE.run_ffmpeg("https://example.invalid", 10), expected)

    def test_interrupted_validation_keeps_previous_playlist(self):
        entry = MODULE.Entry("#EXTINF:-1,TV", "https://example.invalid/live")
        with patch.object(MODULE, "check_entry", side_effect=RuntimeError("secret")):
            with self.assertRaisesRegex(MODULE.SyncError, "validation did not complete"):
                MODULE.check_entries([entry], 1, 1, 1)

    def test_run_failures_and_dry_run_keep_previous_playlist(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "IPTV.m3u"
            sources = Path(directory) / "sources.txt"
            sources.write_text("https://example.invalid/source\n", encoding="utf-8")
            old = "#EXTM3U\n" + "".join(
                f'#EXTINF:-1 group-title="央视",CCTV-{number}\n'
                f"https://example.invalid/cctv{number}.m3u8\n"
                for number in range(1, 16)
            )
            output.write_text(old, encoding="utf-8")
            args = argparse.Namespace(sources=sources, output=output, download_timeout=1, timeout=1,
                                      yw_output=Path(directory) / "ywIPTV.m3u",
                                      workers=1, retries=0, dry_run=False)
            with patch.object(MODULE.shutil, "which", return_value="ffmpeg"):
                with patch.object(MODULE, "fetch", side_effect=OSError("DO_NOT_LEAK")):
                    with self.assertRaisesRegex(MODULE.SyncError, "source 1 download or format validation failed"):
                        MODULE.run(args)
                with patch.object(MODULE, "fetch", return_value=old):
                    with patch.object(MODULE, "check_entries", return_value=([], [{"entry": "1", "reason": "timeout"}])):
                        report = MODULE.run(args)
                        self.assertIn("playlist_blocked", report)
                        self.assertEqual(report["valid_entries"], 0)
                    with patch.object(MODULE, "check_entries", side_effect=MODULE.SyncError("validation did not complete")):
                        with self.assertRaises(MODULE.SyncError):
                            MODULE.run(args)
                    args.dry_run = True
                    with patch.object(MODULE, "check_entries", return_value=(MODULE.parse_m3u(old), [])):
                        self.assertTrue(MODULE.run(args)["would_update"])
                self.assertEqual(output.read_text(encoding="utf-8"), old)

    def test_multiple_sources_and_publish_only_on_change(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "IPTV.m3u"
            sources = Path(directory) / "sources.txt"
            sources.write_text("https://example.invalid/a\nhttps://example.invalid/b\n", encoding="utf-8")
            text = "#EXTM3U\n" + "".join(
                f'#EXTINF:-1 group-title="央视",CCTV-{number}\n'
                f"https://example.invalid/cctv{number}.m3u8\n"
                for number in range(1, 16)
            )
            args = argparse.Namespace(sources=sources, output=output, download_timeout=1, timeout=1,
                                      workers=1, retries=0, dry_run=False)
            with patch.object(MODULE.shutil, "which", return_value="ffmpeg"), \
                 patch.object(MODULE, "fetch", return_value=text), \
                 patch.object(MODULE, "check_entry", return_value=(True, "ok")):
                report = MODULE.run(args)
                self.assertEqual(report["duplicate_entries"], 15)
                self.assertTrue(report["updated"])
                self.assertTrue(report["yw_updated"])
                self.assertTrue((Path(directory) / "ywIPTV.m3u").exists())
                self.assertFalse(MODULE.run(args)["updated"])


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg required for real decode checks")
class DecodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        for filename, duration in (("video.mp4", "4"), ("short.mp4", "1")):
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=c=red:s=64x64:r=10", "-t", duration, "-c:v", "mpeg4",
                "-movflags", "+faststart", str(directory / filename),
            ], check=True, timeout=20)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "sine=frequency=440", "-t", "4", str(directory / "radio.wav"),
        ], check=True, timeout=20)
        (directory / "fake.m3u8").write_text("<html>Not a video</html>", encoding="utf-8")
        handler = functools.partial(QuietHandler, directory=directory)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def test_real_video_decodes_for_three_seconds(self):
        self.assertEqual(MODULE.run_ffmpeg(self.url + "/video.mp4", 10), (True, "ok"))

    def test_http_200_without_media_is_rejected(self):
        with urlopen(self.url + "/fake.m3u8") as response:
            self.assertEqual(response.status, 200)
        self.assertFalse(MODULE.run_ffmpeg(self.url + "/fake.m3u8", 10)[0])

    def test_short_video_is_rejected(self):
        self.assertEqual(MODULE.run_ffmpeg(self.url + "/short.mp4", 10),
                         (False, "insufficient decoded media"))

    def test_audio_only_passes_for_radio_but_not_tv(self):
        self.assertEqual(MODULE.run_ffmpeg(self.url + "/radio.wav", 10, radio=True), (True, "ok"))
        self.assertFalse(MODULE.run_ffmpeg(self.url + "/radio.wav", 10)[0])
        self.assertTrue(MODULE.is_radio(MODULE.Entry('#EXTINF:-1 group-title="广播频道",测试', "http://example.invalid")))


if __name__ == "__main__":
    unittest.main()
