# lark-md-export 行为详情

> 本文件为 `lark-md-export` skill 的行为详情文档，与 `SKILL.md` 配套使用。

## 产物结构

```
{导出根}/
├── {文档标题}_v{MMdd}.md                 # 正文（引用已重写为相对路径）
├── {文档标题}_v{MMdd}.export-log.md      # 导出日志（内联/失败/降级/未本地化的块类型）
└── {文档标题}_v{MMdd}.assets/
    ├── img-{token前8}.png                # 文档图片（扩展名随服务端 Content-Type）
    ├── board-01-{token前6}.svg           # 自包含矢量归档源（截图会自动内联）
    ├── board-01-{token前6}.png           # 长边 4096px 的高清 PNG，供 md / 多模态模型读取
    ├── board-02-{token前6}.jpg           # 仅 SVG 导出或内联失败时的低清预览兜底
    ├── board-01-{token前6}.mmd           # 画板源码（可提取时；plantuml 为 .puml）
    ├── ext-{hash}.png                    # 外链网络图
    └── {附件原名}                         # 附件（同次导出重名时补 -{token前8}）
```

## md 内形态转换

| 原块 | 替换为 |
|---|---|
| 图片 `![](https://feishu.cn/file/{token})` | `![](测试.assets/img-xxx.png)` |
| 普通画板 `<whiteboard token="X"></whiteboard>` | `![](测试_v1009.assets/board-01-xxx.png)`；PNG 生成失败时回退 SVG + `<details>` 折叠源码（有源码时） |
| mermaid 画板（md 中为 ```mermaid 代码块） | 与 XML 画板清单按序对齐后替换为同款「图 + 折叠源码」；对齐失败保留原代码块 |
| 附件（`<figure ...><source name/token .../></figure>`） | `[name](测试.assets/name)`（figure 壳随替换去除） |

md 头部会插入一行导出元信息：`<!-- source: {URL} | revision: {N} | exported: {时间} -->`。

## 行为语义

- **增量**：全部 `{标题}_v{MMdd}.md` 历史版本的最新 revision 与远端一致 → 整体跳过；资源级：已存在文件跳过，`--force` 才覆盖
- **失败降级**：单资源失败不阻塞整体——该处保留原标签/原引用，其余照常完成，失败明细进日志
- **画板源码**：仅当画板内「有且仅有一个 mermaid/plantuml 图」时可提取；提不出属正常（日志不记失败）
- **范围外块**（多维表格 / 电子表格 / 思维导图 / 同步块 / 引用等）：保留原始 XML 标签，日志列清单

## 已知限制

- 画板导出保留自包含 SVG，并以 macOS `qlmanage` 渲染长边 4096px 的 PNG；Markdown 默认引用 PNG 便于多模态模型读取。PNG 生成失败回退 SVG；仅 SVG 导出或截图内联失败时回退服务端 preview JPG（约 2560px）
- 外链网络图（非 feishu 文件）走 https + 公网安全校验；私网 / 回环地址会被拒绝
- mermaid 对齐依赖「md 代码块数 == XML mermaid 画板数」；不等时保守保留原代码块（见日志）
- 批量模式（wiki 整树 / 文件夹）未实现；当前为单篇导出

## 数据通道（全部经 lark-cli）

| 步骤 | 命令 |
|---|---|
| 正文 | `docs +fetch --doc <url> --doc-format markdown` |
| 画板清单/标题 | `docs +fetch --doc <url> --doc-format xml` |
| 图片/附件 | `docs +media-download --token <t> --output <path>` |
| 画板 SVG | `whiteboard +export --whiteboard-token <t> --output-type svg` |
| 画板截图 token | `whiteboard +export --whiteboard-token <t> --output-type raw` + `docs +media-download` |
| 高清画板 PNG | `qlmanage -t -s 4096` 渲染自包含 SVG |
| 画板预览兜底 | `whiteboard +export --whiteboard-token <t> --output-type preview` |
| 画板源码 | `whiteboard +export --whiteboard-token <t> --output-type source` |

## lark-cli 交互注意事项

- lark-cli 命令失败时把错误 JSON 写到 **stderr**、stdout 为空（exit code 仍可能为 0）；解析错误必须读 stderr
- user 身份 refresh token 过期后降级为 bot，访问用户文档报 `3380004 No permission to operate on this document`；恢复方式为重新 `auth login`
- 文件路径参数（`--output` 等）只接受 cwd 下的相对路径
- 成功判定用 `ok == true`（JSON 信封），不要用 `code == 0`（`code` 只出现在错误信封内）
