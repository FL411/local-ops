#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从品牌 PNG 生成 console.ico（ICO 内嵌 PNG，Vista+ 支持，零依赖）。

用法: python tools/gen_console_ico.py
输出: console.ico(项目根,供桌面快捷方式使用)
"""
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "static", "assets", "favicon-32.png")
OUT = os.path.join(ROOT, "console.ico")


def main():
    if not os.path.isfile(SRC):
        print("缺少源图标: %s" % SRC)
        return 1
    with open(SRC, "rb") as f:
        png = f.read()
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        print("源文件不是 PNG")
        return 1
    # ICO 头 + 单目录项(内嵌 PNG 图像)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22)
    with open(OUT, "wb") as f:
        f.write(header)
        f.write(entry)
        f.write(png)
    print("已生成: %s (%d bytes)" % (OUT, 22 + len(png)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
