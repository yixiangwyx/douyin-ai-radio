"""完成 GitHub API 推送的第二步：基于已建 tree 创建/更新 main ref。"""
import json, subprocess, urllib.request

TOKEN = open('/tmp/gh_pat.txt').read().strip()
REPO = "yixiangwyx/douyin-ai-radio"

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

# 远端当前 refs
print("branches:", api(f"/repos/{REPO}/branches"))
print("ref main:", api(f"/repos/{REPO}/git/refs/heads/main"))

# 本地 HEAD sha
sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
print("local HEAD:", sha)
