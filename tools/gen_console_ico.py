#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从品牌大图生成多尺寸 console.ico（16/32/48/256，零依赖）。

用法: python tools/gen_console_ico.py
输出: console.ico(项目根,供 LocalOpsConsole.exe 图标)
实现:标准库解码 PNG(zlib)→ 面积平均缩放 → ICO 内嵌 32bpp BMP。
"""
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "static", "assets", "console-app-icon.png")
OUT = os.path.join(ROOT, "console.ico")
SIZES = (16, 32, 48, 256)


def decode_png(data):
    """PNG → (width, height, bgra)。支持 8 位色深 RGBA/RGB + 四种 filter。"""
    pos = 8
    width = height = color_type = 0
    idat = b""
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            (width, height, _bd, color_type, _c, _f,
             _i) = struct.unpack(">IIBBBBB", payload)
        elif tag == b"IDAT":
            idat += payload
        elif tag == b"IEND":
            break
        pos += 12 + length
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw = zlib.decompress(idat)
    stride = width * ch
    rows = []
    prev = bytearray(stride)
    off = 0
    for _ in range(height):
        (filt,) = raw[off:off + 1]
        line = bytearray(raw[off + 1:off + 1 + stride])
        off += 1 + stride
        if filt == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line))
        prev = line
    out = bytearray()
    for row in reversed(rows):
        for i in range(width):
            if color_type == 6:
                r, g, b, a = row[i * 4:i * 4 + 4]
            elif color_type == 2:
                r, g, b = row[i * 3:i * 3 + 3]
                a = 255
            elif color_type == 4:
                g, a = row[i * 2:i * 2 + 2]
                r = g
                b = g
            else:
                g = row[i]
                r = g
                b = g
                a = 255
            out += bytes((b, g, r, a))
    return width, height, bytes(out)


def resize_bgra(bgra, w, h, nw, nh):
    """面积平均缩放(向下缩放质量好)。"""
    out = bytearray()
    for y in range(nh):
        sy0 = int(y * h / nh)
        sy1 = max(int((y + 1) * h / nh), sy0 + 1)
        for x in range(nw):
            sx0 = int(x * w / nw)
            sx1 = max(int((x + 1) * w / nw), sx0 + 1)
            bs = gs = rs = asum = 0
            n = 0
            for sy in range(sy0, sy1):
                base = sy * w * 4
                for sx in range(sx0, sx1):
                    i = base + sx * 4
                    bs += bgra[i]
                    gs += bgra[i + 1]
                    rs += bgra[i + 2]
                    asum += bgra[i + 3]
                    n += 1
            out += bytes((bs // n, gs // n, rs // n, asum // n))
    return bytes(out)


def bmp_image(w, h, bgra):
    """ICO 内嵌 32bpp BMP(自底向上 BGRA + 1bpp AND 掩码)。"""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    row_bytes = w * 4
    row_pad = (4 - row_bytes % 4) % 4
    pixels = bytearray()
    for y in range(h - 1, -1, -1):
        pixels += bgra[y * row_bytes:(y + 1) * row_bytes]
        pixels += b"\x00" * row_pad
    and_row = ((w + 31) // 32) * 4
    and_mask = b"\x00" * (and_row * h)
    return header + bytes(pixels) + and_mask


def main():
    if not os.path.isfile(SRC):
        print("缺少源图标: %s" % SRC)
        return 1
    with open(SRC, "rb") as f:
        data = f.read()
    w, h, bgra = decode_png(data)
    print("源图: %dx%d" % (w, h))
    entries = []
    offset = 6 + 16 * len(SIZES)
    for size in SIZES:
        if size == w and size == h:
            resized = bgra
        else:
            resized = resize_bgra(bgra, w, h, size, size)
        blob = bmp_image(size, size, resized)
        entries.append((size, blob))
        offset += len(blob)
    with open(OUT, "wb") as f:
        f.write(struct.pack("<HHH", 0, 1, len(SIZES)))
        cur = 6 + 16 * len(SIZES)
        for size, blob in entries:
            # ICO 目录项尺寸字段:0 表示 256
            dim = 0 if size >= 256 else size
            f.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                                len(blob), cur))
            cur += len(blob)
        for _size, blob in entries:
            f.write(blob)
    print("已生成多尺寸 ICO: %s (%d bytes) 尺寸=%s" %
          (OUT, os.path.getsize(OUT), "/".join(str(s) for s in SIZES)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
