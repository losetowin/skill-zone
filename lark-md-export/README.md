# lark-md-export

把飞书云文档（docx / wiki）导出为**本地自包含 Markdown 资源包**：图片、画板、附件全部下载到本地并以相对路径嵌入，完全离线可读，能进代码仓，AI（含多模态）可直接消化。

- **一条链接进，一个本地文件夹出**——核心脚本零第三方依赖（Python 标准库 + lark-cli + macOS 内置命令）
- **画板完整带出**——高清 PNG（正文引用）+ 矢量 SVG（归档）+ 源码（mermaid / plantuml 折叠），不丢流程图
- **环境自举**——lark-cli 缺失自动安装、首次使用引导应用配置与 Device Flow 授权，用户只需给一个文档 URL

## 这是什么

一份 Agent Skill（安装于 AI 编程工具的 skills 目录，支持 Qoder、Claude Code、Cursor 等 32 种工具），由两重形态组成：

1. **AI Skill**：AI 助手检测到「飞书文档下载 / 导出」意图时自动加载并执行（环境检查 → 授权引导 → 导出 → 汇报产物）；
2. **独立脚本**：`scripts/lark_md_export.py` 也可作为普通 CLI 工具直接使用，不依赖 AI。

## 目录结构

```
lark-md-export/
├── SKILL.md                        # AI 执行指令：触发条件、环境自举三条检查、导出工作流、排障速查
├── README.md                       # 本文件：面向人的使用与维护说明
├── references/
│   └── export-behavior.md          # 行为详情：产物结构、md 形态转换、数据通道、已知限制
└── scripts/
    └── lark_md_export.py           # 核心脚本（零第三方依赖，可独立运行）
```

## 安装

### 方式一：npx skills（推荐）

开放 Agent Skills 生态的统一安装器，自动适配 Qoder、Claude Code、Cursor、Codex 等 32 种 AI 编程工具：

```bash
# 全局安装（跨项目可用）
npx skills add losetowin/skill-zone --skill lark-md-export -g

# 或安装到当前项目（随项目共享给团队）
npx skills add losetowin/skill-zone --skill lark-md-export
```

也可用完整子目录 URL 安装：

```bash
npx skills add https://github.com/losetowin/skill-zone/tree/main/lark-md-export
```

### 方式二：手动安装

将 `lark-md-export` 目录拷贝到所用工具的 skills 目录即可，例如 Qoder：

```bash
cp -r lark-md-export ~/.qoder/skills/
```

## 快速使用

### 方式一：交给 AI（推荐）

对 AI 助手说一句话即可：

```
帮我把这篇文档下载到本地 https://xxx.feishu.cn/wiki/xxxx
```

AI 会自动完成：环境检查 →（首次）安装 lark-cli / 应用配置 / 授权引导（扫码）→ 导出 → 汇报产物。

### 方式二：命令行直接跑

```bash
python3 scripts/lark_md_export.py "<文档URL>"                 # 导出到当前目录
python3 scripts/lark_md_export.py "<文档URL>" -o ~/exports    # 指定导出目录
python3 scripts/lark_md_export.py "<文档URL>" --force         # 强制重导（忽略增量判断）
```

## 首次使用：环境自举

| 依赖 | 说明 | 缺失时的处理 |
|---|---|---|
| lark-cli | 飞书官方 CLI（npm 全局包 `@larksuite/cli`） | `npm install -g @larksuite/cli`（需 Node.js） |
| Python 3 | 仅用标准库 | macOS 自带 |
| macOS qlmanage | 画板高清 PNG 渲染 | 缺失时自动降级（md 回退引用 SVG），不影响主体功能 |

首次使用需要完成一次性配置（AI 会自动引导；手工路径如下）：

```bash
lark-cli config init --new    # 应用配置（首次）
lark-cli auth login           # Device Flow 授权（扫码，一次即可）
lark-cli auth status          # 检查授权状态；user 身份可用即可
```

## 产物说明

```
{文档标题}_v{MMdd}.md             # 正文（引用已重写为相对路径）
{文档标题}_v{MMdd}.assets/        # 全部素材：图片、画板（png/svg/源码）、附件（保留原名）
{文档标题}_v{MMdd}.export-log.md  # 导出日志：失败、降级、未本地化块清单
```

- 同一文档跨天重导生成新版本（`_v{MMdd}` 前缀），历史版本互不覆盖；
- 单资源失败不阻塞整体：坏的保留原引用，好的照常完成，明细进日志。

## 常见问题

| 现象 | 处理 |
|---|---|
| 提示无权限 / 3380004 | 授权过期：`lark-cli auth login` 重新授权 |
| 找不到 lark-cli | 确认 npm 全局 bin 在 PATH 中，或设置 `LARK_CLI` 环境变量 |
| 提示「已是最新」 | 文档未变化；确需重导加 `--force` |
| 画板无高清 PNG | 本机 qlmanage 不可用，已自动回退引用 SVG |

更多排障与行为细节见「深入阅读」中的两份文档。

## 维护说明

- 本目录即 skill 的完整源：`SKILL.md` + `references/` + `scripts/`，拷贝整个目录即可分发或二次安装；
- 脚本修改后建议验证：`python3 -m py_compile scripts/lark_md_export.py`，并用实际文档跑一次 `--force` 导出确认产物完整；
- 通过 `npx skills` 安装的副本：`npx skills update` 可追踪更新；手动安装的副本需重新拷贝。

## 深入阅读

- [SKILL.md](SKILL.md)——AI 执行指令（触发条件、环境自举、工作流、URL 展示纪律）
- [references/export-behavior.md](references/export-behavior.md)——产物形态、行为语义、lark-cli 数据通道、已知限制
