"""Cut generated videos and compare every decoded frame with the source range."""
from array import array
from contextlib import redirect_stderr
import io
import json
from fractions import Fraction
import math
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


def make_video(path, *codec_args, seconds=8, audio_codec='aac', rate='25'):
    """Generate moving video and optional sine audio, with explicit codec settings."""
    audio = ['-f', 'lavfi', '-i', 'sine=f=440:r=48000'] if audio_codec else []
    audio_output = ['-c:a', audio_codec] if audio_codec else ['-an']
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', f'testsrc2=s=160x90:r={rate}',
                    *audio, '-t', str(seconds), *(codec_args or H264), *audio_output, str(path)],
                   check=True, capture_output=True, timeout=30)


def probe_packets(path, stream):
    return json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-select_streams', stream, '-show_packets', '-show_data_hash', 'sha256',
        '-show_entries', 'packet=pts_time,dts_time,data_hash', '-of', 'json', str(path)], timeout=30))['packets']


def frame_hashes(path, start=None, end=None):
    """MD5 of each decoded video frame, optionally only frames shown in [start, end)."""
    select = ['-vf', f'select=gte(t\\,{start})*lt(t\\,{end})'] if start is not None else []
    output = subprocess.check_output(['ffmpeg', '-v', 'error', '-copyts', '-i', str(path), '-map', '0:v', *select,
                                      '-fps_mode', 'passthrough', '-f', 'framemd5', '-'], text=True, timeout=30)
    return [line.rsplit(',', 1)[1].strip() for line in output.splitlines() if not line.startswith('#')]


