"""把当前 git HEAD 通过 GitHub API（tree+ref）推到远端仓库；ref 已存在则强制更新。"""
import json, subprocess, urllib.request

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
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "body": e.read().decode()[:300]}

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

tree = api(f"/repos/{REPO}/git/trees", {"tree": entries})
if "_http" in tree:
    print("TREE FAILED:", tree); raise SystemExit(1)
print("tree:", tree["sha"])

ref = api(f"/repos/{REPO}/git/refs", {"ref": "refs/heads/main", "sha": tree["sha"]})
if "_http" in ref and ref["_http"] == 422:  # ref exists -> force update
    ref = api(f"/repos/{REPO}/git/refs/heads/main",
              {"sha": tree["sha"], "force": True}, method="PATCH")
print("ref result:", json.dumps(ref, ensure_ascii=False)[:200])

branches = api(f"/repos/{REPO}/branches")
print("branches now:", [b["name"] for b in branches] if isinstance(branches, list) else branches)
