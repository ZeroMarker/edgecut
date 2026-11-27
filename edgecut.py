"""edgecut: frame-accurate MP4 clips that re-encode only the edges of the cut.

A clip [start, end) of an H.264 or HEVC source is built from three parts on the
source timeline:

    head  [start, K1)  re-encoded; K1 is the first IDR keyframe at or after start
    body  [K1, K2)     stream-copied, packet for packet
    tail  [K2, end)    re-encoded; K2 is the last IDR keyframe at or before end

IDR keyframes close their GOP, so the copied body never references a frame
outside it. Each part is written to MPEG-TS with the source timestamps and
in-band parameter sets, the parts are joined byte-wise, and the result is muxed
to MP4 with the source audio. Only the clip's own packets are probed, so the
cost does not grow with the length of the source file. Uses the FFmpeg CLI only.
"""
import argparse
from fractions import Fraction
import json
from math import ceil
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

__version__ = '0.1.1'
ENGINE = 'edgecut'

# codec: (encoder, extra encoder args, MP4 tag). The parts carry in-band
# parameter sets; the widely supported avc1/hvc1 tags are used anyway, as for
# FFmpeg's own MPEG-TS to MP4 remuxes (Safari rejects hev1).
CODECS = {
    'h264': ('libx264', ['-preset', 'veryfast', '-crf', '16', '-bf', '0'], 'avc1'),
    'hevc': ('libx265', ['-preset', 'superfast', '-crf', '18', '-x265-params', 'bframes=0:log-level=error'], 'hvc1'),
}
IDR_TYPES = {'h264': {5}, 'hevc': {19, 20}}
H264_PROFILES = {'Baseline': 'baseline', 'Constrained Baseline': 'baseline', 'Main': 'main', 'High': 'high',
                 'High 10': 'high10', 'High 4:2:2': 'high422', 'High 4:4:4 Predictive': 'high444'}
COPY_AUDIO = {'aac', 'mp3'}
# Warnings that some demuxers/decoders print even for a valid stream. Container
# packet order is checked separately in verify().
BENIGN_DECODE_WARNING = re.compile(
    r'Referenced QT chapter track not found|Application provided invalid, non monotonically increasing dts to muxer')
# Added to every intermediate timestamp so parts starting near zero never go
# negative in MPEG-TS (which cannot store negative timestamps).
TS_OFFSET = 10
EPSILON = Fraction(1, 1000)


class Unsupported(Exception):
    """The source or range does not suit an edgecut; nothing was run."""


class CutFailed(Exception):
    """An edgecut step failed or produced an implausible result."""


def seconds(value):
    return f'{float(value):.6f}'


def exact_time(value):
    """Interpret float inputs as their decimal spelling; preserve rational inputs."""
    return Fraction(str(value)) if isinstance(value, float) else Fraction(value)


def ffprobe(args, timeout=120, error=Unsupported):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', *args, '-of', 'json'], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise error(f'ffprobe took over {timeout} seconds') from None
    if result.returncode:
        raise error('ffprobe failed: ' + result.stderr.decode('utf-8', 'replace').strip()[-300:])
    try:
        return json.loads(result.stdout or b'{}')
    except ValueError:
        raise error('ffprobe returned invalid JSON') from None


def first_nal_type(source, codec, at, timeout=120):
    """Type of the first picture NAL unit of the keyframe at `at` seconds."""
    bsf = 'h264_mp4toannexb' if codec == 'h264' else 'hevc_mp4toannexb'
    try:
        data = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-seek_timestamp', '1',
                               '-ss', seconds(at + EPSILON), '-i', str(source),
                               '-map', '0:v:0', '-c', 'copy', '-copypriorss', '1', '-bsf:v', bsf, '-frames:v', '1',
                               '-f', codec, '-'], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        raise Unsupported(f'reading the keyframe at {float(at)} took over {timeout} seconds') from None
    position = 0
    while (position := data.find(b'\x00\x00\x01', position) + 3) >= 3 and position < len(data):
        header = data[position]
        nal = header & 0x1f if codec == 'h264' else header >> 1 & 0x3f
        if (nal in (1, 5)) if codec == 'h264' else nal < 32:
            return nal
    return None


