#!/usr/bin/env python3
"""从品牌 AppIcon 主图生成网页图标。

依赖 Pillow（requirements-dev.txt）。
主图必须是带透明通道的正方形 PNG，并自行包含安全留白。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "static" / "assets" / "console-app-icon.png"
ASSETS = ROOT / "static" / "assets"


def resized(source: Image.Image, size: int) -> Image.Image:
    return source.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"缺少品牌主图：{SOURCE}")
    source = Image.open(SOURCE).convert("RGBA")
    if source.width != source.height:
        raise SystemExit("品牌主图必须是正方形")

    resized(source, 32).save(ASSETS / "favicon-32.png", optimize=True)
    resized(source, 180).save(ASSETS / "apple-touch-icon.png", optimize=True)
    source.save(
        ASSETS / "favicon.ico",
        format="ICO",
        sizes=[(16, 16), (32, 32), (48, 48)],
    )

    print(f"已生成 {ASSETS / 'favicon.ico'}")
    print(f"已生成 {ASSETS / 'favicon-32.png'}")
    print(f"已生成 {ASSETS / 'apple-touch-icon.png'}")


if __name__ == "__main__":
    main()
