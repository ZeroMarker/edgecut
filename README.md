# edgecut

Frame-accurate clips from H.264 and HEVC videos in seconds. edgecut copies the original video between keyframes and re-encodes only the few frames at each edge of the cut, so almost every frame of the clip is bit-identical to the source.

It is a single Python file with no dependencies beyond the standard library. All video work is done by the `ffmpeg` and `ffprobe` command-line tools.

Inspired by [skeskinen/smartcut](https://github.com/skeskinen/smartcut) and its approach of cutting video with minimal re-encoding around the cut points.

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

For a reproducible installation, use `uv tool install edgecut==0.2.0`.
See the [changelog](CHANGELOG.md) for what changed in this release.

## Usage

```
edgecut [-v] [--strict] SOURCE START END OUTPUT
```

- `START` and `END` are seconds, `MM:SS` or `HH:MM:SS` (fractions allowed). The clip holds every frame shown in [START, END) on the source timeline.
- `OUTPUT` is an MP4 file with the video tagged `avc1` (or `hvc1`/`hev1` for HEVC), `+faststart`, and timestamps starting at 0.
- The CLI rejects an output that refers to the source file, including symbolic and hard links.
- `-v` prints the plan and every FFmpeg command.
- `--strict` rejects any decoder diagnostics and requires a decode timestamp (DTS) on every video packet. DTS must strictly increase in packet order. Default verification rejects backwards DTS, allows equal DTS, and tolerates known-benign warnings.
- Exit status: `0` success, `2` source or range not supported (nothing was written; use a full re-encode), `1` a step or the output check failed.

### Python API

```python
from pathlib import Path

import edgecut

work = Path("single-cut")
work.mkdir(exist_ok=True)
clip = edgecut.cut("movie.mp4", 0.37, 2.37, work, strict=True)
print(clip)  # single-cut/clip.mp4
```

`cut()` expects an existing work directory. It raises `Unsupported` if it cannot
plan the cut and `CutFailed` if a destination aliases the source or a processing
or verification step fails.

Use `cut_many()` to cut several ranges from the same source. It creates the work
directory and returns the paths in input order.

```python
import edgecut

paths = edgecut.cut_many("movie.mp4", [(0.37, 2.37), (3.37, 5.37)], "batch", jobs=2)
# batch/cut-000/clip.mp4, batch/cut-001/clip.mp4
```

With the default runner, `jobs` limits how many ranges run concurrently and
defaults to 4. Each clip's head and tail are encoded sequentially in a batch.
If a range fails, completed clips may still be on disk.

Both APIs reserve `body.ts`, `head.ts`, `tail.ts`, and `clip.mp4` in each clip's
work directory. Use a dedicated directory because existing files at those paths
may be overwritten. The APIs reject paths that resolve to the source file,
including symbolic and hard links. Batch cuts check all destination paths before
starting. Pass `strict=True` for the same verification as `--strict`.

Both APIs accept optional `run(argv, engine) -> (returncode, output)` and
`event(status, **details)` callbacks. Set `run.parallel_safe = True` on a custom
runner that supports concurrent calls. Batch events include a zero-based `cut`
index and may arrive concurrently.

Times use the source's absolute presentation timestamps. For sources with a nonzero
start time (common in MPEG-TS), include that offset in `START` and `END`; inspect it
with `ffprobe -v error -show_entries format=start_time -of default=nw=1 SOURCE`.
CLI decimal times are parsed exactly. Python callers can pass `Fraction` values;
float inputs are interpreted using their decimal spelling.

## How it works

```
source   ──K0──────s──K1════════════════════K2──e────K3──
clip               [head)[      body       )[tail)
                   re-     stream-copied     re-
                   encoded                   encoded
```

1. **Plan** with ffprobe, without decoding. List the video packets from the keyframe before `start` to 3 s past `end` (with B-frames, frames shown just before `end` can come later in decode order). K1 is the first IDR keyframe at or after `start`, K2 the last at or before `end`; the NAL unit type of each candidate is checked (H.264 5, HEVC 19/20). The clip is rejected if it has fewer than two IDR keyframes or any packet between them is shown outside [K1, K2) (open GOP).
2. **Body.** Stream-copy [K1, K2) to MPEG-TS with the source timestamps and in-band parameter sets.
3. **Head and tail.** Re-encode [start, K1) and [K2, end) with libx264 (`veryfast`, CRF 16) or libx265 (`superfast`, CRF 18), without B-frames, matching the source pixel format, H.264 profile, colour properties and time base. Standalone cuts encode both edges concurrently with the default runner. CLI verbose mode processes them sequentially. Edge DTS is shifted using the reorder delay measured in the copied body so timestamps stay in decode order across both joins.
4. **Mux.** Concatenate the TS parts, add the source audio (AAC/MP3 copied, others encoded to AAC), shift the first frame to 0, and write MP4 without chapters or metadata. HEVC output is first tagged `hvc1`. If decode verification fails near a join, edgecut remuxes once as `hev1` and verifies again.
5. **Check.** Compare the video packet count and duration with the plan, check video DTS order, and decode a two-second window at the start and around each join. The duration must be within 0.5 s of the requested clip.

Input seeks use absolute timestamps (`-seek_timestamp 1`). Matroska seeks to a cue,
while MPEG-TS can land inside a GOP; the planner backs up until it includes a
keyframe before the range. Every step then selects frames and packets by source
PTS: integer time-base ticks in the `noise` bitstream filter and video `select`
filter, and `atrim` for decoded audio. Packet boundaries follow [start, end)
without a fixed time tolerance.

Only the clip and the preceding GOP needed for seeking are read, so the time does not grow with the length of the source. It is dominated by encoding the edges, so it follows the source's keyframe interval.

## Results

On real 1080p–1440p sources (MP4 and MKV, clips of 28–140 s), compared with a frame-accurate decode of the same range:

- 3.4–16.5 s per clip (median 5.8 s) versus about 145 s for a full libx264 re-encode of the same clip.
- Same frame count as the reference; all copied frames bit-identical; re-encoded edge frames at 47–69 dB PSNR.
- Audio packets identical to the source; video and audio start at 0; timestamps within 0.011 ms of the source.
- Plays in Chromium without media errors, including seeks across the joins.

## Limitations

- Video codecs: H.264 and HEVC with IDR keyframes. Open-GOP sources, other codecs, rotated video, and clips shorter than about two keyframe intervals raise `Unsupported`.
- Only the first video stream and the first audio stream are kept; subtitles, chapters and metadata are dropped.
- HEVC output starts with the `hvc1` tag. If the concatenated parts cannot decode because their parameter sets differ, it is remuxed once as `hev1`; Safari/iOS may reject `hev1`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests generate short videos with FFmpeg and compare every decoded frame of each clip with the source range: H.264 with B-frames, HEVC, 10-bit, Matroska, clips starting at 0, and start/end exactly on keyframes. They also check rejection of unsupported sources and errors from failed steps.

They verify every copied body frame at its original position, presentation timestamps,
increasing DTS across joins, edge-frame quality (PSNR), AAC/MP3 packet hashes and audio
timing, PCM-to-AAC audio, video without audio, fractional frame rates, colour metadata
and MP4 faststart. Failure tests
cover invalid ranges, corrupt output, each failed encoding/muxing stage, CLI exit codes,
preserving existing output on failure and temporary-file cleanup.

Input validation, probing and CLI failure tests also run without FFmpeg:

```sh
python3 -m unittest discover -s tests -p test_contracts.py -v
```

CI runs the suite on Python 3.9 and 3.14 with Ubuntu 24.04. It measures statement
and branch coverage with `coverage.py` and requires at least 95% combined coverage.

## License

MIT

Release maintainers: see [RELEASING.md](RELEASING.md) for packaging checks and the
PyPI Trusted Publishing workflow.