def plan(source, start, end):
    """Probe the source and decide the three parts, or raise Unsupported."""
    info = ffprobe(['-show_entries', 'stream=index,codec_type,codec_name,profile,pix_fmt,time_base,start_time,duration,color_range,'
                    'color_space,color_transfer,color_primaries:stream_disposition=attached_pic:'
                    'stream_side_data=rotation:format=start_time,duration', str(source)])
    streams = info.get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if video is None:
        raise Unsupported('no video stream')
    codec = video.get('codec_name')
    if codec not in CODECS:
        raise Unsupported(f'unsupported video codec {codec}')
    if any(side.get('rotation') for side in video.get('side_data_list', [])):
        raise Unsupported('rotated video')
    duration = info.get('format', {}).get('duration', 'nan')
    try:
        s, e = exact_time(start), exact_time(end)
        origin = Fraction(info.get('format', {}).get('start_time', '0'))
        limit = Fraction(video.get('start_time', str(origin))) + Fraction(video.get('duration', duration))
    except (ValueError, ZeroDivisionError):
        raise Unsupported(f'range is outside the source duration {duration}') from None
    if not max(0, origin) <= s < e <= limit:
        raise Unsupported(f'range is outside the source duration {duration}')
    base = Fraction(video['time_base'])
    # Read past the end: with B-frames, frames shown before `end` can follow a
    # later-shown frame in decode order.
    seek, preroll = s, Fraction(1)
    while True:
        listed = ffprobe(['-select_streams', str(video['index']), '-read_intervals', f'{seconds(seek)}%{seconds(e + 3)}',
                          '-show_entries', 'packet=pts,flags', str(source)], timeout=600)['packets']
        # Packets in decode order. Only PTS is used: Matroska stores no DTS.
        if any(p.get('pts') is None for p in listed):
            raise Unsupported('packets without timestamps')
        packets = [(Fraction(p['pts']) * base, 'K' in p.get('flags', '')) for p in listed]
        # Unindexed demuxers such as MPEG-TS can seek into a GOP and omit
        # reordered frames. Back up until the range includes an earlier keyframe.
        if not packets or seek <= origin or any(key and pts <= s for pts, key in packets):
            break
        seek = max(origin, s - preroll)
        preroll *= 2
    frames = sorted(pts for pts, _ in packets if s <= pts < e)
    if not frames:
        raise Unsupported('no frames in range')
    keys = [i for i, (pts, key) in enumerate(packets) if key and s <= pts <= e]
    idr = {}

    def is_idr(i):
        if i not in idr:
            idr[i] = first_nal_type(source, codec, packets[i][0]) in IDR_TYPES[codec]
        return idr[i]

    # Most clips use IDR keyframes at both ends. Probe the first and last
    # candidates concurrently so the common case needs one seek round instead
    # of two sequential ones.
    if len(keys) >= 2:
        picks = sorted({keys[0], keys[-1]})
        with ThreadPoolExecutor(max_workers=len(picks)) as pool:
            idr.update(pool.map(lambda i: (i, first_nal_type(source, codec, packets[i][0]) in IDR_TYPES[codec]), picks))

    first = next((i for i in keys if is_idr(i)), None)
    last = next((i for i in reversed(keys) if first is not None and i > first and is_idr(i)), None)
    if first is None or last is None or packets[first][0] >= e:
        raise Unsupported('fewer than two IDR keyframes in the clip')
    k1, k2 = packets[first][0], packets[last][0]
    body = packets[first:last]
    # The body is selected by PTS, so it must be exactly the packets between the keyframes.
    if any(not k1 <= pts < k2 for pts, _ in body) or sum(k1 <= pts < k2 for pts, _ in packets) != len(body):
        raise Unsupported('open GOP around the keyframes')
    return {
        'codec': codec, 'video': video, 'audio': audio,
        'first_frame': frames[0], 'k1': k1, 'k2': k2,
        'head_frames': sum(1 for pts in frames if pts < k1),
        'body_frames': len(body),
        'tail_frames': sum(1 for pts in frames if pts >= k2),
        'start': s, 'end': e,
        'head_seek': seek,
        'tail_seek': max(origin, max((pts for pts, key in packets if key and pts < k2), default=origin) - EPSILON),
    }


def encoder_args(p):
    video, codec = p['video'], p['codec']
    encoder, extra, _ = CODECS[codec]
    args = ['-c:v', encoder, *extra, '-pix_fmt', video['pix_fmt']]
    if codec == 'h264' and video.get('profile') in H264_PROFILES:
        args += ['-profile:v', H264_PROFILES[video['profile']]]
    for option, key in (('-color_range', 'color_range'), ('-colorspace', 'color_space'),
                        ('-color_trc', 'color_transfer'), ('-color_primaries', 'color_primaries')):
        if video.get(key) not in (None, 'unknown', 'reserved'):
            args += [option, video[key]]
    return args


