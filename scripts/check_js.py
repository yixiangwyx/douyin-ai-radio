#!/usr/bin/env python3
"""check_js.py — 把 console.html / login.html / admin.html 里的内联 <script> 抽出来给 node --check 体检。

踩过的坑：内联脚本里 `const g` 撞名 → 整块脚本不执行（页面看着正常但按钮没反应、
状态永远"连接中…"）。JS 没有构建步骤，只有这个检查能在跑之前抓到。
用法: uv run python scripts/check_js.py
"""
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGES = ["web/console.html", "web/login.html", "web/admin.html", "web/player.html"]
FILES = ["web/app.js"]


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node 不在 PATH，跳过 JS 语法检查")
        return 0
    bad = 0
    tmp = pathlib.Path("/tmp/airadio_js_check")
    tmp.mkdir(exist_ok=True)
    n = 0
    for page in PAGES:
        p = ROOT / page
        if not p.is_file():
            continue
        for i, block in enumerate(re.findall(r"<script>(.*?)</script>", p.read_text(encoding="utf-8"), re.S)):
            n += 1
            f = tmp / f"{page.replace('/', '_')}_{i}.js"
            f.write_text(block, encoding="utf-8")
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            if r.returncode:
                bad += 1
                print(f"[X] {page} 内联脚本 #{i}:\n{r.stderr[:700]}")
    for jsfile in FILES:
        p = ROOT / jsfile
        if not p.is_file():
            continue
        n += 1
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        if r.returncode:
            bad += 1
            print(f"[X] {jsfile}:\n{r.stderr[:700]}")
    print(f"JS 语法检查：{n} 段，{bad} 段有问题")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
