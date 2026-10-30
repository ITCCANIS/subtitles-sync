"""Run with: python -m unittest test_sync_core -v"""
import os
import tempfile
import unittest
from unittest import mock

import sync_core as sc


def touch(folder, name, content="1\n00:00:01,000 --> 00:00:02,000\nhi\n"):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class FindSubtitlesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.video = touch(self.d, "Show S01E1.mkv", "")

    def tearDown(self):
        self.tmp.cleanup()

    def names(self):
        return [os.path.basename(p) for p in sc.find_subtitles(self.video)]

    def test_matches_exact_and_language_tagged(self):
        touch(self.d, "Show S01E1.srt")
        touch(self.d, "Show S01E1.en.srt")
        touch(self.d, "Show S01E1.fr.forced.ass")
        self.assertEqual(
            self.names(),
            ["Show S01E1.en.srt", "Show S01E1.fr.forced.ass", "Show S01E1.srt"],
        )

    def test_does_not_match_other_episodes(self):
        # Old glob "Show S01E1*.srt" matched these and deleted them.
        touch(self.d, "Show S01E10.srt")
        touch(self.d, "Show S01E11.en.srt")
        self.assertEqual(self.names(), [])

    def test_ignores_synced_outputs_and_other_files(self):
        touch(self.d, "Show S01E1.synced.srt")
        touch(self.d, "Show S01E1.en.synced.srt")
        touch(self.d, "Show S01E1.nfo")
        self.assertEqual(self.names(), [])

    def test_orig_backup_reported_under_original_name(self):
        touch(self.d, "Show S01E1.en.srt.orig")
        self.assertEqual(self.names(), ["Show S01E1.en.srt"])


class NamingTest(unittest.TestCase):
    def test_synced_path(self):
        self.assertEqual(sc.synced_path_for("/a/M.en.srt"), "/a/M.en.synced.srt")
        self.assertEqual(sc.synced_path_for("/a/M.ass"), "/a/M.synced.ass")

    def test_excluded(self):
        ex = [os.path.abspath("/media/Silo")]
        self.assertTrue(sc.is_path_excluded("/media/Silo/S01E01.mkv", ex))
        self.assertFalse(sc.is_path_excluded("/media/Silo Extra/a.mkv", ex))


class CommandTest(unittest.TestCase):
    def test_split_flags(self):
        cfg = sc.Config(split_penalty="7", max_offset_seconds="300",
                        parallel_workers="6", extra_args=["--vad", "silero"])
        cmd = sc.build_ffsubsync_cmd("ref.wav", "in.srt", "out.srt", cfg, split=True)
        self.assertEqual(cmd[:6], ["ffsubsync", "ref.wav", "-i", "in.srt", "-o", "out.srt"])
        self.assertIn("--split-penalty", cmd)
        self.assertEqual(cmd[-2:], ["--vad", "silero"])
        no_split = sc.build_ffsubsync_cmd("ref.wav", "in.srt", "out.srt", cfg, split=False)
        self.assertNotIn("--split-penalty", no_split)

    def test_pick_text_stream_skips_pgs_and_forced(self):
        streams = [
            {"codec_type": "video"},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            {"codec_type": "subtitle", "codec_name": "subrip",
             "disposition": {"forced": 1}},
            {"codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"NUMBER_OF_FRAMES": "40"}},
            {"codec_type": "subtitle", "codec_name": "ass",
             "tags": {"NUMBER_OF_FRAMES": "700"}},
        ]
        self.assertEqual(sc.pick_text_subtitle_stream(streams), 3)
        self.assertIsNone(sc.pick_text_subtitle_stream(streams[:2]))


def srt(starts):
    blocks = []
    for i, s in enumerate(starts, 1):
        def ts(t):
            ms = int(round(t * 1000))
            return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
        blocks.append(f"{i}\n{ts(s)} --> {ts(s + 1.5)}\nline {i}\n")
    return "\n".join(blocks)


class SanityCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.cfg = sc.Config(max_split_spread=30.0)
        self.inp = touch(self.d, "in.srt", srt([10, 20, 30, 40, 50, 60]))

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, out_starts):
        out = touch(self.d, "out.srt", srt(out_starts))
        return sc.check_sync_result(self.inp, out, self.cfg)

    def test_accepts_tv_style_breaks(self):
        # +2s for the first act, then +4.5s after an act break.
        self.assertEqual(self.check([12, 22, 32, 44.5, 54.5, 64.5]), "")

    def test_rejects_huge_spread(self):
        # The kind of result seen on S02E02: blocks minutes apart.
        self.assertIn("spread", self.check([10 - 229 + 300, 20 + 70, 30 + 89, 40, 50, 60]))

    def test_rejects_lines_out_of_order(self):
        self.assertIn("before the previous", self.check([10, 20, 30, 25, 35, 45]))