def ts_output(path, delay=0):
    args = ['-fps_mode', 'passthrough', '-enc_time_base:v', '-1'] if delay is not None else []
    if delay:
        args += ['-bsf:v', f'setts=dts=DTS-{float(delay)!r}/TB']
    return [*args, '-output_ts_offset', str(TS_OFFSET), '-mpegts_copyts', '1', '-muxdelay', '0',
            '-muxpreload', '0', '-f', 'mpegts', str(path)]


def between(low, high, base):
    """noise bitstream filter expression dropping packets outside [low, high)."""
    # Comparing integer ticks avoids float rounding at exact packet boundaries.
    low_tick, high_tick = ceil(exact_time(low) / Fraction(base)), ceil(exact_time(high) / Fraction(base))
    return f'noise=drop=lt(pts\\,{low_tick})+gte(pts\\,{high_tick})'


# Seeking only positions the demuxer at some earlier keyframe (Matroska seeks to
# a cue), so every step selects its frames or packets by source PTS. Input
# seeks use absolute timestamps, including for files whose start time is nonzero.

def body_command(source, p, work):
    return ['ffmpeg', '-nostdin', '-y', '-copyts', '-seek_timestamp', '1',
            '-ss', seconds(p['k1']), '-to', seconds(p['k2'] + 1),
            '-i', str(source), '-map', f"0:{p['video']['index']}", '-c', 'copy', '-copypriorss', '1',
            '-bsf:v', between(p['k1'], p['k2'], p['video']['time_base']), *ts_output(work / 'body.ts', None)]


def edge_command(source, p, work, name, first, frames, delay):
    """Re-encode `frames` frames starting at the frame shown at `first`."""
    first_tick = ceil(first / Fraction(p['video']['time_base']))
    return ['ffmpeg', '-nostdin', '-y', '-copyts', '-noaccurate_seek', '-seek_timestamp', '1',
            '-ss', seconds(p[f'{name}_seek']),
            '-i', str(source), '-map', f"0:{p['video']['index']}",
            '-vf', f'select=gte(pts\\,{first_tick})', '-frames:v', str(frames), *encoder_args(p),
            *ts_output(work / f'{name}.ts', delay)]


def body_timing(work):
    """(first PTS - first DTS, last DTS) of the copied body, in source seconds."""
    packets = ffprobe(['-show_entries', 'packet=pts_time,dts_time', str(work / 'body.ts')], error=CutFailed)['packets']
    if not packets:
        raise CutFailed('the copied body is empty')
    first, last = packets[0], packets[-1]
    return (Fraction(first['pts_time']) - Fraction(first['dts_time']),
            Fraction(last['dts_time']) - TS_OFFSET)


def edge_delays(p, timing):
    """DTS shifts for the re-encoded head and tail.

    They have no B-frames, so their DTS equals PTS. Shifting the head by the
    body's reorder delay ends it before the body's first DTS; the tail must
    start after the body's last DTS.
    """
    delay, body_last_dts = timing
    tail = delay if p['k2'] - delay > body_last_dts else (p['k2'] - body_last_dts) / 2
    return delay, tail


def mux_command(source, p, work, parts, tag=None):
    command = ['ffmpeg', '-nostdin', '-y', '-copyts', '-itsoffset', str(-TS_OFFSET),
               '-i', 'concat:' + '|'.join(str(work / f'{part}.ts') for part in parts)]
    maps = ['-map', '0:v']
    if p['audio']:
        command += ['-noaccurate_seek', '-seek_timestamp', '1', '-ss', seconds(p['head_seek']),
                    '-to', seconds(p['end'] + 1), '-i', str(source)]
        maps += ['-map', f"1:{p['audio']['index']}"]
        if p['audio']['codec_name'] in COPY_AUDIO:
            maps += ['-c:a', 'copy', '-copypriorss:a', '1', '-bsf:a',
                     between(p['start'], p['end'], p['audio']['time_base'])]
        else:
            maps += ['-af', f"atrim=start={float(p['start'])!r}:end={float(p['end'])!r}", '-c:a', 'aac', '-b:a', '192k']
    return [*command, *maps, '-c:v', 'copy', '-tag:v', tag or CODECS[p['codec']][2], '-output_ts_offset',
            seconds(-p['first_frame']), '-map_metadata', '-1', '-map_chapters', '-1',
            '-movflags', '+faststart', str(work / 'clip.mp4')]


