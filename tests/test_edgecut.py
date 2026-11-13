"""Cut generated videos and compare every decoded frame with the source range."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import edgecut  # noqa: E402

H264 = ('-c:v', 'libx264', '-g', '25', '-bf', '2')


def make_video(path, *codec_args, seconds=8):
    """A test video with a keyframe every second, B-frames, and AAC audio."""
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=160x90:r=25', '-f', 'lavfi',
                    '-i', 'sine=f=440:r=48000', '-t', str(seconds), *(codec_args or H264), '-c:a', 'aac', str(path)],
                   check=True)


def frame_hashes(path, start=None, end=None):
    """MD5 of each decoded video frame, optionally only frames shown in [start, end)."""
    select = ['-vf', f'select=gte(t\\,{start})*lt(t\\,{end})'] if start is not None else []
    output = subprocess.check_output(['ffmpeg', '-v', 'error', '-copyts', '-i', str(path), '-map', '0:v', *select,
                                      '-fps_mode', 'passthrough', '-f', 'framemd5', '-'], text=True)
    return [line.rsplit(',', 1)[1].strip() for line in output.splitlines() if not line.startswith('#')]


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg and FFprobe required')
class EdgecutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def cut(self, source, start, end):
        events = []
        work = self.directory / f'work-{len(list(self.directory.glob("work-*")))}'
        work.mkdir()
        output = edgecut.cut(source, start, end, work, event=lambda status, **d: events.append((status, d)))
        return output, events

    def assert_exact_clip(self, output, source, start, end, copied):
        """Same frames as the source range; the copied ones bit-identical."""
        expected, actual = frame_hashes(source, start, end), frame_hashes(output)
        self.assertEqual(len(actual), len(expected))
        self.assertGreaterEqual(sum(a == b for a, b in zip(actual, expected)), copied)
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                                   'stream=codec_type,start_time', '-of', 'json', str(output)]))
        self.assertEqual({s['codec_type'] for s in info['streams']}, {'video', 'audio'})
        self.assertTrue(all(abs(float(s['start_time'])) < 0.03 for s in info['streams']))

    def test_copies_between_keyframes(self):
        source = self.directory / 'source 日本語.mp4'
        make_video(source)
        output, events = self.cut(source, 0.37, 5.62)
        plan = dict(events)['plan']
        self.assertEqual((plan['k1'], plan['k2'], plan['body_frames']), (1.0, 5.0, 100))
        self.assert_exact_clip(output, source, 0.37, 5.62, copied=100)

    def test_matroska_hevc_10_bit_and_ranges_on_keyframes(self):
        cases = [('matroska.mkv', H264, 0.37, 5.62, 100),
                 ('hevc.mp4', ('-c:v', 'libx265', '-x265-params',
                               'keyint=25:min-keyint=25:no-open-gop=1:log-level=error'), 0.37, 5.62, 100),
                 ('10 bit.mp4', (*H264, '-pix_fmt', 'yuv420p10le'), 0.37, 5.62, 100),
                 ('on keyframes.mp4', H264, 1, 3, 50),
                 ('from zero.mp4', H264, 0, 2.3, 50)]
        for name, codec_args, start, end, copied in cases:
            with self.subTest(name):
                source = self.directory / name
                make_video(source, *codec_args)
                self.assert_exact_clip(self.cut(source, start, end)[0], source, start, end, copied)

    def test_unsuitable_sources_are_rejected_before_running(self):
        open_gop = ('-c:v', 'libx265', '-x265-params', 'keyint=25:min-keyint=25:open-gop=1:log-level=error')
        cases = [('xvid.avi', ('-c:v', 'mpeg4'), 0.37, 5.62, 'unsupported video codec mpeg4'),
                 ('open gop.mp4', open_gop, 0.37, 5.62, 'fewer than two IDR keyframes'),
                 ('short.mp4', H264, 1.2, 1.8, 'fewer than two IDR keyframes'),
                 ('past end.mp4', H264, 1, 9, 'range is outside the source duration')]
        for name, codec_args, start, end, reason in cases:
            with self.subTest(name):
                source = self.directory / name
                make_video(source, *codec_args)
                run = Mock()
                with self.assertRaisesRegex(edgecut.Unsupported, reason):
                    edgecut.cut(source, start, end, self.directory, run)
                run.assert_not_called()

    def test_failed_step_or_check_raises(self):
        source = self.directory / 'source.mp4'
        make_video(source)
        with patch('edgecut.mux_command', return_value=['ffmpeg', '-v', 'error', '-i', '/nonexistent', '-f', 'null', '-']):
            with self.assertRaisesRegex(edgecut.CutFailed, 'mux step exited'):
                self.cut(source, 0.37, 5.62)
        with patch('edgecut.verify', side_effect=edgecut.CutFailed('decode errors near a join')):
            with self.assertRaisesRegex(edgecut.CutFailed, 'decode errors'):
                self.cut(source, 0.37, 5.62)

    def test_command_line(self):
        source, output = self.directory / 'source.mp4', self.directory / 'clip.mp4'
        make_video(source)
        self.assertEqual(edgecut.main([str(source), '0:00.37', '5.62', str(output)]), 0)
        self.assert_exact_clip(output, source, 0.37, 5.62, copied=100)
        with patch('sys.stderr'):
            self.assertEqual(edgecut.main([str(source), '1.2', '1.8', str(self.directory / 'short.mp4')]), 2)
        self.assertFalse((self.directory / 'short.mp4').exists())

    def test_parse_time(self):
        self.assertEqual(edgecut.parse_time('83.5'), 83.5)
        self.assertEqual(edgecut.parse_time('1:23.5'), 83.5)
        self.assertEqual(edgecut.parse_time('1:01:23'), 3683)
        for value in ['-1', '1:60', '1:61:00', 'abc', '1:2:3:4']:
            with self.assertRaises(Exception):
                edgecut.parse_time(value)


if __name__ == '__main__':
    unittest.main()
