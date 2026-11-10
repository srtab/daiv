"""Cut the waits out of a recording and export it as a GIF and an MP4.

uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/hero.json ~/.cache/daiv-demo
"""

import argparse
import json
import subprocess
from pathlib import Path


def duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(out.stdout.strip())


def keep_segments(skips: list[dict], total: float) -> list[tuple[float, float]]:
    keep, cursor = [], 0.0
    for skip in sorted(skips, key=lambda s: s["start"]):
        if skip["start"] > cursor:
            keep.append((cursor, skip["start"]))
        cursor = max(cursor, skip["end"])
    if total > cursor:
        keep.append((cursor, total))
    return keep


def concat_filter(segments: list[tuple[float, float]]) -> str:
    parts = [
        f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[s{i}]"
        for i, (start, end) in enumerate(segments)
    ]
    inputs = "".join(f"[s{i}]" for i in range(len(segments)))
    return ";".join(parts) + f";{inputs}concat=n={len(segments)}:v=1:a=0[cut]"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--name", default="hero")
    parser.add_argument("--width", type=int, default=800)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--colors", type=int, default=128)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    video = args.manifest.parent / manifest["video"]
    segments = keep_segments(manifest["skips"], duration(video))
    cut = concat_filter(segments)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gif = args.out_dir / f"{args.name}.gif"
    mp4 = args.out_dir / f"{args.name}.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-filter_complex",
            f"{cut};[cut]fps={args.fps},scale={args.width}:-1:flags=lanczos,split[a][b];"
            f"[a]palettegen=max_colors={args.colors}:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            str(gif),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-filter_complex",
            f"{cut};[cut]fps=30,format=yuv420p[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-crf",
            "20",
            "-preset",
            "slow",
            "-movflags",
            "+faststart",
            str(mp4),
        ],
        check=True,
    )
    kept = sum(end - start for start, end in segments)
    print(f"{gif} ({gif.stat().st_size / 1e6:.1f} MB) and {mp4} ({mp4.stat().st_size / 1e6:.1f} MB), {kept:.1f}s")


if __name__ == "__main__":
    main()
