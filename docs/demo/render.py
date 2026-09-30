# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "pillow>=10"]
# ///
"""Render an asciicast v2 recording to GIF, MP4 and a still PNG.

    uv run render.py convene.cast

The cast keeps real timing. Here, any pause longer than IDLE_CAP seconds is
shortened to IDLE_CAP (the same thing `asciinema rec -i` does), so the wait
for the model does not dominate the loop; the timings convene itself prints
are the real ones. Needs ffmpeg on PATH for the MP4.
"""

import datetime
import itertools
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

IDLE_CAP = 1.5  # seconds
FRAME_MIN = 0.04  # coalesce output that lands closer together than this
HOLD_END = 4.0  # seconds on the last frame before the loop restarts
SCALE = 2  # render at 2x so the GIF stays sharp on high-density screens
FONT_PX = 16
LINE = 1.4
PAD = 14
TITLE_H = 30

BG = "#0a0e12"
FG = "#a3aeb9"
AMBER = "#f5a524"
CYAN = "#3fd0e0"
EDGE = "#173a42"
DIM = "#5b6670"
FONTS = [
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
NAMED = {"default": FG, "white": "#e8edf2", "red": "#ff6b6b", "green": "#7bd88f"}


def load_font(px: int) -> ImageFont.FreeTypeFont:
    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, px)
    sys.exit("no monospace font found; add one to FONTS")


def events(cast: Path) -> tuple[dict, list[tuple[float, str]]]:
    lines = cast.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    out, clock, last = [], 0.0, 0.0
    for line in lines[1:]:
        t, kind, data = json.loads(line)
        if kind != "o":
            continue
        clock += min(t - last, IDLE_CAP)
        last = t
        out.append((clock, data))
    return header, out


def colour(c: str) -> str:
    if c in NAMED:
        return NAMED[c]
    return f"#{c}" if len(c) == 6 else FG


def draw(screen: pyte.Screen, font, cw: float, title: str, stamp: str) -> Image.Image:
    s = SCALE
    lh = round(FONT_PX * LINE * s)
    w = round(2 * PAD * s + screen.columns * cw)
    h = round((2 * PAD + TITLE_H) * s + screen.lines * lh)
    img = Image.new("RGB", (w + w % 2, h + h % 2), BG)
    d = ImageDraw.Draw(img)
    # Hairline bezel, amber title, dim recording date.
    d.rectangle(
        [s * 4, s * 4, img.width - s * 4, img.height - s * 4], outline=EDGE, width=s
    )
    small = font.font_variant(size=round(13 * s))
    d.text((PAD * s, s * 16), title, font=small, fill=AMBER)
    d.text((img.width - PAD * s, s * 16), stamp, font=small, fill=DIM, anchor="ra")
    top = (PAD + TITLE_H) * s
    for y in range(screen.lines):
        row = screen.buffer[y]
        for x in range(screen.columns):
            ch = row[x]
            if ch.data in (" ", ""):
                continue
            d.text(
                (PAD * s + x * cw, top + y * lh), ch.data, font=font, fill=colour(ch.fg)
            )
    if not screen.cursor.hidden:
        cx, cy = PAD * s + screen.cursor.x * cw, top + screen.cursor.y * lh
        d.rectangle([cx, cy + s * 2, cx + cw - 1, cy + lh - s * 2], fill=CYAN)
    return img


def main(cast: Path) -> None:
    header, evs = events(cast)
    screen = pyte.Screen(header["width"], header["height"])
    stream = pyte.Stream(screen)
    font = load_font(FONT_PX * SCALE)
    cw = font.getlength("M")
    stamp = "recorded " + datetime.date.fromtimestamp(header["timestamp"]).isoformat()

    frames: list[tuple[float, Image.Image]] = []
    i = 0
    while i < len(evs):
        t = evs[i][0]
        while i < len(evs) and evs[i][0] - t < FRAME_MIN:
            stream.feed(evs[i][1])
            i += 1
        frames.append((t, draw(screen, font, cw, "convene", stamp)))

    durations = [b[0] - a[0] for a, b in itertools.pairwise(frames)] + [HOLD_END]
    images = [img for _, img in frames]
    stem = cast.with_suffix("")

    images[-1].save(f"{stem}.png")
    palette = images[-1].quantize(colors=48)
    gif = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
    gif[0].save(
        f"{stem}.gif",
        save_all=True,
        append_images=gif[1:],
        duration=[max(20, round(d * 1000)) for d in durations],
        loop=0,
        optimize=True,
    )

    with tempfile.TemporaryDirectory() as tmp:
        listing = []
        for n, (im, d) in enumerate(zip(images, durations, strict=True)):
            im.save(f"{tmp}/{n:05}.png")
            listing += [f"file '{tmp}/{n:05}.png'", f"duration {d:.3f}"]
        # concat drops the last duration unless the last file is listed twice;
        # -t then trims the result to the intended length.
        listing.append(f"file '{tmp}/{len(images) - 1:05}.png'")
        Path(f"{tmp}/list.txt").write_text("\n".join(listing))
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                f"{tmp}/list.txt",
                "-vf",
                "fps=30,format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "20",
                "-movflags",
                "+faststart",
                "-t",
                f"{sum(durations):.3f}",
                f"{stem}.mp4",
            ],
            check=True,
        )
    print(f"{len(images)} frames, {sum(durations):.1f}s -> {stem}.{{gif,mp4,png}}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
