#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lark-md-export — 把飞书云文档（docx / wiki）导出为本地自包含 Markdown 资源包。

数据全部经 lark-cli 获取：
  1. docs +fetch --doc-format markdown  取正文
     （md 中：普通画板=<whiteboard token>；mermaid 画板=```mermaid 代码块；
       图片=![](https://feishu.cn/file/{token})；附件=<source token name>）
  2. docs +fetch --doc-format xml       取画板清单（token/type/顺序）、标题、附件清单
  3. docs +media-download               下载图片 / 附件素材
  4. whiteboard +export                 画板导出 svg 矢量图（失败时降级 preview 位图）与 source(mmd/puml)
  5. 引用重写为相对路径，落盘 {标题}_v{MMdd}.md + {标题}_v{MMdd}.assets/
     + {标题}_v{MMdd}.export-log.md（版本化命名，历史版本各自独立、互不覆盖）

默认导出根 = 运行脚本时所在的目录（cwd）；用 -o 指定其他目录。
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import html
import ipaddress
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LARK = os.environ.get("LARK_CLI", "lark-cli")

# ---------- 正则 ----------
RE_TITLE_XML = re.compile(r"<title>(.*?)</title>", re.S)
RE_TITLE_MD = re.compile(r"^# (.+)$", re.M)
RE_WB_XML = re.compile(r"<whiteboard\b([^>]*?)(?:/>|>(.*?)</whiteboard>)", re.S)
RE_WB_MD = re.compile(r'<whiteboard\b[^>]*?\btoken="([^"]+)"[^>]*?>(?:</whiteboard>)?')
RE_FENCE = re.compile(r"```mermaid[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
RE_IMG = re.compile(r"!\[([^\]]*)\]\(\s*(?:<([^>]+)>|([^)\s]+))\s*\)")
RE_SOURCE = re.compile(r"<source\b([^>]*?)/?>", re.S)
RE_FIGURE_SOURCE = re.compile(r"<figure\b[^>]*>\s*<source\b([^>]*?)/?>\s*</figure>", re.S)

# 不本地化的块类型（保留原始标签，日志列清单）
OUT_OF_SCOPE_TAGS = [
    "bitable", "sheet", "mindnote", "synced_reference", "grid", "callout",
    "todo", "button", "date_reminder", "mention_doc", "mention_user", "add_ons", "isv",
]

IMG_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"]
SRC_EXTS = [".mmd", ".puml"]
BOARD_PNG_MAX_EDGE = 4096
RE_SVG_EXTERNAL_IMAGE = re.compile(
    r"<image\b[^>]*\b(?:href|xlink:href)=[\"']https?://[^\"']+[\"']", re.I)
RE_SVG_EXTERNAL_HREF = re.compile(
    r"(<image\b[^>]*\b(?:href|xlink:href)=[\"'])https?://[^\"']+([\"'])", re.I)


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


# ---------- lark-cli 调用 ----------
def run_lark(args: list[str], timeout: int = 180) -> dict:
    """调用 lark-cli 并解析 JSON 输出。"""
    try:
        p = subprocess.run([LARK] + args, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        die("找不到 lark-cli，请确认已安装并在 PATH 中（或用环境变量 LARK_CLI 指定）")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"message": "lark-cli 调用超时"}}
    out = (p.stdout or "").strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"ok": False,
                "error": {"message": f"lark-cli 输出无法解析：{out[:200]} / stderr：{(p.stderr or '')[:200]}"}}


def fetch_doc(doc: str, fmt: str) -> dict:
    r = run_lark(["docs", "+fetch", "--doc", doc, "--doc-format", fmt])
    if not r.get("ok"):
        err = json.dumps(r.get("error", r), ensure_ascii=False)
        die(f"读取文档失败（{fmt}）：{err}")
    return r["data"]["document"]


# ---------- 解析 ----------
def attr(attrs: str, name: str) -> str:
    m = re.search(name + r'="([^"]*)"', attrs)
    return html.unescape(m.group(1)) if m else ""