class JobFlowTest(unittest.TestCase):
    """End-to-end job flow with ffmpeg/ffsubsync mocked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.video = touch(self.d, "Ep.mkv", "video")
        self.cfg = sc.Config(excluded_paths=[], api_key="", tmp_dir=self.d,
                             split_penalty="7", originals="keep")
        patch_ref = mock.patch.object(sc, "extract_reference", return_value="ref.wav")
        patch_ref.start()
        self.addCleanup(patch_ref.stop)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, succeed_when_split=True):
        def run(cmd, sub_out, timeout):
            split = "--split-penalty" in cmd
            self.calls.append(split)
            if split and not succeed_when_split:
                return False
            with open(sub_out, "w", encoding="utf-8") as f:
                f.write("synced")
            return True
        return run

    def test_sync_keeps_original_as_orig(self):
        touch(self.d, "Ep.en.srt")
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run()):
            self.assertEqual(sc.sync_subtitles_for_video(self.video, self.cfg), 1)
        files = sorted(os.listdir(self.d))
        self.assertIn("Ep.en.synced.srt", files)
        self.assertIn("Ep.en.srt.orig", files)
        self.assertNotIn("Ep.en.srt", files)
        self.assertEqual(self.calls, [True])

        # Second run: nothing to do.
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run()):
            self.assertEqual(sc.sync_subtitles_for_video(self.video, self.cfg), 0)

    def test_force_resyncs_from_orig(self):
        touch(self.d, "Ep.en.srt.orig", "original")
        touch(self.d, "Ep.en.synced.srt", "old bad sync")
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run()):
            self.assertEqual(
                sc.sync_subtitles_for_video(self.video, self.cfg, force=True), 1)
        with open(os.path.join(self.d, "Ep.en.synced.srt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "synced")
        self.assertTrue(os.path.exists(os.path.join(self.d, "Ep.en.srt.orig")))

    def test_force_resyncs_old_output_without_original(self):
        touch(self.d, "Ep.synced.srt", "old bad sync")
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run()):
            self.assertEqual(
                sc.sync_subtitles_for_video(self.video, self.cfg, force=True), 1)

    def test_falls_back_to_single_offset(self):
        touch(self.d, "Ep.srt")
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run(succeed_when_split=False)):
            self.assertEqual(sc.sync_subtitles_for_video(self.video, self.cfg), 1)
        self.assertEqual(self.calls, [True, False])

    def test_scrambled_split_result_falls_back(self):
        touch(self.d, "Ep.srt", srt([10, 20, 30, 40]))

        def run(cmd, sub_out, timeout):
            split = "--split-penalty" in cmd
            self.calls.append(split)
            starts = [200, 20, 300, 40] if split else [12, 22, 32, 42]
            with open(sub_out, "w", encoding="utf-8") as f:
                f.write(srt(starts))
            return True

        with mock.patch.object(sc, "run_ffsubsync", run):
            self.assertEqual(sc.sync_subtitles_for_video(self.video, self.cfg), 1)
        self.assertEqual(self.calls, [True, False])
        out = sc.read_cue_starts(os.path.join(self.d, "Ep.synced.srt"))
        self.assertEqual(out, [12, 22, 32, 42])

    def test_failure_leaves_original(self):
        touch(self.d, "Ep.srt")
        with mock.patch.object(sc, "run_ffsubsync", lambda *a: False):
            self.assertEqual(sc.sync_subtitles_for_video(self.video, self.cfg), 0)
        self.assertEqual(sorted(os.listdir(self.d)), ["Ep.mkv", "Ep.srt"])

    def test_delete_mode(self):
        touch(self.d, "Ep.srt")
        self.cfg.originals = "delete"
        with mock.patch.object(sc, "run_ffsubsync", self.fake_run()):
            sc.sync_subtitles_for_video(self.video, self.cfg)
        self.assertEqual(sorted(os.listdir(self.d)), ["Ep.mkv", "Ep.synced.srt"])


if __name__ == "__main__":
    unittest.main()