def gray_frames(path, start=None, end=None):
    filters = ['-vf', f'select=gte(t\\,{start})*lt(t\\,{end})'] if start is not None else []
    return subprocess.check_output(['ffmpeg', '-v', 'error', '-copyts', '-i', str(path), '-map', '0:v:0',
        *filters, '-fps_mode', 'passthrough', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'], timeout=30)


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

    def assert_exact_clip(self, output, source, start, end, copied, audio=True):
        """Same frames as the source range; the copied ones bit-identical."""
        start, end = Fraction(str(start)), Fraction(str(end))
        expected, actual = frame_hashes(source, start, end), frame_hashes(output)
        self.assertEqual(len(actual), len(expected))
        source_pts = sorted(Fraction(p['pts_time']) for p in probe_packets(source, 'v:0')
                            if start <= Fraction(p['pts_time']) < end)
        output_packets = probe_packets(output, 'v:0')
        output_pts = sorted(Fraction(p['pts_time']) for p in output_packets)
        self.assertEqual(len(source_pts), len(output_pts))
        for original, written in zip(source_pts, output_pts):
            self.assertLessEqual(abs(original - source_pts[0] - written), Fraction(1, 1000))
        dts = [Fraction(p['dts_time']) for p in output_packets]
        self.assertTrue(all(a < b for a, b in zip(dts, dts[1:])), 'DTS must strictly increase at joins')
        planned = edgecut.plan(source, start, end)
        body = [(a, b) for pts, a, b in zip(source_pts, expected, actual) if planned['k1'] <= pts < planned['k2']]
        self.assertEqual(len(body), copied)
        self.assertTrue(all(a == b for a, b in body), 'Every copied body frame must match at its exact position')
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries',
                                                   'stream=codec_type,start_time', '-of', 'json', str(output)]))
        self.assertEqual({s['codec_type'] for s in info['streams']}, {'video', 'audio'} if audio else {'video'})
        self.assertTrue(all(abs(float(s['start_time'])) < 0.03 for s in info['streams']))

    def test_copies_between_keyframes(self):
        source = self.directory / 'source 日本語.mp4'
        make_video(source, *H264, '-color_range', 'tv', '-color_primaries', 'bt709',
                   '-color_trc', 'bt709', '-colorspace', 'bt709')
        output, events = self.cut(source, 0.37, 5.62)
        plan = dict(events)['plan']
        self.assertEqual((plan['k1'], plan['k2'], plan['body_frames']), (1.0, 5.0, 100))
        self.assert_exact_clip(output, source, 0.37, 5.62, copied=100)
        def video_properties(path):
            return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=pix_fmt,profile,color_range,color_space,color_transfer,color_primaries',
                '-of', 'json', str(path)], timeout=30))['streams'][0]

        # Some FFprobe builds omit transfer and primaries even for the source.
        self.assertEqual(video_properties(output), video_properties(source))
        original, written = gray_frames(source, 0.37, 5.62), gray_frames(output)
        self.assertEqual(len(original), len(written))
        frame_size = 160 * 90
        frame_count = len(original) // frame_size
        head = plan['head_frames']
        for index in [*range(head), *range(frame_count - plan['tail_frames'], frame_count)]:
            with self.subTest(edge_frame=index):
                low, high = index * frame_size, (index + 1) * frame_size
                mse = sum((a - b) ** 2 for a, b in zip(original[low:high], written[low:high])) / frame_size
                psnr = 10 * math.log10(255 ** 2 / mse) if mse else math.inf
                self.assertGreater(psnr, 30, 'Each re-encoded edge frame must preserve source content')

    def test_matroska_hevc_10_bit_and_ranges_on_keyframes(self):
        cases = [('matroska.mkv', H264, 0.37, 5.62, 100),
                 ('hevc.mp4', ('-c:v', 'libx265', '-x265-params',
                               'keyint=25:min-keyint=25:no-open-gop=1:log-level=error'), 0.37, 5.62, 100),
                 ('10 bit.mp4', (*H264, '-pix_fmt', 'yuv420p10le'), 0.37, 5.62, 100),
                 ('on keyframes.mp4', H264, 1, 3, 50),
                 ('head only.mp4', H264, 0.37, 5, 100),
                 ('to eof.mp4', H264, 0, 8, 175),
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

    def test_custom_runner_failure_stops_at_each_stage(self):
        source = self.directory / 'source.mp4'
        make_video(source)
        stages = ['body', 'head', 'tail', 'clip']
        for index, stage in enumerate(stages):
            with self.subTest(stage=stage):
                work = self.directory / f'fail-{stage}'
                work.mkdir()
                observed = []
                def runner(command, engine):
                    self.assertEqual(engine, 'edgecut')
                    observed.append(Path(command[-1]).stem)
                    return (42, 'injected failure') if observed[-1] == stage else edgecut.run_quietly(command)
                name = 'mux' if stage == 'clip' else stage
                with self.assertRaisesRegex(edgecut.CutFailed, f'{name} step exited with 42: injected failure'):
                    edgecut.cut(source, 0.37, 5.62, work, run=runner)
                self.assertEqual(observed, stages[:index + 1])
                self.assertFalse((work / 'clip.mp4').exists())

    def test_command_line(self):
        source, output = self.directory / 'source.mp4', self.directory / 'clip.mp4'
        make_video(source)
        log = io.StringIO()
        with redirect_stderr(log):
            self.assertEqual(edgecut.main(['-v', str(source), '0:00.37', '5.62', str(output)]), 0)
        self.assertIn('plan ', log.getvalue())
        self.assertIn('+ ffmpeg ', log.getvalue())
        self.assert_exact_clip(output, source, 0.37, 5.62, copied=100)
        with patch('sys.stderr'):
            self.assertEqual(edgecut.main([str(source), '1.2', '1.8', str(self.directory / 'short.mp4')]), 2)
        self.assertFalse((self.directory / 'short.mp4').exists())

    def test_cut_many_parallel_ranges(self):
        source = self.directory / 'source.mp4'
        make_video(source)
        work = self.directory / 'batch'
        ranges = [(0.37, 2.37), (3.37, 5.37)]
        outputs = edgecut.cut_many(source, ranges, work, jobs=2)
        self.assertEqual([p.parent.name for p in outputs], ['cut-000', 'cut-001'])
        for (start, end), output in zip(ranges, outputs):
            planned = edgecut.plan(source, start, end)
            self.assert_exact_clip(output, source, start, end, copied=planned['body_frames'])

    def test_video_without_audio(self):
        source = self.directory / 'silent.mp4'
        make_video(source, audio_codec=None)
        self.assert_exact_clip(self.cut(source, 0.37, 5.62)[0], source, 0.37, 5.62, copied=100, audio=False)

    def test_decimal_frame_boundaries_include_start_and_exclude_end(self):
        source = self.directory / 'boundaries.mp4'
        make_video(source)
        for start, end in [(0.4, 5.4), (Fraction(2, 5), Fraction(27, 5))]:
            with self.subTest(start=start, end=end):
                p = edgecut.plan(source, start, end)
                self.assertEqual(p['first_frame'], Fraction(2, 5))
                self.assertEqual((p['head_frames'], p['body_frames'], p['tail_frames']), (15, 100, 10))
                output = self.cut(source, start, end)[0]
                self.assert_exact_clip(output, source, 0.4, 5.4, copied=100)

    def test_nonzero_ts_timestamps_and_range_near_eof(self):
        original, source = self.directory / 'source.mp4', self.directory / 'source.ts'
        make_video(original)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(original), '-c', 'copy', str(source)],
                       check=True, capture_output=True, timeout=30)
        first = min(Fraction(p['pts_time']) for p in probe_packets(source, 'v:0'))
        # TS wraps AAC in ADTS headers; compare the original MP4 payloads,
        # shifted onto the TS timeline, rather than hashing transport headers.
        source_audio = probe_packets(original, 'a:0')
        self.assertGreater(first, 1)
        for low, high, copied in [(Fraction('0.37'), Fraction('5.62'), 100), (4, 8, 75)]:
            with self.subTest(low=low, high=high):
                start, end = first + low, first + high
                output = self.cut(source, start, end)[0]
                self.assert_exact_clip(output, source, start, end, copied=copied)
                expected_audio = [p for p in source_audio if start <= Fraction(p['pts_time']) + first < end]
                actual_audio = probe_packets(output, 'a:0')
                self.assertEqual([p['data_hash'] for p in actual_audio], [p['data_hash'] for p in expected_audio])

    def test_shifted_mp4_uses_video_end_timestamp_for_range_validation(self):
        original, source = self.directory / 'source.mp4', self.directory / 'shifted.mp4'
        make_video(original)
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(original), '-c', 'copy',
                        '-output_ts_offset', '5', str(source)], check=True, capture_output=True, timeout=30)
        self.assert_exact_clip(self.cut(source, Fraction('5.4'), Fraction('10.4'))[0],
                               source, Fraction('5.4'), Fraction('10.4'), copied=100)
        runner = Mock()
        with self.assertRaisesRegex(edgecut.Unsupported, 'range is outside'):
            edgecut.cut(source, 9, 14, self.directory, run=runner)
        runner.assert_not_called()

    def test_audio_boundaries_within_one_millisecond_of_packet_pts(self):
        source = self.directory / 'audio-boundaries.mp4'
        make_video(source)
        original = probe_packets(source, 'a:0')
        for start, end in [(Fraction('0.3841'), Fraction('5.6321')), (Fraction('0.384'), Fraction('5.632'))]:
            with self.subTest(start=start, end=end):
                output = self.cut(source, start, end)[0]
                expected = [p for p in original if start <= Fraction(p['pts_time']) < end]
                actual = probe_packets(output, 'a:0')
                self.assertEqual([p['data_hash'] for p in actual], [p['data_hash'] for p in expected])

    def test_audio_packets_are_copied_without_reencoding(self):
        for codec, suffix in [('aac', 'mp4'), ('libmp3lame', 'mkv')]:
            with self.subTest(codec=codec):
                source = self.directory / f'{codec}.{suffix}'
                make_video(source, audio_codec=codec)
                output = self.cut(source, 0.37, 5.62)[0]
                original = [p for p in probe_packets(source, 'a:0') if 0.37 <= float(p['pts_time']) < 5.62]
                written = probe_packets(output, 'a:0')
                self.assertGreater(len(written), 100)
                self.assertEqual([p['data_hash'] for p in written], [p['data_hash'] for p in original])
                first_frame = Fraction(10, 25)
                for before, after in zip(original, written):
                    self.assertLessEqual(abs(Fraction(before['pts_time']) - first_frame - Fraction(after['pts_time'])),
                                         Fraction(1, 1000))

    def test_pcm_audio_is_transcoded_to_aac(self):
        source = self.directory / 'pcm.mkv'
        make_video(source, audio_codec='pcm_s16le')
        output = self.cut(source, 0.37, 5.62)[0]
        self.assert_exact_clip(output, source, 0.37, 5.62, copied=100)
        streams = json.loads(subprocess.check_output([
            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)], timeout=30))['streams']
        audio = next(s for s in streams if s['codec_type'] == 'audio')
        self.assertEqual(audio['codec_name'], 'aac')
        self.assertEqual(audio['sample_rate'], '48000')
        decoded = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(output), '-map', '0:a:0',
                                           '-f', 's16le', '-'], timeout=30)
        self.assertGreater(len(decoded), 48000 * 2 * 5)
        samples = array('h', decoded)
        if sys.byteorder != 'little':
            samples.byteswap()
        rms = (sum(sample * sample for sample in samples) / len(samples)) ** 0.5
        self.assertGreater(rms, 2000, 'Transcoded audio must retain the sine signal')
        frequency = sum(a <= 0 < b for a, b in zip(samples, samples[1:])) * 48000 / len(samples)
        self.assertAlmostEqual(frequency, 440, delta=10)

    def test_fractional_frame_rate_and_tail_only_range(self):
        source = self.directory / 'ntsc.mp4'
        make_video(source, '-c:v', 'libx264', '-g', '30', '-bf', '2', rate='30000/1001')
        output = self.cut(source, 0, 5.62)[0]
        self.assert_exact_clip(output, source, 0, 5.62, copied=150)

    def test_output_uses_faststart_and_avc1_tag(self):
        source = self.directory / 'source.mp4'
        make_video(source)
        output = self.cut(source, 0.37, 5.62)[0]
        data = output.read_bytes()
        boxes, at = [], 0
        while at < len(data):
            size = int.from_bytes(data[at:at + 4], 'big')
            boxes.append(data[at + 4:at + 8])
            if size == 1:
                size = int.from_bytes(data[at + 8:at + 16], 'big')
            self.assertGreaterEqual(size, 8)
            at += size
        self.assertEqual(at, len(data))
        self.assertLess(boxes.index(b'moov'), boxes.index(b'mdat'))
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_tag_string', '-of', 'json', str(output)], timeout=30))
        self.assertEqual(info['streams'][0]['codec_tag_string'], 'avc1')


if __name__ == '__main__':
    unittest.main()
