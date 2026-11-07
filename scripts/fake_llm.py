"""Local fake OpenAI-compatible endpoint used for an end-to-end UI smoke test (not a real model)."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

counter = {"n": 0}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n).decode())
        counter["n"] += 1
        i = counter["n"]
        lines = [
            f"第{i}套新稿开场：先说清这场能听到什么。",
            "停留短不是话术差，是观众不知道能得到什么。",
            "进场第一句先给一个具体的好处，而不是欢迎。",
            "第二段讲一个做法：把长句拆成短句，一行只说一件事。",
            "第三段讲数值：一段30到60秒，一个主题说完再换。",
            "互动：你现在卡在开场还是卡在中途？打1或2。",
            "第四段讲收尾：总结三句，再预告下一场的时间。",
            f"这套稿到这就完了，下一套接着讲。{i}",
        ]
        content = "\n".join(lines)
        out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("127.0.0.1", 8099), H)
    print("fake LLM on 127.0.0.1:8099", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