def verify(p, output, run, strict=False):
    """Check frame count, duration, packet order, and decode around each join.

    In strict mode any decoder stderr fails and DTS must strictly increase;
    otherwise known-benign muxer/demuxer warnings are ignored and equal DTS
    values are tolerated.
    """
    info = ffprobe(['-select_streams', 'v:0', '-count_packets',
                    '-show_entries', 'stream=codec_type,nb_read_packets:format=duration:packet=dts_time',
                    str(output)], error=CutFailed)
    count = next((int(s['nb_read_packets']) for s in info.get('streams', []) if s.get('codec_type') == 'video'), 0)
    expected = p['head_frames'] + p['body_frames'] + p['tail_frames']
    if count != expected:
        raise CutFailed(f'output has {count} video frames instead of {expected}')
    duration = float(info.get('format', {}).get('duration', 'nan'))
    if not abs(duration - float(p['end'] - p['start'])) <= 0.5:
        raise CutFailed(f"output lasts {duration} seconds instead of {float(p['end'] - p['start'])}")
    # The join must not reorder or rewind container timestamps. Checking the
    # packets is stronger than trusting the decoder's stderr, which also emits
    # harmless muxer warnings for some sources.
    times = [Fraction(packet['dts_time']) for packet in info.get('packets', [])
             if packet.get('dts_time') not in (None, 'N/A')]
    out_of_order = ((later <= earlier) if strict else (later < earlier) for earlier, later in zip(times, times[1:]))
    if any(out_of_order):
        raise CutFailed('output packet timestamps are not strictly increasing' if strict
                        else 'output packet timestamps go backwards')
    joins = [0] + [float(k - p['first_frame']) for k, n in ((p['k1'], p['head_frames']), (p['k2'], p['tail_frames'])) if n]
    command = ['ffmpeg', '-nostdin', '-v', 'error']
    for join in joins:
        command += ['-ss', seconds(max(0, join - 1)), '-t', '2', '-i', str(output)]
    for index in range(len(joins)):
        command += ['-map', f'{index}:v', '-f', 'null', '-']
    returncode, text = run(command, ENGINE)
    if not strict:
        text = '\n'.join(line for line in text.splitlines() if line.strip() and not BENIGN_DECODE_WARNING.search(line))
    if returncode or text.strip():
        raise CutFailed('decode errors near a join: ' + text.strip()[-300:])


def run_quietly(command, _engine=ENGINE):
    """Default runner: run one command and return (returncode, stderr text)."""
    result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace')
    return result.returncode, result.stderr


def cut(source, start, end, work, run=run_quietly, event=lambda status, **details: None, strict=False):
    """Write the clip [start, end) seconds of `source` to work/clip.mp4 and return its path.

    `run(argv, engine)` runs and logs one command and returns (returncode,
    output); `event(status, **details)` logs a step. Raises Unsupported before
    running anything, or CutFailed afterwards. Head and tail re-encodes run in
    parallel when `run` is known to be safe for concurrent use (the default
    runner, or a runner marked `parallel_safe = True`). `strict` makes
    verification reject any decoder stderr and equal DTS values.
    """
    source, work = Path(source), Path(work)
    p = plan(source, start, end)
    event('plan', engine=ENGINE, codec=p['codec'], k1=float(p['k1']), k2=float(p['k2']),
          head_frames=p['head_frames'], body_frames=p['body_frames'], tail_frames=p['tail_frames'])
    def step(name, command):
        returncode, text = run(command, ENGINE)
        if returncode:
            raise CutFailed(f'{name} step exited with {returncode}: ' + text.strip()[-300:])

    step('body', body_command(source, p, work))
    head_delay, tail_delay = edge_delays(p, body_timing(work))
    parts = ['body']
    edges = []
    if p['head_frames']:
        edges.append(('head', edge_command(source, p, work, 'head', p['first_frame'], p['head_frames'], head_delay)))
        parts.insert(0, 'head')
    if p['tail_frames']:
        edges.append(('tail', edge_command(source, p, work, 'tail', p['k2'], p['tail_frames'], tail_delay)))
        parts.append('tail')
    if len(edges) == 2 and getattr(run, 'parallel_safe', run is run_quietly):
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [(name, pool.submit(run, command, ENGINE)) for name, command in edges]
            results = [(name, future.result()) for name, future in futures]
        for name, (returncode, text) in results:
            if returncode:
                raise CutFailed(f'{name} step exited with {returncode}: ' + text.strip()[-300:])
    else:
        for name, command in edges:
            step(name, command)
    step('mux', mux_command(source, p, work, parts))
    output = work / 'clip.mp4'
    try:
        verify(p, output, run, strict=strict)
    except CutFailed as exc:
        # hvc1 keeps parameter sets only in hvcC. Head, body and tail are
        # encoded separately, so their VPS/SPS/PPS can differ; in that case the
        # concatenated stream decodes with the wrong RPS. hev1 allows parameter
        # sets in-band, so retry the mux once before giving up.
        if p['codec'] != 'hevc' or 'decode errors near a join' not in str(exc):
            raise
        event('fallback', codec='hevc', tag='hev1')
        step('mux', mux_command(source, p, work, parts, tag='hev1'))
        verify(p, output, run, strict=strict)
    return output


