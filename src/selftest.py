"""End-to-end selftest for PagesLinker (no GUI required).

Builds a test set, runs the engine, and verifies:
- small files copied verbatim
- duplicate file deduped to one physical copy
- CJK filename kept + URL percent-encoding
- 60 MiB file split into chunks all < 25 MiB, count == ceil(size/threshold)
- reassembly page exists with correct embedded metadata
- manifest matches files on disk
- reassembled chunks == original bytes (local reconstruction of what the
  browser page does)

Exit code 0 = all pass.
"""

import hashlib
import json
import math
import os
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pageslinker import build_site, human_size

HERE = Path(__file__).parent
WORK = HERE / "selftest_work"
TESTSET = WORK / "testset"
OUT = WORK / "out"

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def make_testset() -> None:
    if TESTSET.exists():
        shutil.rmtree(TESTSET)
    TESTSET.mkdir(parents=True)

    # 1. small image (tiny valid PNG)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626000000006000527dcca4b0000000049454e44ae426082"
    )
    (TESTSET / "pic.png").write_bytes(png)

    # 2. text file
    (TESTSET / "note.txt").write_text("hello pageslinker\n" * 100, encoding="utf-8")

    # 3. small zip
    import zipfile
    with zipfile.ZipFile(TESTSET / "pack.zip", "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inner/a.txt", "aaa" * 100)
        zf.writestr("inner/b.bin", os.urandom(4096))

    # 4. duplicate of pic.png in a subfolder
    sub = TESTSET / "sub"
    sub.mkdir()
    (sub / "pic_copy.png").write_bytes(png)  # same content -> dedupe

    # 5. CJK filename
    (TESTSET / "中文名称文件.txt").write_text("中文内容测试", encoding="utf-8")

    # 6. 60 MiB pseudo-random big file (deterministic-ish: random per 1MiB blocks)
    rng = random.Random(42)
    big = TESTSET / "bigfile.bin"
    with open(big, "wb") as f:
        for _ in range(60):
            f.write(rng.randbytes(1024 * 1024))


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    make_testset()

    if OUT.exists():
        shutil.rmtree(OUT)

    res = build_site(
        [str(TESTSET)],
        str(OUT),
        threshold_mb=20.0,
        dedupe=True,
        skip_hidden=True,
        force_ascii=False,
    )

    site = res.site_dir

    # ---- basic outputs ----
    check("index.html exists", (site / "index.html").is_file())
    check("manifest.json exists", (site / "manifest.json").is_file())
    check("README.txt exists", (site / "README.txt").is_file())
    check("site.zip exists", res.zip_path.is_file())

    # ---- small files copied verbatim ----
    check("pic.png copied", (site / "pic.png").is_file())
    check("note.txt copied", (site / "note.txt").is_file())
    check("pack.zip copied", (site / "pack.zip").is_file())
    check("sub/pic_copy.png listed in manifest (dedupe)",
          any(e["original_path"] == "sub/pic_copy.png" for e in json.loads((site / "manifest.json").read_text(encoding="utf-8"))))

    # ---- dedupe: same sha -> only one physical chunk/copy ----
    manifest = json.loads((site / "manifest.json").read_text(encoding="utf-8"))
    pic_entry = next(e for e in manifest if e["original_path"] == "pic.png")
    copy_entry = next(e for e in manifest if e["original_path"] == "sub/pic_copy.png")
    check("dedupe: same url for identical content", pic_entry["url_path"] == copy_entry["url_path"])

    # ---- CJK filename ----
    cjk = next((e for e in manifest if "中文" in e["original_path"]), None)
    check("CJK file in manifest", cjk is not None)
    if cjk:
        check("CJK url percent-encoded", "%" in cjk["url_path"], cjk["url_path"])
        from urllib.parse import unquote
        check("CJK url decodes back to original name",
              unquote(cjk["url_path"]) == cjk["original_path"])

    # ---- big file chunking ----
    big_entry = next(e for e in manifest if e["original_path"] == "bigfile.bin")
    check("bigfile kind == chunked", big_entry["kind"] == "chunked")
    n_expect = math.ceil((TESTSET / "bigfile.bin").stat().st_size / (20 * 1024 * 1024))
    check("chunk count == ceil(size/threshold)", big_entry["chunks"] == n_expect,
          f"expected {n_expect}, got {big_entry['chunks']}")

    chunk_dir = site / "__chunks__" / big_entry["sha256"][:16]
    chunk_files = sorted(chunk_dir.glob("*.bin"))
    check("chunk files on disk == chunk count", len(chunk_files) == big_entry["chunks"])
    sizes = [p.stat().st_size for p in chunk_files]
    check("all chunks < 25 MiB", all(s < 25 * 1024 * 1024 for s in sizes),
          f"max {max(sizes)}")
    check("all non-final chunks == threshold",
          all(s == 20 * 1024 * 1024 for s in sizes[:-1]))

    # reassembly page
    from urllib.parse import unquote
    page_rel = unquote(big_entry["url_path"])
    page = site / page_rel
    check("reassembly page exists", page.is_file(), str(page_rel))
    page_html = page.read_text(encoding="utf-8")
    check("page embeds total sha256", big_entry["sha256"] in page_html)
    check("page embeds chunk count", f"chunks: {big_entry['chunks']}" in page_html or str(big_entry["chunks"]) in page_html)
    check("page uses relative chunk refs", "__chunks__" in page_html and "http://" not in page_html.split("<body")[0])

    # ---- local reconstruction: chunks concatenated == original ----
    merged = b"".join(p.read_bytes() for p in chunk_files)
    check("concatenated chunks == original bigfile",
          hashlib.sha256(merged).hexdigest() == big_entry["sha256"] and
          merged == (TESTSET / "bigfile.bin").read_bytes())

    # ---- manifest vs disk ----
    ok_all = True
    for e in manifest:
        p = site / unquote(e["url_path"])
        if not p.is_file():
            ok_all = False
            print(f"      missing: {e['url_path']}")
    check("every manifest url_path exists on disk", ok_all)

    # ---- file count cap ----
    check("total files <= 20000", res.total_files <= 20000, str(res.total_files))

    # ---- zip integrity ----
    import zipfile
    with zipfile.ZipFile(res.zip_path) as zf:
        bad = zf.testzip()
    check("zip integrity", bad is None)

    print()
    print(f"resources={len(res.entries)} direct={res.direct_count} chunked={res.chunked_count} "
          f"chunks={res.chunk_count} files={res.total_files} bytes={human_size(res.total_bytes)}")
    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
