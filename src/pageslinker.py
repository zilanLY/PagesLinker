"""PagesLinker core engine.

Pure-Python, dependency-free. Builds a Cloudflare Pages static site from
arbitrary local resources:

- small files are copied verbatim (direct links)
- large files are split into <25MiB chunks; a self-contained HTML page
  reassembles them in the browser (that page's URL is the resource link)
- manifest.json + index.html provide a browsable link directory

No Functions, no R2, no server logic, no network I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import shutil
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

CHUNK_DIR = "__chunks__"
DEFAULT_THRESHOLD = 20 * 1024 * 1024  # 20 MiB
MAX_ASSET_BYTES = 25 * 1024 * 1024    # Cloudflare Pages hard cap
MAX_FILES = 20000                     # Cloudflare Pages per-deploy cap
COPY_BUFFER = 8 * 1024 * 1024         # 8 MiB streaming buffer

MI_TO_BYTES = 1024 * 1024


class PagesLinkerError(Exception):
    pass


# --------------------------------------------------------------------------
# Status / reporting
# --------------------------------------------------------------------------

@dataclass
class StatusEvent:
    kind: str            # 'list' | 'start' | 'file' | 'log' | 'done' | 'error'
    path: str = ""
    total: int = 0
    index: int = 0
    message: str = ""


# --------------------------------------------------------------------------
# Manifest entry
# --------------------------------------------------------------------------

@dataclass
class Entry:
    original_path: str       # user-facing original relative path (for display)
    url_path: str            # site-relative URL path, percent-encoded
    bytes: int
    sha256: str
    kind: str                # 'direct' | 'chunked'
    chunks: int = 0
    mime: str = "application/octet-stream"
    display_name: str = ""   # original file name


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def human_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _sha256_stream(path: Path, emit: Optional[Callable[[int, int], None]] = None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(COPY_BUFFER)
            if not block:
                break
            h.update(block)
            done += len(block)
            if emit:
                emit(done, total)
    return h.hexdigest()


def _safe_url_part(name: str) -> str:
    """Make a single path segment URL-safe without breaking UTF-8 names."""
    if not name:
        return "_"
    out = []
    for ch in name:
        o = ord(ch)
        # Keep letters/digits and a conservative punctuation set; UTF-8 name chars OK.
        if ch.isalnum() or ch in "-._~":
            out.append(ch)
        elif o < 128:
            out.append("_")
        else:
            out.append(ch)  # non-ASCII (CJK etc.) is kept, then percent-encoded later
    # Guard against a fully blank name and Windows-reserved-ish dot-only names
    cleaned = "".join(out).strip(". ")
    if not cleaned:
        cleaned = "_"
    return cleaned


def _percent_encode_url_path(rel: str) -> str:
    """Percent-encode a POSIX relative path for use in a URL, keeping '/'."""
    parts = rel.split("/")
    enc = []
    for p in parts:
        enc.append(_quote_segment(p))
    return "/".join(enc)


def _quote_segment(seg: str) -> str:
    from urllib.parse import quote
    return quote(seg, safe="-._~")


def _dedupe_name(path: str, used: set[str]) -> str:
    if path not in used:
        used.add(path)
        return path
    stem, dot, ext = path.rpartition(".")
    i = 1
    while True:
        candidate = f"{stem}_{i}" + (dot + ext if dot else "")
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


# --------------------------------------------------------------------------
# Build context
# --------------------------------------------------------------------------

@dataclass
class BuildResult:
    site_dir: Path
    zip_path: Path
    entries: list[Entry]
    total_files: int
    direct_count: int
    chunked_count: int
    chunk_count: int
    total_bytes: int


@dataclass
class _Item:
    src: Path
    rel: str            # original relative path (POSIX, used for naming)
    used_rel: str       # possibly deduped/sanitized site path


# --------------------------------------------------------------------------
# Core build function
# --------------------------------------------------------------------------

def build_site(
    inputs: list[str],
    output_dir: str,
    *,
    threshold_mb: float = 20.0,
    dedupe: bool = True,
    skip_hidden: bool = True,
    force_ascii: bool = False,
    emit: Optional[Callable[[StatusEvent], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> BuildResult:
    """Build a static Pages site from the given inputs."""
    threshold = int(threshold_mb * MI_TO_BYTES)
    if threshold < 1 * MI_TO_BYTES or threshold > 24 * MI_TO_BYTES:
        raise PagesLinkerError("分片阈值必须在 1–24 MiB 之间")

    out = Path(output_dir)
    site = out / "_site"

    def log(msg: str) -> None:
        if emit:
            emit(StatusEvent(kind="log", message=msg))

    def check_cancel() -> None:
        if cancel and cancel():
            raise PagesLinkerError("已取消")

    # --- Collect input roots -------------------------------------------------
    roots: list[tuple[str, Path]] = []  # (name, path)
    for inp in inputs:
        p = Path(inp)
        if not p.exists():
            raise PagesLinkerError(f"输入不存在: {inp}")
        if p.is_dir():
            roots.append((p.name, p))
        elif p.suffix.lower() == ".zip":
            roots.append((p.name, p))
        else:
            roots.append((p.name, p))

    # --- Enumerate source items ---------------------------------------------
    items: list[_Item] = []
    tmp_extract: Optional[Path] = None
    try:
        seen_paths: set[str] = set()
        for name, p in roots:
            check_cancel()
            if p.is_file() and p.suffix.lower() == ".zip":
                tmp_extract = out / ".extract" / (name + "_unpacked")
                if tmp_extract.exists():
                    shutil.rmtree(tmp_extract, ignore_errors=True)
                tmp_extract.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(p) as zf:
                    zf.extractall(tmp_extract)
                _collect_dir(tmp_extract, "", items, skip_hidden, check_cancel)
            elif p.is_dir():
                _collect_dir(p, "", items, skip_hidden, check_cancel)
            else:
                items.append(_Item(src=p, rel=p.name, used_rel=p.name))

        if not items:
            raise PagesLinkerError("未找到任何资源文件")

        log(f"共发现 {len(items)} 个资源文件")

        # --- Build site file layout -----------------------------------------
        if site.exists():
            shutil.rmtree(site, ignore_errors=True)
        site.mkdir(parents=True, exist_ok=True)

        used_names: set[str] = set()
        entries: list[Entry] = []
        content_sha: dict[str, str] = {}   # sha256 -> site-relative chunk dir
        file_count = 0
        chunk_count = 0
        total_bytes = 0
        direct_count = 0
        chunked_count = 0

        for i, item in enumerate(items):
            check_cancel()
            if emit:
                emit(StatusEvent(kind="file", path=item.rel, index=i, total=len(items)))

            size = item.src.stat().st_size

            # Sanitize / dedupe path
            if force_ascii:
                parts = [_safe_url_part(x) for x in item.rel.split("/")]
            else:
                parts = []
                for x in item.rel.split("/"):
                    parts.append(_safe_url_part(x))
            rel_clean = "/".join(parts)
            rel_clean = _dedupe_name(rel_clean, used_names)

            # Hash (streaming)
            sha = _sha256_stream(item.src)
            log(f"哈希: {item.rel} -> {sha[:16]}…")

            mime, _enc = mimetypes.guess_type(item.src.name)
            if not mime:
                mime = "application/octet-stream"

            if size <= threshold:
                # Direct copy (with content dedupe: identical sha reuses the copy)
                if dedupe and sha in content_sha:
                    rel_clean = content_sha[sha]
                    log(f"去重命中: {item.rel} -> {rel_clean}")
                    file_count += 0
                else:
                    dest = site / rel_clean
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item.src, dest)
                    if dedupe:
                        content_sha[sha] = rel_clean
                    file_count += 1
                url = _percent_encode_url_path(rel_clean)
                entries.append(Entry(
                    original_path=item.rel,
                    url_path=url,
                    bytes=size,
                    sha256=sha,
                    kind="direct",
                    mime=mime,
                    display_name=item.src.name,
                ))
                direct_count += 1
                total_bytes += size
            else:
                # Chunked (dedupe: same sha reuses the same chunk directory)
                chunk_rel_dir = f"{CHUNK_DIR}/{sha[:16]}"
                if dedupe and sha in content_sha:
                    log(f"去重命中(分片): {item.rel} -> {content_sha[sha]}")
                    n_chunks = math.ceil(size / threshold)
                    already_split = True
                else:
                    chunk_dir = site / chunk_rel_dir
                    chunk_dir.mkdir(parents=True, exist_ok=True)
                    n_chunks = math.ceil(size / threshold)
                    _split_file(item.src, chunk_dir, threshold, sha, log, check_cancel)
                    if dedupe:
                        content_sha[sha] = chunk_rel_dir
                    already_split = False
                    chunk_count += n_chunks
                # Build reassembly page
                page_rel = rel_clean + ".html"
                page_rel = _dedupe_name(page_rel, used_names)
                page_url = _percent_encode_url_path(page_rel)
                # Compute chunk URLs relative to the page's directory
                page_dir_depth = page_rel.count("/")
                up = "/".join([".."] * page_dir_depth) if page_dir_depth else "."
                chunk_urls = [
                    f"{up}/{chunk_rel_dir}/{_chunk_name(k)}"
                    for k in range(n_chunks)
                ]
                page_html = render_reassembly_page(
                    display_name=item.src.name,
                    mime=mime,
                    total_bytes=size,
                    total_sha256=sha,
                    chunk_urls=chunk_urls,
                    chunk_count=n_chunks,
                )
                (site / page_rel).parent.mkdir(parents=True, exist_ok=True)
                (site / page_rel).write_text(page_html, encoding="utf-8")
                entries.append(Entry(
                    original_path=item.rel,
                    url_path=page_url,
                    bytes=size,
                    sha256=sha,
                    kind="chunked",
                    chunks=n_chunks,
                    mime=mime,
                    display_name=item.src.name,
                ))
                chunked_count += 1
                total_bytes += size
                file_count += 1 + (0 if already_split else n_chunks)  # page (+ chunks if new)

        # --- Write manifest / index / README ---------------------------------
        manifest_path = site / "manifest.json"
        manifest = [e.__dict__ for e in entries]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        file_count += 1

        index_html = render_index_page()
        (site / "index.html").write_text(index_html, encoding="utf-8")
        file_count += 1

        readme_txt = render_readme()
        (site / "README.txt").write_text(readme_txt, encoding="utf-8")
        file_count += 1

        log(f"文件总数(含分片/索引): {file_count}")

        # --- Cap check -------------------------------------------------------
        if file_count > MAX_FILES:
            raise PagesLinkerError(
                f"站点文件数 {file_count} 超过 Cloudflare Pages 单次部署上限 {MAX_FILES}，"
                f"请分批处理或提高分片阈值"
            )

        # --- Zip -------------------------------------------------------------
        zip_path = out / "site.zip"
        if zip_path.exists():
            zip_path.unlink()
        log("正在打包 zip…")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for root, _dirs, files in os.walk(site):
                for fn in files:
                    fp = Path(root) / fn
                    rel = fp.relative_to(site).as_posix()
                    zf.write(fp, rel)

        result = BuildResult(
            site_dir=site,
            zip_path=zip_path,
            entries=entries,
            total_files=file_count,
            direct_count=direct_count,
            chunked_count=chunked_count,
            chunk_count=chunk_count,
            total_bytes=total_bytes,
        )
        if emit:
            emit(StatusEvent(kind="done", message="完成"))
        return result
    finally:
        if tmp_extract and tmp_extract.exists():
            shutil.rmtree(tmp_extract, ignore_errors=True)


def _collect_dir(root: Path, rel_prefix: str, items: list[_Item], skip_hidden: bool,
                 check_cancel: Callable[[], None]) -> None:
    for child in sorted(root.iterdir()):
        check_cancel()
        name = child.name
        if skip_hidden and (name.startswith(".") or name in ("Thumbs.db", "desktop.ini", "Desktop.ini")):
            continue
        rel = f"{rel_prefix}/{name}" if rel_prefix else name
        if child.is_dir():
            _collect_dir(child, rel, items, skip_hidden, check_cancel)
        elif child.is_file():
            items.append(_Item(src=child, rel=rel, used_rel=rel))


def _chunk_name(k: int) -> str:
    return f"{k:06d}.bin"


def _split_file(src: Path, chunk_dir: Path, threshold: int, sha: str,
                log: Callable[[str], None], check_cancel: Callable[[], None]) -> None:
    idx = 0
    with open(src, "rb") as f:
        while True:
            check_cancel()
            buf = f.read(threshold)
            if not buf:
                break
            part = chunk_dir / _chunk_name(idx)
            with open(part, "wb") as w:
                w.write(buf)
            idx += 1
    log(f"分片: {src.name} -> {idx} 片 ({sha[:16]}…)")


# --------------------------------------------------------------------------
# HTML templates
# --------------------------------------------------------------------------

_HTML_ESCAPES = {
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}


def _html_escape(s: str) -> str:
    return "".join(_HTML_ESCAPES.get(c, c) for c in s)


def render_reassembly_page(display_name: str, mime: str, total_bytes: int,
                           total_sha256: str, chunk_urls: list[str],
                           chunk_count: int) -> str:
    """Render the client-side reassembly page.

    The template is a plain string (NOT an f-string) with placeholders
    @@NAME@@ / @@NAME_JSON@@ / @@MIME@@ / @@BYTES@@ / @@SHA@@ / @@CHUNKS@@ /
    @@COUNT@@ / @@SIZE_H@@ / @@TITLE@@, injected via .replace() so the JS braces
    never fight Python format specifiers.
    """
    name_html = _html_escape(display_name)
    name_json = json.dumps(display_name, ensure_ascii=False)
    mime_json = json.dumps(mime, ensure_ascii=False)
    urls_json = json.dumps(chunk_urls, ensure_ascii=False)
    size_h = human_size(total_bytes)
    page = _REASSEMBLY_TEMPLATE
    for ph, val in (
        ("@@NAME@@", name_html),
        ("@@NAME_JSON@@", name_json),
        ("@@MIME@@", mime_json),
        ("@@BYTES@@", str(total_bytes)),
        ("@@SHA@@", total_sha256),
        ("@@CHUNKS@@", urls_json),
        ("@@COUNT@@", str(chunk_count)),
        ("@@SIZE_H@@", size_h),
    ):
        page = page.replace(ph, val)
    assert "@@" not in page, "unresolved placeholder in reassembly page"
    return page


_REASSEMBLY_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>@@NAME@@ · 分片重组</title>
<style>
:root{--bg:#0f1115;--fg:#e6e8ec;--dim:#9aa0a8;--accent:#4f8cff;--card:#181b21;--ok:#2ecc71;--bad:#ff5f56;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:20px;word-break:break-all;margin:0 0 6px}
.meta{color:var(--dim);font-size:13px;line-height:1.7}
.card{background:var(--card);border-radius:12px;padding:20px;margin:20px 0}
.bar{height:10px;background:#262a33;border-radius:6px;overflow:hidden}
.bar>div{height:100%;width:0;background:var(--accent);transition:width .2s}
button{background:var(--accent);border:0;color:#fff;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px;margin-right:8px}
button:disabled{opacity:.5;cursor:not-allowed}
.btn-a{display:inline-block;background:var(--accent);color:#fff;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:14px;margin-right:8px;text-decoration:none}
.tag-ok{display:inline-block;padding:8px 14px;border-radius:8px;font-size:13px;background:#1d2f24;color:var(--ok)}
.status{font-size:13px;color:var(--dim);margin-top:12px}
.ok{color:var(--ok)}.bad{color:var(--bad)}
#preview img,#preview video,#preview audio{max-width:100%;border-radius:8px}
#preview iframe{width:100%;height:480px;border:0;border-radius:8px;background:#fff}
a{color:var(--accent)}
</style>
</head>
<body>
<div class="wrap">
  <h1>@@NAME@@</h1>
  <div class="meta">大小 @@SIZE_H@@ · 分片 @@COUNT@@ 片 · SHA-256 <span id="shashow" class="ok" style="font-size:12px">@@SHA@@</span></div>
  <div class="card">
    <div class="bar"><div id="bar"></div></div>
    <div class="status" id="status">正在准备分片…</div>
  </div>
  <div class="card" style="display:none" id="out">
    <a id="dl" class="btn-a" download="">下载文件</a>
    <span id="dlc" class="tag-ok" hidden>正在校验…</span>
    <div class="status" id="verify"></div>
  </div>
  <div id="preview"></div>
  <p class="meta">提示：本页需通过 http(s) 访问（部署后或本地静态服务器）；直接用 file:// 打开无法加载分片。</p>
</div>
<script>
"use strict";
const META = {
  name: @@NAME_JSON@@,
  mime: @@MIME@@,
  bytes: @@BYTES@@,
  sha256: "@@SHA@@",
  chunks: @@CHUNKS@@,
};
const bar = document.getElementById('bar');
const status = document.getElementById('status');
const out = document.getElementById('out');
const dl = document.getElementById('dl');
const dlc = document.getElementById('dlc');
const verifyEl = document.getElementById('verify');
const preview = document.getElementById('preview');

const results = new Array(META.chunks.length);

function setPct(pct) {
  bar.style.width = Math.min(100, Math.max(0, pct)).toFixed(2) + '%';
}

async function fetchOne(i) {
  const res = await fetch(META.chunks[i]);
  if (!res.ok) throw new Error('HTTP ' + res.status);
  if (!res.body) {
    const ab = await res.arrayBuffer();
    return {index: i, data: new Uint8Array(ab)};
  }
  const reader = res.body.getReader();
  const chunks = [];
  let got = 0;
  const len = Number(res.headers.get('Content-Length') || 0);
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    chunks.push(value);
    got += value.length;
    const overall = (i / META.chunks.length) + (got / (len || META.bytes)) / META.chunks.length;
    setPct(overall * 100);
  }
  const buf = new Uint8Array(got);
  let off = 0;
  for (const c of chunks) { buf.set(c, off); off += c.length; }
  return {index: i, data: buf};
}

async function sha256Hex(buf) {
  const digest = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2,'0')).join('');
}

async function downloadAll() {
  try {
    status.textContent = '正在拉取 ' + META.chunks.length + ' 个分片…';
    const workers = [];
    for (let i = 0; i < META.chunks.length; i++) workers.push(fetchOne(i));
    const settled = await Promise.allSettled(workers);
    settled.forEach((r, i) => { if (r.status === 'fulfilled') results[r.value.index] = r.value.data; });
    const failed = [];
    settled.forEach((r, i) => { if (r.status === 'rejected') failed.push(i); });
    if (failed.length) {
      status.textContent = '失败分片：' + failed.join(', ') + '（可重试）';
      setPct(0);
      return;
    }
    const total = results.reduce((s, d) => s + d.length, 0);
    const merged = new Uint8Array(total);
    let off = 0;
    for (const d of results) { merged.set(d, off); off += d.length; }
    setPct(100);
    status.textContent = '拼接完成，正在校验 SHA-256…';

    const hash = await sha256Hex(merged);
    const ok = hash === META.sha256;
    verifyEl.textContent = ok ? '✓ SHA-256 校验通过' : '✗ SHA-256 校验失败（实际 ' + hash + '）';
    verifyEl.className = 'status ' + (ok ? 'ok' : 'bad');

    const blob = new Blob([merged], {type: META.mime});
    const url = URL.createObjectURL(blob);
    dl.href = url;
    dl.download = META.name;
    dlc.textContent = ok ? '校验通过 ✓' : '校验失败（仍可下载）';
    dlc.hidden = false;
    out.style.display = 'block';
    renderPreview(blob, url);
    status.textContent = '完成';
  } catch (e) {
    status.textContent = '错误：' + e.message;
  }
}

function renderPreview(blob, url) {
  const m = META.mime || '';
  preview.innerHTML = '';
  if (m.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = url; img.alt = META.name;
    preview.appendChild(img);
  } else if (m.startsWith('video/')) {
    const v = document.createElement('video');
    v.src = url; v.controls = true;
    preview.appendChild(v);
  } else if (m.startsWith('audio/')) {
    const a = document.createElement('audio');
    a.src = url; a.controls = true;
    preview.appendChild(a);
  } else if (m === 'application/pdf') {
    const f = document.createElement('iframe');
    f.src = url;
    preview.appendChild(f);
  }
}

downloadAll();
</script>
</body>
</html>
"""


