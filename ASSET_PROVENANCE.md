# 素材来源与发布状态

本文件是发行门禁的一部分，登记会进入源码仓库或发行包的字体、图标、Logo、App Icon、插画和生成纹理。根目录 MIT 许可证只覆盖项目有权许可的代码和文档，不会自动改变第三方素材的许可，也不能替代 AI 生成平台或素材提供方的条款核验。

状态含义：

- `CLEARED`：来源、许可和再分发条件已在仓库中形成可核验记录；
- `REVIEW_REQUIRED`：有初步来源，但仍需补齐原文、校验值或授权判断；
- `BLOCKED`：不得进入公开发行包，必须补证或替换；
- `TO_REPLACE`：已决定替换，替换完成前不得公开发行。

公开发行前，`RELEASE_CHECKLIST.md` 要求发行范围内不存在 `BLOCKED` 或 `TO_REPLACE` 项。自动检查会核对正式素材路径与当前 SHA-256 是否在本文件中登记，但不能替代人工权利核验。

## 每项记录必须包含

新增或替换素材时，至少记录：

1. 仓库路径和用途；
2. 原始文件名、来源 URL 或生成工具/模型；
3. 作者、版权方或生成操作人；
4. 获取/生成日期与上游版本；
5. 对原始素材执行的裁切、压缩、描摹、子集化等修改；
6. 适用许可和允许捆绑再分发的依据；
7. 当前文件 SHA-256；
8. 原始文件、许可快照、订单或授权邮件等凭证的归档位置；
9. 状态、复核人和复核日期。

凭证可能包含个人信息时，只在本文件记录受控归档位置，不要把私人邮件、订单或账户信息提交到仓库。

## 当前素材登记

### Lucide 图标

- 路径：`static/icons/*.svg`、生成文件 `static/icons.js`
- 用途：界面功能图标
- 来源/版本：Lucide Static 0.544.0；SVG 文件头带版本与许可标记
- 修改：`tools/gen_icons.py` 清理注释并生成 JavaScript 注册表；不改变图标路径语义
- 许可：ISC；部分 Feather 来源图标同时保留 MIT 条款
- 凭证：`licenses/Lucide-LICENSE.txt`、`THIRD_PARTY_NOTICES.md`
- 状态：`CLEARED`
- 复核：发布负责人仍需在每次上游升级后重新核对版本与许可

### Geist Mono

- 路径：`static/fonts/GeistMono-Variable.woff2`
- 用途：数据与代码字体
- 来源/版本：Vercel Geist Mono；仓库当前未单独记录上游 release/tag
- 修改：未记录
- 许可：SIL Open Font License 1.1
- SHA-256：`fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab`
- 凭证：`licenses/Geist-OFL-1.1.txt`、`THIRD_PARTY_NOTICES.md`
- 状态：`REVIEW_REQUIRED`
- 待办：补齐准确上游版本、下载 URL 和原始文件校验值

### 统一品牌标识与图标导出

- 路径与 SHA-256：
  - `static/assets/console-app-icon.png`：`464d5ed1ca52d33c64de4f004df126f280f27f20346620ef0b2e6cb4143ccec3`
  - `static/assets/brand-mark.png`：`44644d14d7e3cf91808fa2f03e7735f7f4a9ab6c635f29eb98cf7ad4c85eaa0f`
  - `static/assets/favicon-32.png`：`6c1c34a718d9f26737fc1edc2a1a1fd3838e66826e0a19284e116449f031abbb`
  - `static/assets/favicon.ico`：`71b9aa89ea479762f7ed7c54a665c88ef7786089523417119292d446ea12648d`
  - `static/assets/apple-touch-icon.png`：`1108214aa511f206409c2daf7a3f7ac318dd4d2554a95476f2f606bfe8b49621`
- 用途：浏览器 favicon、Apple Touch Icon 与网页顶栏品牌标识
- 设计：琥珀色“长期服务”轨道与紫色“批处理任务”轨道汇聚到青柠色状态节点，呼应产品的两类本地操作与统一监测
- 来源：由项目维护者在用户明确选定第三套方向后，于 2026-07-23 在 Codex 中使用 OpenAI Image Generation `image_gen` 工具生成；工具没有向本次会话暴露底层模型版本
- 修改：主输出经透明通道整理、裁切和安全留白处理形成 `console-app-icon.png` 与 `brand-mark.png`；`tools/gen_brand_assets.py` 使用 Pillow Lanczos 缩放生成网页图标
- 凭证：本地维护档案 `tmp/brand/`（不进入 Git/发行包）、`tools/gen_brand_assets.py`、本文件校验值和 Git 历史
- 许可：作为项目定向生成的品牌素材使用；公开发行前仍需由发布负责人保存当次 OpenAI 适用条款、生成主体和可再分发结论
- 状态：`REVIEW_REQUIRED`
- 待办：将原始输出、当次条款快照和人工权利结论归档到发布记录；若未来需要可编辑矢量主源，应另行重绘并重新登记，不能把当前 PNG 描摹为“原始矢量”

## 更新规则

- 文件内容变化后必须更新 SHA-256 和修改说明。
- 上游素材升级后必须重新核对许可原文、版本和版权声明。
- AI 生成素材不能只记录“AI 生成”；必须能说明生成主体、平台/模型、生成日期、适用条款和原始输出凭证。
- 发行负责人必须对最终解压产物重新计算校验值，而不是只核对开发工作区。