def parse_title(xml_content: str, md_content: str, doc_id: str) -> str:
    m = RE_TITLE_XML.search(xml_content)
    if m and m.group(1).strip():
        return html.unescape(m.group(1)).strip()
    m = RE_TITLE_MD.search(md_content)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return f"untitled-{doc_id[:8]}" if doc_id else "untitled"


def parse_boards(xml_content: str) -> list[dict]:
    """按文档顺序解析 XML 中的全部画板：idx（1 起）、token、type。"""
    boards = []
    for i, m in enumerate(RE_WB_XML.finditer(xml_content), start=1):
        attrs = m.group(1) or ""
        tok = attr(attrs, "token")
        if not tok:
            continue
        boards.append({"idx": i, "token": tok, "type": attr(attrs, "type") or "normal"})
    return boards


def parse_md_whiteboard_tags(md_content: str) -> list[dict]:
    """md 正文中的普通画板标签（mermaid 画板在 md 中已降级为代码块，不在此列）。"""
    return [{"span": m.span(), "token": m.group(1)} for m in RE_WB_MD.finditer(md_content)]


def parse_md_fences(md_content: str) -> list[dict]:
    """md 正文中的 ```mermaid 代码块（对应文档里的 mermaid 画板）。"""
    return [{"span": m.span(), "code": m.group(1)} for m in RE_FENCE.finditer(md_content)]


def parse_md_images(md_content: str) -> list[dict]:
    return [{"span": m.span(), "alt": m.group(1), "url": m.group(2) or m.group(3)}
            for m in RE_IMG.finditer(md_content)]


def parse_sources(text: str) -> list[dict]:
    """附件标签。优先整体吸收 <figure ...><source .../></figure> 包裹（替换时连壳一起去掉），
    兼容裸 <source .../> 形态。"""
    out = []
    covered: list[tuple[int, int]] = []
    for m in RE_FIGURE_SOURCE.finditer(text):
        attrs = m.group(1) or ""
        tok = attr(attrs, "token")
        if tok:
            out.append({"span": m.span(), "token": tok, "name": attr(attrs, "name")})
            covered.append(m.span())
    for m in RE_SOURCE.finditer(text):
        s, e = m.span()
        if any(s >= cs and e <= ce for cs, ce in covered):
            continue
        attrs = m.group(1) or ""
        tok = attr(attrs, "token")
        if tok:
            out.append({"span": (s, e), "token": tok, "name": attr(attrs, "name")})
    return out


def feishu_file_token(url: str) -> str | None:
    u = urllib.parse.urlparse(url)
    if u.hostname and u.hostname.endswith("feishu.cn") and u.path.startswith("/file/"):
        tok = u.path[len("/file/"):].strip("/")
        if re.fullmatch(r"[A-Za-z0-9_-]+", tok):
            return tok
    return None


