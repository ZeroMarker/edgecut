"""Input validation and failure contracts that do not require FFmpeg."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import edgecut


class TimeTests(unittest.TestCase):
    def test_valid_times(self):
        for text, expected in [('0', 0), ('83.5', 83.5), ('1:23.5', 83.5), ('1:01:23', 3683),
                               ('123:59.5', 7439.5), ('100:00:00', 360000), ('00:00:00.001', 0.001)]:
            with self.subTest(text=text):
                self.assertEqual(edgecut.parse_time(text), expected)

    def test_invalid_times_raise_argument_error(self):
        for text in ['', '-1', '+1', '1:60', '1:61:00', 'abc', '1:2:3:4', 'nan', 'inf', '1e3',
                     ' 1', '1 ', ':1', '1:', '.5', '1.', '1:2.5:3']:
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                edgecut.parse_time(text)


class ProbeTests(unittest.TestCase):
    def test_probe_failures_preserve_error_category(self):
        cases = [(subprocess.CompletedProcess([], 1, b'', b'bad media'), 'ffprobe failed: bad media'),
                 (subprocess.CompletedProcess([], 0, b'not json', b''), 'invalid JSON'),
                 (subprocess.TimeoutExpired('ffprobe', 3), 'over 3 seconds')]
        for outcome, message in cases:
            for error in (edgecut.Unsupported, edgecut.CutFailed):
                with self.subTest(outcome=outcome, error=error), patch('edgecut.subprocess.run') as run:
                    if isinstance(outcome, Exception):
                        run.side_effect = outcome
                    else:
                        run.return_value = outcome
                    with self.assertRaisesRegex(error, message):
                        edgecut.ffprobe(['missing.mp4'], timeout=3, error=error)

    def test_nal_parser_skips_parameter_sets_and_accepts_start_code_lengths(self):
        cases = [('h264', b'\x00\x00\x00\x01\x67abc\x00\x00\x01\x65x', 5),
                 ('h264', b'\x00\x00\x01\x41x', 1),
                 ('hevc', b'\x00\x00\x01\x40x\x00\x00\x00\x01\x26x', 19),
                 ('hevc', b'\x00\x00\x01\x28x', 20),
                 ('hevc', b'\x00\x00\x01\x2ax', 21),
                 ('h264', b'\x00\x00\x01\x67x', None), ('h264', b'', None),
                 ('h264', b'\x00\x00\x01', None)]
        for codec, data, expected in cases:
            with self.subTest(codec=codec, data=data), patch('edgecut.subprocess.run') as run:
                run.return_value = subprocess.CompletedProcess([], 0, data, b'')
                self.assertEqual(edgecut.first_nal_type('source', codec, 0), expected)

    def test_nal_read_timeout_rejects_source(self):
        with patch('edgecut.subprocess.run', side_effect=subprocess.TimeoutExpired('ffmpeg', 3)):
            with self.assertRaisesRegex(edgecut.Unsupported, 'keyframe.*over 3 seconds'):
                edgecut.first_nal_type('source', 'h264', 1, timeout=3)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.video = {'index': 0, 'codec_type': 'video', 'codec_name': 'h264', 'time_base': '1/25'}
        self.info = {'streams': [self.video], 'format': {'duration': '8'}}

    def test_invalid_ranges_rejected_before_packet_probe_or_runner(self):
        for start, end in [(-1, 3), (3, 3), (4, 3), (0, 9), (float('nan'), 3), (0, float('inf'))]:
            with self.subTest(start=start, end=end), patch('edgecut.ffprobe', return_value=self.info) as probe:
                runner = Mock()
                with self.assertRaisesRegex(edgecut.Unsupported, 'range is outside'):
                    edgecut.cut('source', start, end, '/unused', runner)
                runner.assert_not_called()
                self.assertEqual(probe.call_count, 1)

    def test_no_video_and_rotated_video(self):
        for streams, message in [([], 'no video'),
                                 ([dict(self.video, disposition={'attached_pic': 1})], 'no video'),
                                 ([dict(self.video, side_data_list=[{'rotation': 90}])], 'rotated video')]:
            with self.subTest(streams=streams), patch('edgecut.ffprobe', return_value=dict(self.info, streams=streams)):
                with self.assertRaisesRegex(edgecut.Unsupported, message):
                    edgecut.plan('source', 0, 3)

    def test_missing_timestamps_and_empty_range(self):
        for packets, message in [([{'flags': 'K'}], 'without timestamps'), ([], 'no frames in range')]:
            with self.subTest(packets=packets), patch('edgecut.ffprobe', side_effect=[self.info, {'packets': packets}]):
                with self.assertRaisesRegex(edgecut.Unsupported, message):
                    edgecut.plan('source', 0, 3)

    def test_keyframe_flag_does_not_make_a_non_idr_frame_safe(self):
        packets = [{'pts': i, 'flags': 'K' if i % 25 == 0 else ''} for i in range(100)]
        with patch('edgecut.ffprobe', side_effect=[self.info, {'packets': packets}]), \
                patch('edgecut.first_nal_type', return_value=1):
            with self.assertRaisesRegex(edgecut.Unsupported, 'fewer than two IDR'):
                edgecut.plan('source', 0, 3)

    def test_open_gop_packets_crossing_idr_boundary_are_rejected(self):
        packets = [{'pts': 0, 'flags': 'K'}, {'pts': 25, 'flags': 'K'},
                   {'pts': 24, 'flags': ''}, {'pts': 50, 'flags': 'K'}]
        with patch('edgecut.ffprobe', side_effect=[self.info, {'packets': packets}]), \
                patch('edgecut.first_nal_type', return_value=5):
            with self.assertRaisesRegex(edgecut.Unsupported, 'open GOP'):
                edgecut.plan('source', 1, 2)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.plan = {'head_frames': 5, 'body_frames': 40, 'tail_frames': 5,
                     'start': 0, 'end': 2, 'first_frame': 0, 'k1': Fraction(1, 5), 'k2': Fraction(9, 5)}

    def test_corrupt_frame_count_and_duration_fail_before_decode(self):
        for streams, duration, message in [([], '2', 'video frames'),
                ([{'codec_type': 'video', 'nb_read_packets': '49'}], '2', 'video frames'),
                ([{'codec_type': 'video', 'nb_read_packets': '50'}], '4', 'output lasts'),
                ([{'codec_type': 'video', 'nb_read_packets': '50'}], 'nan', 'output lasts')]:
            runner = Mock()
            with self.subTest(streams=streams, duration=duration), patch('edgecut.ffprobe', return_value={
                    'streams': streams, 'format': {'duration': duration}}):
                with self.assertRaisesRegex(edgecut.CutFailed, message):
                    edgecut.verify(self.plan, Path('clip.mp4'), runner)
                runner.assert_not_called()

    def test_decode_stderr_is_failure_even_with_success_exit(self):
        info = {'streams': [{'codec_type': 'video', 'nb_read_packets': '50'}], 'format': {'duration': '2'}}
        for result in [(0, 'corrupt frame'), (1, ''), (0, '  \n')]:
            with self.subTest(result=result), patch('edgecut.ffprobe', return_value=info):
                runner = Mock(return_value=result)
                if result == (0, '  \n'):
                    edgecut.verify(self.plan, Path('clip.mp4'), runner)
                else:
                    with self.assertRaisesRegex(edgecut.CutFailed, 'decode errors near a join'):
                        edgecut.verify(self.plan, Path('clip.mp4'), runner)

    def test_empty_copied_body_is_failure(self):
        with patch('edgecut.ffprobe', return_value={'packets': []}):
            with self.assertRaisesRegex(edgecut.CutFailed, 'copied body is empty'):
                edgecut.body_timing(Path('/unused'))


class CommandLineTests(unittest.TestCase):
    def test_failures_preserve_existing_output_and_clean_work_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'existing.mp4'
            output.write_bytes(b'existing file')
            for error, status in [(edgecut.Unsupported('short clip'), 2),
                                  (edgecut.CutFailed('encode failed'), 1), (OSError('missing ffmpeg'), 1)]:
                workdirs = []
                def fail(source, start, end, work, *callbacks):
                    workdirs.append(Path(work))
                    (Path(work) / 'partial.ts').write_bytes(b'partial')
                    raise error
                with self.subTest(error=error), patch('edgecut.cut', side_effect=fail), redirect_stderr(io.StringIO()):
                    self.assertEqual(edgecut.main(['source.mp4', '0', '3', str(output)]), status)
                self.assertEqual(output.read_bytes(), b'existing file')
                self.assertTrue(workdirs)
                self.assertTrue(all(not path.exists() for path in workdirs))

    def test_invalid_arguments_do_not_start_cut(self):
        with patch('edgecut.cut') as cut:
            for args in [[], ['source', 'bad', '2', 'out'], ['source', '0', '2', 'out', '--unknown']]:
                with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    edgecut.main(args)
                self.assertEqual(raised.exception.code, 2)
            cut.assert_not_called()

    def test_version_and_help_do_not_require_ffmpeg(self):
        with patch('edgecut.cut') as cut:
            for flag, expected in [('--version', f'edgecut {edgecut.__version__}'), ('--help', 'source start end output')]:
                out = io.StringIO()
                with self.subTest(flag=flag), redirect_stdout(out), self.assertRaises(SystemExit) as raised:
                    edgecut.main([flag])
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(expected, out.getvalue())
            cut.assert_not_called()


if __name__ == '__main__':
    unittest.main()