def render_index_page() -> str:
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>资源直链索引</title>
<style>
:root{--bg:#0f1115;--fg:#e6e8ec;--dim:#9aa0a8;--accent:#4f8cff;--card:#181b21}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--fg)}
.wrap{max-width:960px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:18px;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:18px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}
input[type=text]{flex:1;min-width:220px;background:#12141a;border:1px solid #2a2e38;color:var(--fg);border-radius:8px;padding:10px 12px}
button{background:var(--accent);border:0;color:#fff;padding:10px 14px;border-radius:8px;cursor:pointer;font-size:13px}
button.ghost{background:transparent;border:1px solid #2a2e38;color:var(--dim)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-bottom:1px solid #23262e;font-size:13px;vertical-align:top}
th{color:var(--dim);font-weight:500}
td.name{word-break:break-all}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;background:#23262e;color:var(--dim)}
.tag.chunked{background:#1d2b3a;color:#7fb4ff}
a{color:var(--accent);text-decoration:none}
.copy{cursor:pointer}
.empty{color:var(--dim);padding:40px 0;text-align:center}
#allcopy{margin-left:auto}
</style>
</head>
<body>
<div class="wrap">
  <h1>资源直链索引</h1>
  <div class="sub">由 PagesLinker 生成 · 纯静态 · 大文件分片在浏览器内重组</div>
  <div class="row">
    <input type="text" id="domain" placeholder="部署域名（可选，如 https://xxx.pages.dev）">
    <input type="text" id="q" placeholder="搜索名称…">
    <button class="ghost" id="allcopy">复制全部直链</button>
  </div>
  <table>
    <thead><tr><th>名称</th><th>大小</th><th>类型</th><th>直链</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="empty" id="empty" style="display:none">无匹配结果</div>
</div>
<script>
"use strict";
const rows = document.getElementById('rows');
const empty = document.getElementById('empty');
const q = document.getElementById('q');
const domain = document.getElementById('domain');
const allcopy = document.getElementById('allcopy');
let data = [];
const savedDomain = localStorage.getItem('pl_domain') || '';
domain.value = savedDomain;
domain.addEventListener('input', () => { localStorage.setItem('pl_domain', domain.value.trim()); render(); });

function base() { return domain.value.trim().replace(/\/+$/, ''); }
function full(u) { const b = base(); return b ? b + '/' + u : u; }

async function load() {
  const res = await fetch('manifest.json');
  data = await res.json();
  render();
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function hs(n){if(n<1024)return n+' B';const u=['KiB','MiB','GiB','TiB'];let v=n,i=0;while(v>=1024&&i<u.length-1){v/=1024;i++;}return v.toFixed(1)+' '+u[i];}
function render() {
  const kw = q.value.trim().toLowerCase();
  rows.innerHTML = '';
  const filtered = data.filter(e => !kw || e.display_name.toLowerCase().includes(kw) || e.original_path.toLowerCase().includes(kw));
  empty.style.display = filtered.length ? 'none' : 'block';
  for (const e of filtered) {
    const tr = document.createElement('tr');
    const u = full(e.url_path);
    const kindTag = e.kind === 'chunked' ? '<span class="tag chunked">分片 ×'+e.chunks+'</span>' : '<span class="tag">直接</span>';
    tr.innerHTML =
      '<td class="name">'+esc(e.display_name)+'</td>'+
      '<td>'+hs(e.bytes)+'</td>'+
      '<td>'+kindTag+'</td>'+
      '<td><a href="'+esc(u)+'" target="_blank" rel="noopener">'+esc(u)+'</a> <button class="copy" data-u="'+esc(u)+'">复制</button></td>';
    rows.appendChild(tr);
  }
  rows.querySelectorAll('.copy').forEach(b => b.addEventListener('click', () => {
    navigator.clipboard.writeText(b.dataset.u).then(()=>{b.textContent='已复制';setTimeout(()=>b.textContent='复制',1200);});
  }));
}
q.addEventListener('input', render);
allcopy.addEventListener('click', () => {
  const lines = data.map(e => full(e.url_path));
  navigator.clipboard.writeText(lines.join('\n')).then(()=>{allcopy.textContent='已复制';setTimeout(()=>allcopy.textContent='复制全部直链',1200);});
});
load().catch(err => { empty.style.display='block'; empty.textContent='加载失败：'+err.message; });
</script>
</body>
</html>
"""


def render_readme() -> str:
    return """PagesLinker 生成站点 · 部署说明
================================

一、部署到 Cloudflare Pages（手动）
  方式 A（网页控制台）：
    1. 登录 dash.cloudflare.com，选择 Pages
    2. Create new project → Direct Upload
    3. 拖入整个 _site 文件夹（或解压 site.zip 后拖入）
    4. 点击 Deploy，得到 https://<项目名>.pages.dev

  方式 B（命令行）：
    npm i -g wrangler
    wrangler pages deploy _site

二、直链规则
  - 小文件（≤阈值）：https://<项目名>.pages.dev/<资源路径>
  - 大文件（>阈值）：https://<项目名>.pages.dev/<资源路径>.html
    该页会在浏览器内自动拉取分片并还原文件，可下载/预览。

三、限制说明
  - 单个静态文件上限 25 MiB，因此大文件被拆成 <25 MiB 分片
  - 单次部署文件数上限 20,000
  - 无需 Pages Functions、无需 R2，纯静态

四、index.html 为资源索引页，可在页内按需复制直链。
"""


# --------------------------------------------------------------------------
# CLI entry (no GUI dependency)
# --------------------------------------------------------------------------

def cli_main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="PagesLinker - Cloudflare Pages 资源直链打包器")
    ap.add_argument("paths", nargs="+", help="文件/文件夹/zip 路径")
    ap.add_argument("-o", "--output", default="output", help="输出目录（默认 output）")
    ap.add_argument("--threshold", type=float, default=20.0, help="分片阈值 MiB（1–24，默认 20）")
    ap.add_argument("--no-dedupe", action="store_true", help="关闭去重")
    ap.add_argument("--keep-hidden", action="store_true", help="保留隐藏文件")
    ap.add_argument("--ascii", action="store_true", help="全量 ASCII 安全化重命名")
    args = ap.parse_args(argv)

    def emit(ev: StatusEvent) -> None:
        if ev.kind == "log":
            print("  " + ev.message)
        elif ev.kind == "file":
            print(f"[{ev.index+1}/{ev.total}] {ev.path}")
        elif ev.kind == "done":
            print("完成")

    try:
        res = build_site(
            args.paths, args.output,
            threshold_mb=args.threshold,
            dedupe=not args.no_dedupe,
            skip_hidden=not args.keep_hidden,
            force_ascii=args.ascii,
            emit=emit,
        )
    except PagesLinkerError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    print("\n=== 构建结果 ===")
    print(f"站点目录: {res.site_dir}")
    print(f"ZIP:      {res.zip_path}")
    print(f"资源总数: {len(res.entries)}  (直接 {res.direct_count} / 分片 {res.chunked_count})")
    print(f"分片总数: {res.chunk_count}")
    print(f"站点文件: {res.total_files}")
    print(f"原始体积: {human_size(res.total_bytes)}")
    return 0


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv[1:]))
