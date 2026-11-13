# edgecut

Frame-accurate clips from H.264 and HEVC videos in seconds. edgecut copies the original video between keyframes and re-encodes only the few frames at each edge of the cut, so almost every frame of the clip is bit-identical to the source.

It is a single Python file with no dependencies beyond the standard library. All video work is done by the `ffmpeg` and `ffprobe` command-line tools.

```sh
edgecut movie.mkv 1:02:03.5 1:04:10 clip.mp4
```

## Install

Requirements: Python 3.9+, and FFmpeg with libx264 (and libx265 for HEVC sources).

```sh
uv tool install edgecut
# or
pipx install edgecut
```

Or copy `edgecut.py` into your project.

## Usage

```
edgecut [-v] SOURCE START END OUTPUT
```

- `START` and `END` are seconds, `MM:SS` or `HH:MM:SS` (fractions allowed). The clip holds every frame shown in [START, END) on the source timeline.
- `OUTPUT` is an MP4 file with the video tagged `avc1`/`hvc1`, `+faststart`, and timestamps starting at 0.
- `-v` prints the plan and every FFmpeg command.
- Exit status: `0` success, `2` source or range not supported (nothing was written; use a full re-encode), `1` a step or the output check failed.

From Python:

```python
import tempfile, edgecut

with tempfile.TemporaryDirectory() as work:
    try:
        clip = edgecut.cut('movie.mp4', 3723.5, 3850.0, work)  # returns work/clip.mp4
    except edgecut.Unsupported:   # raised before anything runs
        ...                       # fall back to a full re-encode
    except edgecut.CutFailed:     # a step or the output check failed
        ...
```

`cut()` takes optional `run(argv, engine) -> (returncode, output)` and `event(status, **details)` callbacks, so an application can run the FFmpeg commands under its own logging and timeouts.

## How it works

```
source   ──K0──────s──K1════════════════════K2──e────K3──
clip               [head)[      body       )[tail)
                   re-     stream-copied     re-
                   encoded                   encoded
```

1. **Plan** with ffprobe, without decoding. List the video packets from the keyframe before `start` to 3 s past `end` (with B-frames, frames shown just before `end` can come later in decode order). K1 is the first IDR keyframe at or after `start`, K2 the last at or before `end`; the NAL unit type of each candidate is checked (H.264 5, HEVC 19/20). The clip is rejected if it has fewer than two IDR keyframes or any packet between them is shown outside [K1, K2) (open GOP).
2. **Body.** Stream-copy [K1, K2) to MPEG-TS with the source timestamps and in-band parameter sets.
3. **Head and tail.** Re-encode [start, K1) and [K2, end) with libx264 (`veryfast`, CRF 16) or libx265 (`superfast`, CRF 18), without B-frames, matching the source pixel format, H.264 profile, colour properties and time base. Their DTS is shifted by the body's reorder delay, read from the written body because Matroska stores no DTS, so DTS keeps increasing across both joins.
4. **Mux.** Concatenate the TS parts, add the source audio (AAC/MP3 copied, others encoded to AAC), shift the first frame to 0, and write MP4.
5. **Check.** The frame count must equal the plan, the duration must be within 0.5 s of the clip, and FFmpeg must decode 2 s around the start and each join without errors.

Seeking only positions the demuxer at some earlier keyframe: Matroska seeks to a cue, and with `-copyts` FFmpeg's own `-ss` trimming is offset by the container start time. So every step selects frames and packets by source PTS: the `noise` bitstream filter's `drop` expression for copied packets, and `select`/`atrim` for decoded frames.

Only the clip's own packets are read, so the time does not grow with the length of the source. It is dominated by encoding the edges, so it follows the source's keyframe interval.

## Results

On real 1080p–1440p sources (MP4 and MKV, clips of 28–140 s), compared with a frame-accurate decode of the same range:

- 3.4–16.5 s per clip (median 5.8 s) versus about 145 s for a full libx264 re-encode of the same clip.
- Same frame count as the reference; all copied frames bit-identical; re-encoded edge frames at 47–69 dB PSNR.
- Audio packets identical to the source; video and audio start at 0; timestamps within 0.011 ms of the source.
- Plays in Chromium without media errors, including seeks across the joins.

## Limitations

- Video codecs: H.264 and HEVC with IDR keyframes. Open-GOP sources, other codecs, rotated video, and clips shorter than about two keyframe intervals raise `Unsupported`.
- Only the first video stream and the first audio stream are kept; subtitles, chapters and metadata are dropped.
- Not yet verified: HEVC output in browsers, and Safari/iOS playback across the joins, where parameter sets change in-band under the `avc1`/`hvc1` tags.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests generate short videos with FFmpeg and compare every decoded frame of each clip with the source range: H.264 with B-frames, HEVC, 10-bit, Matroska, clips starting at 0, and start/end exactly on keyframes. They also check rejection of unsupported sources and errors from failed steps.

## License

MIT

Release maintainers: see [RELEASING.md](RELEASING.md) for packaging checks and the
PyPI Trusted Publishing workflow.
