"""Input validation and failure contracts that do not require FFmpeg."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
import io
import os
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
                self.assertEqual(edgecut.parse_time(text), Fraction(str(expected)))

    def test_decimal_times_remain_exact_through_cli_parsing(self):
        self.assertEqual(edgecut.parse_time('0.4'), Fraction(2, 5))
        self.assertEqual(edgecut.parse_time('0:00.4'), Fraction(2, 5))
        self.assertEqual(edgecut.parse_time('5.4'), Fraction(27, 5))

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

    def test_benign_muxer_warning_does_not_fail(self):
        info = {'streams': [{'codec_type': 'video', 'nb_read_packets': '50'}], 'format': {'duration': '2'},
                'packets': [{'dts_time': '0.0'}, {'dts_time': '0.04'}]}
        runner = Mock(return_value=(0, 'Application provided invalid, non monotonically increasing dts to muxer in stream 0: 12 >= 12'))
        with patch('edgecut.ffprobe', return_value=info):
            edgecut.verify(self.plan, Path('clip.mp4'), runner)

    def test_strict_mode_rejects_benign_muxer_warning(self):
        info = {'streams': [{'codec_type': 'video', 'nb_read_packets': '50'}], 'format': {'duration': '2'},
                'packets': [{'dts_time': '0.0'}, {'dts_time': '0.04'}]}
        runner = Mock(return_value=(0, 'Application provided invalid, non monotonically increasing dts to muxer in stream 0: 12 >= 12'))
        with patch('edgecut.ffprobe', return_value=info):
            with self.assertRaisesRegex(edgecut.CutFailed, 'decode errors near a join'):
                edgecut.verify(self.plan, Path('clip.mp4'), runner, strict=True)

    def test_strict_mode_rejects_equal_dts(self):
        info = {'streams': [{'codec_type': 'video', 'nb_read_packets': '50'}], 'format': {'duration': '2'},
                'packets': [{'dts_time': '0.04'}, {'dts_time': '0.04'}]}
        runner = Mock()
        with patch('edgecut.ffprobe', return_value=info):
            with self.assertRaisesRegex(edgecut.CutFailed, 'not strictly increasing'):
                edgecut.verify(self.plan, Path('clip.mp4'), runner, strict=True)
        runner.assert_not_called()

    def test_backwards_packet_timestamps_fail_before_decode(self):
        info = {'streams': [{'codec_type': 'video', 'nb_read_packets': '50'}], 'format': {'duration': '2'},
                'packets': [{'dts_time': '0.08'}, {'dts_time': '0.04'}]}
        runner = Mock()
        with patch('edgecut.ffprobe', return_value=info):
            with self.assertRaisesRegex(edgecut.CutFailed, 'timestamps go backwards'):
                edgecut.verify(self.plan, Path('clip.mp4'), runner)
        runner.assert_not_called()

    def test_empty_copied_body_is_failure(self):
        with patch('edgecut.ffprobe', return_value={'packets': []}):
            with self.assertRaisesRegex(edgecut.CutFailed, 'copied body is empty'):
                edgecut.body_timing(Path('/unused'))


class MuxTests(unittest.TestCase):
    def test_chapters_are_dropped_explicitly(self):
        plan = {'codec': 'h264', 'audio': None, 'head_seek': Fraction(0), 'end': Fraction(2),
                'start': Fraction(0), 'first_frame': Fraction(0), 'video': {'index': 0}}
        command = edgecut.mux_command('source.mp4', plan, Path('/work'), ['body'])
        self.assertIn('-map_chapters', command)
        self.assertEqual(command[command.index('-map_chapters') + 1], '-1')


class FallbackTests(unittest.TestCase):
    def test_hevc_hvc1_decode_failure_retries_with_hev1(self):
        plan = {'codec': 'hevc', 'head_frames': 1, 'body_frames': 1, 'tail_frames': 1,
                'first_frame': Fraction(0), 'k1': Fraction(0), 'k2': Fraction(1)}
        run = Mock(return_value=(0, ''))
        run.parallel_safe = False
        mux = Mock(side_effect=lambda source, p, work, parts, tag=None: ['mux', tag or 'hvc1'])
        verify = Mock(side_effect=[edgecut.CutFailed('decode errors near a join: bad RPS'), None])
        with patch.multiple(edgecut,
                            plan=Mock(return_value=plan),
                            body_command=Mock(return_value=['body']),
                            body_timing=Mock(return_value=(0, 0)),
                            edge_delays=Mock(return_value=(0, 0)),
                            edge_command=Mock(return_value=['edge']),
                            mux_command=mux,
                            verify=verify):
            edgecut.cut('source.mp4', 0, 1, Path('/work'), run=run)
        self.assertEqual(mux.call_count, 2)
        self.assertIsNone(mux.call_args_list[0].kwargs.get('tag'))
        self.assertEqual(mux.call_args_list[1].kwargs['tag'], 'hev1')
        self.assertEqual(verify.call_count, 2)

    def test_h264_decode_failure_is_not_retried(self):
        plan = {'codec': 'h264', 'head_frames': 1, 'body_frames': 1, 'tail_frames': 1,
                'first_frame': Fraction(0), 'k1': Fraction(0), 'k2': Fraction(1)}
        run = Mock(return_value=(0, ''))
        run.parallel_safe = False
        mux = Mock(return_value=['mux'])
        with patch.multiple(edgecut,
                            plan=Mock(return_value=plan),
                            body_command=Mock(return_value=['body']),
                            body_timing=Mock(return_value=(0, 0)),
                            edge_delays=Mock(return_value=(0, 0)),
                            edge_command=Mock(return_value=['edge']),
                            mux_command=mux,
                            verify=Mock(side_effect=edgecut.CutFailed('decode errors near a join'))):
            with self.assertRaisesRegex(edgecut.CutFailed, 'decode errors'):
                edgecut.cut('source.mp4', 0, 1, Path('/work'), run=run)
        self.assertEqual(mux.call_count, 1)


class CommandLineTests(unittest.TestCase):
    def test_source_cannot_be_overwritten_via_same_path_or_alias(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.mp4'
            source.write_bytes(b'original source')
            symlink, hardlink = Path(folder) / 'symbolic.mp4', Path(folder) / 'hard.mp4'
            symlink.symlink_to(source)
            os.link(source, hardlink)
            for output in [source, source.parent / '.' / source.name, symlink, hardlink]:
                with self.subTest(output=output), patch('edgecut.cut') as cut, redirect_stderr(io.StringIO()):
                    self.assertEqual(edgecut.main([str(source), '0', '3', str(output)]), 1)
                    cut.assert_not_called()
                    self.assertEqual(source.read_bytes(), b'original source')

    def test_failures_preserve_existing_output_and_clean_work_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'existing.mp4'
            output.write_bytes(b'existing file')
            for error, status in [(edgecut.Unsupported('short clip'), 2),
                                  (edgecut.CutFailed('encode failed'), 1), (OSError('missing ffmpeg'), 1)]:
                workdirs = []
                def fail(source, start, end, work, *callbacks, **kwargs):
                    workdirs.append(Path(work))
                    (Path(work) / 'partial.ts').write_bytes(b'partial')
                    raise error
                with self.subTest(error=error), patch('edgecut.cut', side_effect=fail), redirect_stderr(io.StringIO()):
                    self.assertEqual(edgecut.main(['source.mp4', '0', '3', str(output)]), status)
                self.assertEqual(output.read_bytes(), b'existing file')
                self.assertTrue(workdirs)
                self.assertTrue(all(not path.exists() for path in workdirs))

    def test_strict_flag_is_forwarded_to_cut(self):
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / 'source.mp4', Path(folder) / 'clip.mp4'
            source.write_bytes(b'source')
            with patch('edgecut.cut', return_value=source) as cut, redirect_stderr(io.StringIO()):
                self.assertEqual(edgecut.main(['--strict', str(source), '0', '3', str(output)]), 0)
            self.assertTrue(cut.call_args.kwargs['strict'])

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
