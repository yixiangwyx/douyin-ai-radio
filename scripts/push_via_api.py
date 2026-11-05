"""把当前 git HEAD 通过 GitHub API（tree+ref 两次调用）推到远端空仓库。"""
import json, subprocess, urllib.request, base64, os, sys

TOKEN = open('/tmp/gh_pat.txt').read().strip()
REPO = "yixiangwyx/douyin-ai-radio"
EXCLUDE_PREFIX = (".venv/", "out/", "__pycache__/", ".git/")

def api(path, payload=None, method=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method or ("POST" if payload is not None else "GET"),
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/json",
                 "Content-Type": "application/json", "User-Agent": "airadio-push"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())

# 1) 取 HEAD 文件清单与内容（排除敏感/生成物）
files = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"],
                       capture_output=True, text=True, check=True).stdout.split("\n")
entries = []
skipped = []
for f in files:
    if not f or any(f.startswith(p) for p in EXCLUDE_PREFIX) or f == ".env":
        skipped.append(f); continue
    content = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, check=True).stdout
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        skipped.append(f + " (binary)"); continue
    entries.append({"path": f, "mode": "100644", "type": "blob", "content": text})

print(f"files: {len(entries)}, skipped: {skipped}")

# 2) create tree（含全部文件内容）
tree = api(f"/repos/{REPO}/git/trees", {"tree": entries})
print("tree:", tree.get("sha"), tree.get("message", ""))

# 3) create ref main
ref = api(f"/repos/{REPO}/git/refs", {"ref": "refs/heads/main", "sha": tree["sha"]})
print("ref:", ref.get("ref"), ref.get("object", {}).get("sha", ref.get("message", "")))
