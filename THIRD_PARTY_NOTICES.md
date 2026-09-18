# 第三方软件与素材声明

项目有权许可的自有代码和文档采用根目录 `LICENSE` 中的 MIT License。MIT License 不会自动改变下列第三方素材、品牌素材或 AI 生成素材的许可状态；对外分发前必须核对来源、版本、完整许可和再分发范围，并由发布负责人确认。

素材级来源、SHA-256、修改记录与发布状态见 `ASSET_PROVENANCE.md`。两份文件必须同步维护：本文件说明适用权利和上游声明，素材台账负责逐文件追溯与发布门禁。

## Lucide Icons

- 位置：`static/icons/*.svg` 与由它们生成的 `static/icons.js`
- 版本：`lucide-static` 0.544.0（依据 SVG 文件头）
- 项目：<https://github.com/lucide-icons/lucide>
- 许可：ISC
- 随包许可原文：`licenses/Lucide-LICENSE.txt`（含部分 Feather 图标适用的 MIT 条款）

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part
of Feather (MIT). All other copyright (c) 2022, Lucide Contributors.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.

## Geist Mono

- 位置：`static/fonts/GeistMono-Variable.woff2`
- 项目：<https://github.com/vercel/geist-font>
- 许可：SIL Open Font License 1.1
- 版权：Copyright (c) 2023 Vercel, in collaboration with basement.studio
- 随包许可原文：`licenses/Geist-OFL-1.1.txt`

OFL 1.1 允许字体与软件一同捆绑和再分发，前提是每份副本包含版权声明与 OFL 许可文本，且不将字体文件单独出售。上游原文见：<https://github.com/vercel/geist-font/blob/main/LICENSE.txt>。

## 项目图像

- `static/assets/console-app-icon.png`、`brand-mark.png`、`favicon-32.png`、`favicon.ico` 与 `apple-touch-icon.png` 来自同一套品牌方向；派生图标由 `tools/gen_brand_assets.py` 生成。
- 上述品牌素材由项目维护者在用户选定方向后，于 2026-07-23 在 Codex 中使用 OpenAI Image Generation `image_gen` 工具定向生成。本次工具调用未暴露底层模型版本，因此在 `ASSET_PROVENANCE.md` 中标为 `REVIEW_REQUIRED`。

首次公开发行前，发布负责人必须归档原始输出、生成主体、当次适用条款和允许随本项目再分发的书面结论。若结论不支持当前发行方式，应在发布前替换素材并同步更新来源、修改记录、SHA-256 与状态。

## 开发期工具

`tools/gen_brand_assets.py` 使用 `requirements-dev.txt` 精确锁定的 Pillow 生成 PNG/ICO 网页图标；`tools/gen_icons.py` 由 vendored Lucide SVG 重新生成 `static/icons.js`。这些工具只用于重新生成已入库资源，不随总控台运行，也不是运行时依赖。更新版本时必须重新核对各自上游许可和来源记录。
