"""Cut the waits out of a recording and export it as a GIF and an MP4.

The README hero stops at the merge request; the launch video runs to the merge and ends on a still card:

uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/demo.json ~/.cache/daiv-demo \
  --until hero
uv run --no-project python -I scripts/demo/make_gif.py ~/.cache/daiv-demo/recordings/demo.json ~/.cache/daiv-demo \
  --name demo --formats mp4 --outro ~/.cache/daiv-demo/outro.png
"""

import argparse
import json
import subprocess
from pathlib import Path


def probe(video: Path, entries: str) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "csv=p=0", str(video)],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip().splitlines()[0].split(",")


def keep_segments(skips: list[dict], total: float) -> list[tuple[float, float]]:
    keep, cursor = [], 0.0
    for skip in sorted(skips, key=lambda s: s["start"]):
        if skip["start"] >= total:
            break
        if skip["start"] > cursor:
            keep.append((cursor, skip["start"]))
        cursor = max(cursor, skip["end"])
    if total > cursor:
        keep.append((cursor, total))
    return keep


def cut_filter(segments: list[tuple[float, float]]) -> str:
    """One pass over the input: a trim and concat per segment buffers every later segment's decoded frames."""
    kept = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in segments)
    return f"[0:v]fps=30,select='{kept}',setpts=N/30/TB[cut]"


def export_gif(video: Path, cut: str, gif: Path, args: argparse.Namespace) -> None:
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


def export_mp4(video: Path, cut: str, mp4: Path, args: argparse.Namespace) -> None:
    inputs = ["-i", str(video)]
    graph = f"{cut};[cut]fps=30,format=yuv420p,setsar=1[v]"
    if args.outro:
        width, height = probe(video, "stream=width,height")
        inputs += ["-loop", "1", "-t", f"{args.outro_seconds}", "-i", str(args.outro)]
        graph = (
            f"{cut};[cut]fps=30,format=yuv420p,setsar=1[main];"
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p,setsar=1[outro];"
            "[main][outro]concat=n=2:v=1:a=0[v]"
        )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            *inputs,
            "-filter_complex",
            graph,
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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--name", default="hero")
    parser.add_argument("--until", help="end at this mark of the recording, e.g. hero")
    parser.add_argument("--formats", default="gif,mp4")
    parser.add_argument("--width", type=int, default=800, help="GIF width")
    parser.add_argument("--fps", type=int, default=10, help="GIF frame rate")
    parser.add_argument("--colors", type=int, default=128)
    parser.add_argument("--outro", type=Path, help="still image held at the end of the MP4")
    parser.add_argument("--outro-seconds", type=float, default=3.0)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text())
    video = args.manifest.parent / manifest["video"]
    total = manifest["marks"][args.until] if args.until else float(probe(video, "format=duration")[0])
    segments = keep_segments(manifest["skips"], total)
    cut = cut_filter(segments)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    formats = set(args.formats.split(","))

    written = []
    if "gif" in formats:
        export_gif(video, cut, gif := args.out_dir / f"{args.name}.gif", args)
        written.append(gif)
    if "mp4" in formats:
        export_mp4(video, cut, mp4 := args.out_dir / f"{args.name}.mp4", args)
        written.append(mp4)
    kept = sum(end - start for start, end in segments) + (args.outro_seconds if args.outro else 0)
    print(", ".join(f"{path} ({path.stat().st_size / 1e6:.1f} MB)" for path in written) + f", {kept:.1f}s")


if __name__ == "__main__":
    main()
