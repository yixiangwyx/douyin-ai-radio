"""空仓库初始化（低层 git db API）：blobs → tree → commit(no parents) → refs/heads/main。"""
import base64, json, subprocess, urllib.request

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
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "body": e.read().decode()[:400]}

files = subprocess.run(["git", "ls-tree", "-r", "HEAD", "--name-only"],
                       capture_output=True, text=True, check=True).stdout.split("\n")
tree_entries = []
skipped = []
for f in files:
    if not f or any(f.startswith(p) for p in EXCLUDE_PREFIX) or f == ".env":
        skipped.append(f); continue
    content = subprocess.run(["git", "show", f"HEAD:{f}"], capture_output=True, check=True).stdout
    blob = api(f"/repos/{REPO}/git/blobs",
               {"content": base64.b64encode(content).decode(), "encoding": "base64"})
    if "_http" in blob:
        print("BLOB FAILED", f, blob); raise SystemExit(1)
    tree_entries.append({"path": f, "mode": "100644", "type": "blob", "sha": blob["sha"]})
print(f"blobs created: {len(tree_entries)}, skipped: {skipped}")

tree = api(f"/repos/{REPO}/git/trees", {"tree": tree_entries})
if "_http" in tree:
    print("TREE FAILED:", tree); raise SystemExit(1)
print("tree:", tree["sha"])

commit = api(f"/repos/{REPO}/git/commits", {"message": "抖音AI电台直播系统 v0.1：webcast互动采集 + 句边界随机插播 + 百炼CosyVoice TTS", "tree": tree["sha"]})
if "_http" in commit:
    print("COMMIT FAILED:", commit); raise SystemExit(1)
print("commit:", commit["sha"])

ref = api(f"/repos/{REPO}/git/refs", {"ref": "refs/heads/main", "sha": commit["sha"]})
if "_http" in ref:
    print("REF FAILED:", ref); raise SystemExit(1)
print("ref created:", ref["ref"])
print("default branch:", api(f"/repos/{REPO}").get("default_branch"))
