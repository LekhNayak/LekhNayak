#!/usr/bin/env python3
"""
Re-embed the ASCII portrait from face.txt into profile.svg.

Usage:
    python3 update_face.py

Run this after editing face.txt, then commit both files:
    git add face.txt profile.svg && git commit -m "Update face art" && git push

The art is cropped to fit the 355px column at the same 4.3px zoom
(max 137 cols wide x 92 rows tall), centered on the face, with the
line-by-line loading animation intact. Stats in profile.svg are untouched.
"""
import re

SVG = 'profile.svg'
ART = 'face.txt'

FONT = 4.3          # px, the zoom level (same as current)
CHAR_W = FONT * 0.6  # monospace advance
MAXCOLS = 137       # 137 * CHAR_W = 353.5px, fits the 355px column
MAXROWS = 92        # fits the 140-570 art area at FONT px


def load_art():
    """Read face.txt, drop blank rows and the uniform left margin."""
    lines = [l.rstrip() for l in open(ART, encoding='utf-8').read().splitlines()]
    content = [l for l in lines if l.strip()]
    left = 0
    while all(len(l) > left and l[left] == ' ' for l in content):
        left += 1
    return [l[left:] for l in content]


def crop(art):
    """Crop to MAXCOLS x MAXROWS, horizontally centered on the head."""
    head = art[:max(1, int(len(art) * 0.8))]  # head sits in the top 80%
    cols = [j for r in head for j, c in enumerate(r) if c != ' ']
    center = sum(cols) // len(cols)
    x0 = max(0, center - MAXCOLS // 2)
    return [r[x0:x0 + MAXCOLS] for r in art[:MAXROWS]]


def build_block(art):
    """Build the <text> block: one tspan per row, same colors/animations."""
    W = max(len(r) for r in art)
    N = len(art)
    spacing = round(FONT * 1.0909, 3)
    x = round(15 + (355 - W * CHAR_W) / 2, 2)            # centered in 15-370
    start_y = round(140 + (430 - (N - 1) * spacing) / 2, 1)  # centered in 140-570
    rows = []
    for i, row in enumerate(art):
        y = round(start_y + i * spacing, 1)
        delay = round(0.90 + i * (0.05 / 1.5), 2)  # 0.033s step (1.5x speed)
        rows.append(f'<tspan x="{x}" y="{y}" class="t" style="animation-delay:{delay}s">{row.ljust(W)}</tspan>')
    return (f'<text x="{x}" y="{start_y}" fill="#c9d1d9" font-size="{FONT}px" '
            f'stroke="#c9d1d9" stroke-width="0.5">\n' + '\n'.join(rows) + '\n</text>')


def main():
    art = crop(load_art())
    block = build_block(art)
    src = open(SVG, encoding='utf-8').read()
    pat = re.compile(r'<text[^>]*fill="#c9d1d9" font-size="4.3px"[^>]*>.*?</text>', re.S)
    m = pat.search(src)
    if not m:
        raise SystemExit('art block not found in profile.svg')
    src = src[:m.start()] + block + src[m.end():]
    open(SVG, 'w', encoding='utf-8').write(src)
    print(f'Embedded {len(art)} rows x {max(len(r) for r in art)} cols from {ART} into {SVG}')


if __name__ == '__main__':
    main()
