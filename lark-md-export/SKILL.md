---
name: lark-md-export
description: 把飞书云文档（docx / wiki）导出为本地自包含 Markdown 资源包：图片、画板（高清 PNG/矢量 SVG + 源码折叠）、附件全部下载到本地并以相对路径嵌入，附导出日志与增量判断；含环境自举（lark-cli 检测与安装、应用配置、Device Flow 授权引导），用户只需给一个文档 URL 即可完成下载。Use when 用户给出 feishu.cn 的 /docx/ 或 /wiki/ 链接要求下载/导出/保存/本地化为 Markdown，或要求把飞书文档连同图片、画板、附件一起离线归档到本地。
---

# lark-md-export — 飞书文档导出为本地 Markdown

把飞书云文档（docx / wiki）导出为**本地自包含** Markdown 资源包：图片、画板、附件全部下载到本地，以相对路径嵌入 md。核心脚本零第三方依赖，数据全部经 lark-cli 获取。

## 何时使用

- 用户给出飞书文档 URL（`/docx/` 或 `/wiki/`）要求「下载 / 导出 / 保存到本地 / 转成 Markdown」
- 要求把飞书文档离线归档（含图片、画板、附件）
- **不适用**：只需读取文档内容或在飞书上编辑 → 用 lark-doc 系列 skill

## 工作流

```
任务进度：
- [ ] 1. 环境自举（lark-cli 三条就绪检查）
- [ ] 2. 执行导出
- [ ] 3. 汇报产物与日志
```

### 第 1 步：环境自举

按序执行三项检查，全部就绪才进入导出。

**1.1 lark-cli 检测**

```bash
which lark-cli && lark-cli --version
```

- 缺失时安装（官方 npm 全局包）：
  ```bash
  npm install -g @larksuite/cli
  ```
- 若 npm / node 不可用：告知用户需先安装 Node.js，不要自创其他安装方式。

**1.2 应用配置检测**

```bash
lark-cli config show
```

- 输出无 `appId` 或报错 → 首次使用需完成应用配置：
  ```bash
  lark-cli config init --new
  ```
  - 该命令阻塞等待用户操作，用 background 方式发起，读取输出提取 `verification_url`
  - 生成二维码并展示给用户（见「URL 展示纪律」）；用户完成后回读确认配置就绪

**1.3 用户身份检测**

```bash
LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 lark-cli auth status --json --verify
```

- 判定：`identities.user` 存在且可用（`status` 为 `ready` 或 `needs_refresh`——后者会在下次调用时自动刷新）→ 就绪，进入第 2 步
- 缺失 / 过期且无法刷新 → 走 Device Flow 授权（split-flow，分两轮）：
  1. 本轮发起：`lark-cli auth login --domain all --no-wait --json`
  2. 从 JSON 提取 `verification_url` 与 `device_code`
  3. 生成二维码：`lark-cli auth qrcode <verification_url> --output "./lark-auth-qrcode.png"`（路径须为 cwd 相对路径）
  4. 先 URL、后二维码展示给用户，明确告知：「完成授权后回来告诉我已授权完成，我会帮你完成后续步骤」；结束本轮
  5. 用户确认后（下一轮）：由你执行 `lark-cli auth login --device-code <device_code>` 完成登录
- 纪律：
  - 禁止在同一轮展示 URL 后立刻执行 `--device-code`（阻塞轮询会导致用户看不到 URL）
  - 禁止复用已过期链接：每次授权重新发起，拿新的 `verification_url` 与 `device_code`
  - 导出用户文档必须 user 身份；bot 身份访问用户文档会报 3380004

### 第 2 步：执行导出

```bash
python3 ~/.qoder/skills/lark-md-export/scripts/lark_md_export.py "<文档URL>" [-o <导出目录>] [--force]
```

- **导出根**：`-o` 指定；缺省 = 脚本运行目录（cwd）。若 cwd 是代码仓库根目录且用户未明示导出到当前目录，先与用户确认导出位置
- **增量**：历史版本最新 revision 与远端一致 → 整体跳过；`--force` 强制重导（覆盖已存在文件）
- **依赖**：Python 3（仅标准库）+ lark-cli；高清画板 PNG 依赖 macOS 内置 `qlmanage`（缺失时自动降级并记日志）
- 单资源失败不阻塞整体：该处保留原引用，明细写入导出日志

### 第 3 步：汇报产物

向用户汇报三件套（路径均为绝对路径）：

| 产物 | 路径 |
|---|---|
| 正文 | `{导出根}/{标题}_v{MMdd}.md` |
| 资源目录 | `{标题}_v{MMdd}.assets/`（N 个文件） |
| 导出日志 | `{标题}_v{MMdd}.export-log.md` |

有失败 / 降级项时：重述关键项并给修复建议（对照下节「排障速查」）。

## 排障速查

| 症状 | 处理 |
|---|---|
| 找不到 lark-cli | 检查 PATH（nvm 环境 npm 全局 bin）；或设 `LARK_CLI` 环境变量指向可执行文件 |
| 报错含 `3380004` / No permission | user 身份失效降级为 bot → 重新走 1.3 授权 |
| lark-cli 输出无法解析 | 错误 JSON 写在 **stderr**、stdout 为空（exit code 可能为 0）→ 解析 stderr 的 JSON 拿 code/message |
| 画板无高清 PNG | 本机无 `qlmanage`；md 自动回退引用 SVG（可接受），日志有记录 |
| 提示「已是最新」但想要新产物 | 加 `--force` 重导 |
| 画板图文字看不清 | 复杂画板 PNG 长边上限 4096px；SVG 矢量源已并存于 assets 目录 |

## URL 展示纪律（引用 lark-shared 规范）

1. `verification_url` 等 URL 必须原样转发（不编解码、不加标点、不重拼 query），且**必须配二维码**（`lark-cli auth qrcode` 生成 PNG），先 URL 后二维码
2. 判断 lark-cli 成功用 `ok == true`（或退出码 0），**不要**用 `code == 0`
3. lark-cli 文件路径参数只接受 cwd 相对路径

## 详情索引

- 产物结构 / md 内形态转换 / 行为语义 / 已知限制 / 数据通道 / lark-cli 交互注意 → [references/export-behavior.md](references/export-behavior.md)
- 导出机制细节问题（如「为什么画板 PNG / SVG 双份」）→ 同上

## 维护说明

- 脚本开发源：`JCRM-NEW-DOC/tools/lark-md-export/lark_md_export.py`；本 skill 内 `scripts/lark_md_export.py` 为交付副本——源侧迭代后同步复制并跑 `diff` 对账
- 本 skill 位于用户级 `~/.qoder/skills/lark-md-export/`；台账 `~/.qoder/skills/README.md` 同步维护