# ---------- 命名与相对路径 ----------
def safe_filename(name: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip().strip(".").strip()
    return s or "untitled"


def rel_uri(assets_dirname: str, filename: str) -> str:
    s = f"{assets_dirname}/{filename}"
    return s.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def find_existing_glob(assets: Path, stem: str) -> Path | None:
    matches = sorted(assets.glob(stem + ".*"))
    return matches[0] if matches else None


def find_existing_exts(assets: Path, stem: str, exts: list[str]) -> Path | None:
    for ext in exts:
        p = assets / (stem + ext)
        if p.exists():
            return p
    return None


# ---------- 资源下载 ----------
def lark_media_download(token: str, dest_stem: Path, force: bool) -> tuple[Path | None, str | None]:
    """下载素材；dest_stem 不含扩展名时按 Content-Type 自动补全。"""
    args = ["docs", "+media-download", "--token", token, "--output", str(dest_stem)]
    if force:
        args.append("--overwrite")
    r = run_lark(args)
    if r.get("ok"):
        p = (r.get("data") or {}).get("saved_path")
        if p:
            return Path(p), None
    return None, (r.get("error") or {}).get("message", "未知错误")


def export_board_svg(token: str, dest_stem: Path, force: bool) -> tuple[Path | None, str | None]:
    """导出 SVG 矢量图（任意缩放不失真，替代低清 preview 位图）。"""
    # [AI-2026-10-09] preview 位图有分辨率上限（约 2560px），复杂画板缩放后文字看不清，改用矢量 svg
    target = f"{dest_stem}.svg"
    args = ["whiteboard", "+export", "--whiteboard-token", token,
            "--output-type", "svg", "--output", target]
    if force:
        args.append("--overwrite")
    r = run_lark(args)
    if r.get("ok"):
        p = (r.get("data") or {}).get("svg_path")
        if p:
            return Path(p), None
        g = Path(target)
        if g.exists():
            return g, None
    return None, (r.get("error") or {}).get("message", "未知错误")


def render_svg_png(svg_path: Path, png_path: Path) -> tuple[Path | None, str | None]:
    """通过 macOS Quick Look 渲染 SVG 为长边 4096px 的 PNG。"""
    # [AI-2026-10-09] 为多模态模型提供标准高清位图，SVG 仍作为可缩放归档源保留
    qlmanage = shutil.which("qlmanage")
    if qlmanage is None:
        return None, "找不到 macOS qlmanage，无法生成高清 PNG"
    try:
        with tempfile.TemporaryDirectory(prefix=".render-", dir=png_path.parent) as temp_dir:
            rendered_dir = Path(temp_dir)
            p = subprocess.run([qlmanage, "-t", "-s", str(BOARD_PNG_MAX_EDGE), "-o",
                                str(rendered_dir), str(svg_path)], capture_output=True, text=True, timeout=180)
            if p.returncode != 0:
                return None, f"qlmanage 渲染失败：{(p.stderr or p.stdout).strip()[:200]}"
            candidates = list(rendered_dir.glob(svg_path.name + ".png"))
            if not candidates:
                return None, "qlmanage 未生成 PNG 文件"
            shutil.copyfile(candidates[0], png_path)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"PNG 渲染失败：{exc}"
    return png_path, None


def svg_external_image_count(svg_path: Path) -> int:
    """返回 SVG 中未内联的外部图片数量；此类图片离线打开时会丢失。"""
    try:
        return len(RE_SVG_EXTERNAL_IMAGE.findall(svg_path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def inline_svg_external_images(svg_text: str, payloads: list[tuple[str, bytes]]) -> tuple[str, str | None]:
    """按 SVG <image> 出现顺序以 data URI 内联截图，返回（新 SVG，错误说明）。"""
    matches = list(RE_SVG_EXTERNAL_HREF.finditer(svg_text))
    if len(matches) != len(payloads):
        return svg_text, f"SVG 外部截图数={len(matches)}，raw 图片数={len(payloads)}，无法安全内联"
    data_iter = iter(payloads)

    def replace(match: re.Match[str]) -> str:
        mime, content = next(data_iter)
        encoded = base64.b64encode(content).decode("ascii")
        return f"{match.group(1)}data:{mime};base64,{encoded}{match.group(2)}"

    return RE_SVG_EXTERNAL_HREF.sub(replace, svg_text), None


def localize_svg_external_images(svg_path: Path, board_token: str, assets: Path) -> str | None:
    """用 raw 节点中的 image.token 下载截图并内联 SVG；失败时返回原因。"""
    raw = run_lark(["whiteboard", "+export", "--whiteboard-token", board_token,
                    "--output-type", "raw", "--as", "user"])
    if not raw.get("ok"):
        return (raw.get("error") or {}).get("message", "无法读取画板 raw 节点")
    nodes = (raw.get("data") or {}).get("nodes") or []
    images = sorted((node for node in nodes if isinstance(node.get("image"), dict)
                     and node["image"].get("token")), key=lambda node: node.get("z_index", 0))
    try:
        svg_text = svg_path.read_text(encoding="utf-8", errors="replace")
        payloads: list[tuple[str, bytes]] = []
        with tempfile.TemporaryDirectory(prefix=".inline-", dir=assets) as temp_dir:
            temp = Path(temp_dir)
            for i, node in enumerate(images, start=1):
                image, err = lark_media_download(node["image"]["token"], temp / f"image-{i}", True)
                if image is None:
                    return f"第{i}张截图下载失败：{err}"
                mime = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
                payloads.append((mime, image.read_bytes()))
        inlined, err = inline_svg_external_images(svg_text, payloads)
        if err:
            return err
        svg_path.write_text(inlined, encoding="utf-8")
    except OSError as exc:
        return f"SVG 截图内联失败：{exc}"
    return None


def export_board_preview(token: str, dest_stem: Path, force: bool) -> tuple[Path | None, str | None]:
    """导出低清预览位图（降级路径：SVG 导出失败时使用）。"""
    args = ["whiteboard", "+export", "--whiteboard-token", token,
            "--output-type", "preview", "--output", str(dest_stem)]
    if force:
        args.append("--overwrite")
    r = run_lark(args)
    if r.get("ok"):
        p = (r.get("data") or {}).get("preview_image_path")
        if p:
            return Path(p), None
    return None, (r.get("error") or {}).get("message", "未知错误")


def export_board_source(token: str, dest_stem: Path, force: bool) -> tuple[Path | None, str | None]:
    """返回 (源码文件路径 | None, 说明)。无源码可提属正常降级。"""
    args = ["whiteboard", "+export", "--whiteboard-token", token,
            "--output-type", "source", "--output", str(dest_stem)]
    if force:
        args.append("--overwrite")
    r = run_lark(args)
    if r.get("ok"):
        data = r.get("data") or {}
        if data.get("output_path"):
            return Path(data["output_path"]), None
        return None, data.get("msg", "无源码")
    return None, (r.get("error") or {}).get("message", "未知错误")


def ensure_image(token: str, assets: Path, force: bool) -> tuple[Path | None, str | None]:
    stem = f"img-{token[:8]}"
    if not force:
        e = find_existing_glob(assets, stem)
        if e:
            return e, None
    return lark_media_download(token, assets / stem, force)


def ensure_attachment(token: str, name: str, assets: Path, force: bool,
                      seen: set[str]) -> tuple[Path | None, str | None]:
    base = safe_filename(name) if name else f"attachment-{token[:8]}"
    stem, ext = os.path.splitext(base)
    target = base
    if target in seen:  # 同一次运行内同名不同附件
        target = f"{stem}-{token[:8]}{ext}"
    seen.add(target)
    p = assets / target
    if p.exists() and not force:
        return p, None
    return lark_media_download(token, p, force)


def ensure_board_files(board: dict, assets: Path, force: bool,
                       fails: list[str], notes: list[str]) -> tuple[Path | None, Path | None]:
    idx, tok, btype = board["idx"], board["token"], board["type"]
    stem = f"board-{idx:02d}-{tok[:6]}"
    # [AI-2026-10-09] 优先使用完整的 SVG 矢量图。飞书 SVG 若引用外部截图，
    # 离线查看会丢图，必须回退 preview 位图以保证画板内容完整。
    svg = find_existing_exts(assets, stem, [".svg"])
    err = None
    if force or svg is None:
        svg, err = export_board_svg(tok, assets / stem, force)
    external_images = svg_external_image_count(svg) if svg else 0
    inline_err = None
    if svg and external_images:
        inline_err = localize_svg_external_images(svg, tok, assets)
        if inline_err is None:
            notes.append(f"[画板] 第{idx}个 token={tok[:6]} 已内联 {external_images} 张截图，保留高清 SVG")
            external_images = 0
    if svg and external_images == 0:
        png = assets / f"{stem}.png"
        png_err = None
        if force or not png.exists():
            got, png_err = render_svg_png(svg, png)
            if got:
                png = got
            else:
                png = None
        img = png or svg
        if png is None:
            notes.append(f"[画板] 第{idx}个 token={tok[:6]} 高清 PNG 生成失败（{png_err}），"
                         "Markdown 已回退引用 SVG")
    else:
        img = find_existing_exts(assets, stem, [".jpg", ".jpeg", ".png", ".webp"])
        if force or img is None:
            got, err2 = export_board_preview(tok, assets / stem, force)
            if got:
                img = got
            else:
                fails.append(f"[画板] 第{idx}个 token={tok[:6]}（{btype}）导出失败："
                             f"SVG：{err or inline_err or '含外部截图'}；preview：{err2}")
                img = None
        if img and external_images:
            notes.append(f"[画板] 第{idx}个 token={tok[:6]} SVG 截图内联失败（{inline_err}），"
                         "已改用 preview 位图以保留完整内容")
        elif img and not svg:
            notes.append(f"[画板] 第{idx}个 token={tok[:6]} SVG 导出失败（{err}），已降级为预览图")
    src = find_existing_exts(assets, stem, SRC_EXTS)
    if force or src is None:
        got, note = export_board_source(tok, assets / stem, force)
        if got:
            src = got
        elif note and "no code blocks found" not in note:
            notes.append(f"[画板] 第{idx}个 token={tok[:6]} 源码提取异常：{note}")
    return img, src


# ---------- 外链图下载（含安全校验） ----------
def is_safe_public_https(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return False
    try:
        infos = socket.getaddrinfo(u.hostname, 443)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            return False
    return True


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_safe_public_https(newurl):
            raise urllib.error.URLError(f"重定向目标未通过安全校验：{newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def guess_ext(url: str, ctype: str) -> str:
    for key, ext in (("image/png", ".png"), ("image/jpeg", ".jpg"), ("image/gif", ".gif"),
                     ("image/webp", ".webp"), ("image/svg+xml", ".svg"), ("image/bmp", ".bmp")):
        if ctype.startswith(key):
            return ext
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    return suffix if suffix else ".bin"


def ensure_external(url: str, assets: Path, force: bool) -> tuple[Path | None, str | None]:
    stem = "ext-" + hashlib.sha1(url.encode()).hexdigest()[:8]
    if not force:
        e = find_existing_glob(assets, stem)
        if e:
            return e, None
    if not is_safe_public_https(url):
        return None, "外链未通过安全校验（仅允许 https 公网地址）"
    try:
        opener = urllib.request.build_opener(SafeRedirect())
        req = urllib.request.Request(url, headers={"User-Agent": "lark-md-export/1.0"})
        with opener.open(req, timeout=60) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "") or ""
    except Exception as e:  # noqa: BLE001 网络下载失败统一降级
        return None, f"下载失败：{e}"
    p = assets / (stem + guess_ext(url, ctype))
    p.write_bytes(data)
    return p, None


# ---------- md 重写 ----------
def fence_block(code: str, lang: str) -> str:
    return f"```{lang}\n{code.strip(chr(10))}\n```"


def board_md_block(img: Path, src: Path | None, assets_dirname: str,
                   fallback_code: str | None = None) -> str:
    out = f"![]({rel_uri(assets_dirname, img.name)})"
    code, lang = None, None
    if src is not None and src.exists():
        code = src.read_text(encoding="utf-8", errors="replace")
        lang = "plantuml" if src.suffix == ".puml" else "mermaid"
    elif fallback_code is not None:
        code, lang = fallback_code, "mermaid"
    if code:
        out += ("\n\n<details>\n<summary>画板源码（" + lang + "）</summary>\n\n"
                + fence_block(code, lang) + "\n\n</details>")
    return out


def apply_spans(text: str, spans: list[dict]) -> str:
    """应用 {(start,end): repl} 替换；外层 span 优先、被包含的 span 丢弃；倒序应用。"""
    valid = [s for s in spans if s.get("repl") is not None]
    valid.sort(key=lambda x: (x["span"][0], -(x["span"][1] - x["span"][0])))
    chosen, last_end = [], -1
    for s in valid:
        if s["span"][0] < last_end:
            continue
        chosen.append(s)
        last_end = s["span"][1]
    for s in sorted(chosen, key=lambda x: x["span"][0], reverse=True):
        st, en = s["span"]
        text = text[:st] + s["repl"] + text[en:]
    return text


# ---------- 增量 ----------
def read_existing_revision(md_path: Path) -> int | None:
    if not md_path.exists():
        return None
    head = md_path.read_text(encoding="utf-8", errors="ignore")[:400]
    m = re.search(r"revision: (\d+)", head)
    return int(m.group(1)) if m else None


def latest_exported_revision(outdir: Path, safe_title: str) -> int | None:
    """扫描 {标题}_v{MMdd}.md 全部历史版本文件，返回其中最大的 revision。"""
    # [AI-2026-10-09] 版本化命名后，增量跳过判断改为对比全量历史中的最新 revision
    best: int | None = None
    pat = re.compile(rf"^{re.escape(safe_title)}_v\d{{4}}\.md$")
    for p in outdir.iterdir():
        if not p.is_file() or not pat.match(p.name):
            continue
        r = read_existing_revision(p)
        if r is not None:
            best = r if best is None else max(best, r)
    return best


# ---------- 日志 ----------
def write_log(outdir: Path, stem: str, doc_arg: str, revision: int,
              exported_at: str, fails: list[str], notes: list[str], oos: dict) -> Path:
    lines = [
        f"# 导出日志：{stem}", "",
        f"- 来源: {doc_arg}",
        f"- revision: {revision}",
        f"- 导出时间: {exported_at}", "",
        "## 失败与降级", "",
    ]
    lines += [f"- {x}" for x in fails] if fails else ["- 无"]
    lines += ["", "## 提示", ""]
    lines += [f"- {x}" for x in notes] if notes else ["- 无"]
    lines += ["", "## 未本地化的块类型（原始标签已保留在 md 中）", ""]
    lines += [f"- <{tag}> × {n}" for tag, n in oos.items()] if oos else ["- 无"]
    p = outdir / f"{stem}.export-log.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------- 主流程 ----------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="把飞书云文档（docx/wiki）导出为本地自包含 Markdown 资源包（数据经 lark-cli 获取）")
    ap.add_argument("doc", help="文档 URL 或 token（支持 /docx/、/wiki/）")
    ap.add_argument("-o", "--output-dir", default=None,
                    help="导出根目录；默认 = 运行脚本时所在的目录（cwd）")
    ap.add_argument("--force", action="store_true",
                    help="忽略增量与 revision 判定，强制重新导出（覆盖已存在文件）")
    args = ap.parse_args()

    outdir = Path(args.output_dir).expanduser().resolve() if args.output_dir else Path.cwd()
    outdir.mkdir(parents=True, exist_ok=True)

    print("[1/5] 读取文档（markdown）…")
    md_doc = fetch_doc(args.doc, "markdown")
    print("[2/5] 读取文档（xml）…")
    xml_doc = fetch_doc(args.doc, "xml")

    md_content = md_doc.get("content", "")
    xml_content = xml_doc.get("content", "")
    doc_id = md_doc.get("document_id", "")
    revision = int(md_doc.get("revision_id", -1))

    title = parse_title(xml_content, md_content, doc_id)
    safe_title = safe_filename(title)
    # [AI-2026-10-09] 版本化命名：md / assets / 日志统一使用 {标题}_v{MMdd} 前缀，
    # 跨天导出生成新版本文件并完整保留历史版本，避免互相覆盖
    ver = datetime.datetime.now().strftime("%m%d")
    stem = f"{safe_title}_v{ver}"
    md_path = outdir / f"{stem}.md"

    prev = latest_exported_revision(outdir, safe_title)
    if not args.force and prev is not None and prev == revision:
        print(f"已是最新（历史版本最新 revision={revision}），跳过。使用 --force 强制重导。")
        return 0

    assets = outdir / f"{stem}.assets"
    assets.mkdir(exist_ok=True)
    fails: list[str] = []
    notes: list[str] = []
    exported_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")

    # 画板资源（无论普通还是 mermaid，统一先导出）
    print("[3/5] 导出画板（svg + 高清 png + source）…")
    boards = parse_boards(xml_content)
    board_assets: dict[str, tuple[Path | None, Path | None]] = {}
    for b in boards:
        board_assets[b["token"]] = ensure_board_files(b, assets, args.force, fails, notes)
    board_by_token = {b["token"]: b for b in boards}

    spans: list[dict] = []

    # 图片
    print("[4/5] 下载图片与附件…")
    for im in parse_md_images(md_content):
        tok = feishu_file_token(im["url"])
        if tok:
            p, err = ensure_image(tok, assets, args.force)
        else:
            p, err = ensure_external(im["url"], assets, args.force)
        if p:
            spans.append({"span": im["span"], "repl": f"![{im['alt']}]({rel_uri(assets.name, p.name)})"})
        else:
            fails.append(f"[图片] {im['url'][:80]} 下载失败：{err}（保留原引用）")

    # 普通画板（md 中的 <whiteboard> 标签）
    for mb in parse_md_whiteboard_tags(md_content):
        b = board_by_token.get(mb["token"])
        if not b:
            fails.append(f"[画板] md 中的画板 token={mb['token'][:8]} 未在 XML 清单中找到（保留原标签）")
            continue
        img, src = board_assets.get(mb["token"], (None, None))
        if img:
            spans.append({"span": mb["span"], "repl": board_md_block(img, src, assets.name)})
        else:
            fails.append(f"[画板] token={mb['token'][:6]} 画板图不可用（保留原标签）")

    # mermaid 画板：md 中的 ```mermaid 代码块与 XML 中 type=mermaid 画板按序对齐
    fences = parse_md_fences(md_content)
    mboards = [b for b in boards if b["type"] == "mermaid"]
    if fences and mboards:
        if len(fences) == len(mboards):
            for f, b in zip(fences, mboards):
                img, src = board_assets.get(b["token"], (None, None))
                if img:
                    spans.append({"span": f["span"],
                                  "repl": board_md_block(img, src, assets.name, fallback_code=f["code"])})
                else:
                    notes.append(f"[画板] mermaid 画板 token={b['token'][:6]} 画板图不可用（保留原代码块）")
        else:
            notes.append(f"[画板] md 中 mermaid 代码块数（{len(fences)}）与 XML 中 mermaid 画板数"
                         f"（{len(mboards)}）不一致，全部保留原代码块")
    elif fences:
        notes.append(f"[画板] md 中有 {len(fences)} 个 mermaid 代码块，但 XML 中未发现 mermaid 画板，保留原样")

    # 附件（md 中的 <source> 标签；XML 中额外出现但 md 未呈现的也一并下载）
    seen_names: set[str] = set()
    handled: set[str] = set()
    for sx in parse_sources(md_content):
        p, err = ensure_attachment(sx["token"], sx["name"], assets, args.force, seen_names)
        if p:
            spans.append({"span": sx["span"], "repl": f"[{p.name}]({rel_uri(assets.name, p.name)})"})
            handled.add(sx["token"])
        else:
            fails.append(f"[附件] {sx['name'] or sx['token'][:8]} 下载失败：{err}（保留原标签）")
    for sx in parse_sources(xml_content):
        if sx["token"] in handled:
            continue
        p, err = ensure_attachment(sx["token"], sx["name"], assets, args.force, seen_names)
        if p:
            notes.append(f"[附件] {p.name} 已下载，但 md 中未定位到引用位（md 中附件形态可能与预期不同）")
        else:
            fails.append(f"[附件] {sx['name'] or sx['token'][:8]} 下载失败：{err}")

    # 替换 + 落盘
    print("[5/5] 重写引用并落盘…")
    new_content = apply_spans(md_content, spans)
    header = f"<!-- source: {args.doc} | revision: {revision} | exported: {exported_at} -->\n\n"
    md_path.write_text(header + new_content, encoding="utf-8")

    oos = {}
    for tag in OUT_OF_SCOPE_TAGS:
        n = len(re.findall(r"<" + tag + r"(?=[\s/>])", xml_content))
        if n:
            oos[tag] = n
    log_path = write_log(outdir, stem, args.doc, revision, exported_at, fails, notes, oos)

    n_assets = len([p for p in assets.iterdir() if p.is_file()])
    print(f"完成：{md_path}")
    print(f"资源目录：{assets}（{n_assets} 个文件）")
    print(f"日志：{log_path}")
    if fails:
        print(f"注意：有 {len(fails)} 项失败/降级，详见日志。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