def cut_many(source, ranges, work, run=run_quietly, event=lambda status, **details: None, jobs=4, strict=False):
    """Cut several ranges at once, returning their paths in input order.

    Each clip gets its own directory under `work` (``cut-000/clip.mp4``, ...).
    Ranges are cut concurrently when `run` is safe for concurrent use; head and
    tail are kept sequential inside each cut to avoid oversubscribing the CPU.
    If one range fails, the others may already have been written.
    """
    source, work = Path(source), Path(work)
    ranges = [(start, end) for start, end in ranges]
    if not ranges:
        return []
    work.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(int(jobs), len(ranges)))

    def one(index, start, end):
        folder = work / f'cut-{index:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        def edge_run(command, engine=ENGINE):
            return run(command, engine)
        edge_run.parallel_safe = False
        def cut_event(status, **details):
            event(status, cut=index, **details)
        return cut(source, start, end, folder, run=edge_run, event=cut_event, strict=strict)

    if jobs == 1 or not getattr(run, 'parallel_safe', run is run_quietly):
        return [one(index, start, end) for index, (start, end) in enumerate(ranges)]
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(lambda item: one(item[0], *item[1]), enumerate(ranges)))


def parse_time(value):
    """Seconds from '83.5', '1:23.5' or '0:01:23.5'."""
    if not re.fullmatch(r'(\d+:){0,2}\d+(\.\d+)?', value):
        raise argparse.ArgumentTypeError(f'invalid time {value!r}; use seconds, MM:SS or HH:MM:SS')
    *rest, last = value.split(':')
    if any(int(part) >= 60 for part in rest[1:]) or (rest and Fraction(last) >= 60):
        raise argparse.ArgumentTypeError(f'invalid time {value!r}')
    total = 0
    for part in rest:
        total = total * 60 + int(part)
    return total * 60 + Fraction(last)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='edgecut', description='Cut a frame-accurate MP4 clip from an H.264 or HEVC '
                                     'video, copying the video between keyframes and re-encoding only the edges.')
    parser.add_argument('source', type=Path)
    parser.add_argument('start', type=parse_time, help='seconds, MM:SS or HH:MM:SS (inclusive)')
    parser.add_argument('end', type=parse_time, help='seconds, MM:SS or HH:MM:SS (exclusive)')
    parser.add_argument('output', type=Path, help='MP4 file to write')
    parser.add_argument('-v', '--verbose', action='store_true', help='print the plan and every FFmpeg command')
    parser.add_argument('--strict', action='store_true',
                        help='fail on any decoder stderr and require strictly increasing DTS')
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    args = parser.parse_args(argv)

    def run(command, engine=ENGINE):
        if args.verbose:
            print('+', shlex.join(command), file=sys.stderr)
        return run_quietly(command, engine)
    # Verbose mode prints commands as they start, so keep the head/tail order
    # deterministic there; normal runs can encode both edges at once.
    run.parallel_safe = not args.verbose

    def event(status, **details):
        if args.verbose:
            print(status, json.dumps(details), file=sys.stderr)

    try:
        if (args.source.resolve() == args.output.resolve()
                or (args.source.exists() and args.output.exists() and args.source.samefile(args.output))):
            raise CutFailed('source and output must be different files')
        with tempfile.TemporaryDirectory(prefix='edgecut-') as work:
            shutil.copyfile(cut(args.source, args.start, args.end, work, run, event, strict=args.strict), args.output)
    except Unsupported as exc:
        print(f'edgecut: not supported: {exc} (use a full re-encode instead)', file=sys.stderr)
        return 2
    except (CutFailed, OSError) as exc:
        print(f'edgecut: failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
