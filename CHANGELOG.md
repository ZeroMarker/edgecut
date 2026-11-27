# Changelog

## Unreleased

### Added

- `cut_many()` batch cutting in the Python API: multiple ranges are cut
  concurrently into `work/cut-NNN/clip.mp4` (configured with `jobs=`), returned
  in input order.
- `--strict` and `strict=True` for `cut()` / `cut_many()`: reject any decoder
  stderr and require strictly increasing container DTS. The default keeps
  tolerating known-benign muxer/demuxer warnings.
- HEVC mux fallback: try `hvc1`, and if the concatenated parts fail to decode
  because their VPS/SPS/PPS differ, remux once as `hev1` and verify again.

### Changed

- Head and tail edges are encoded concurrently for the default runner (verbose
  mode and custom runners stay sequential).
- The first and last keyframe candidates are probed for NAL type concurrently.
- Verification now checks packet DTS order and ignores only known-benign
  decoder warnings instead of rejecting any stderr.
- Chapters are dropped explicitly with `-map_chapters -1`; this also avoids
  false failures on MP4s with chapter tracks.

### Fixed

- Sources with chapter tracks no longer fail during decode verification.
- HEVC clips whose independently encoded parts use different parameter sets
  now decode after the `hev1` fallback.
