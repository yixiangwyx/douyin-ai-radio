"""话题库 / 稿子库：本地 JSON 存储（data/library.json）。

结构：{"topics": [...], "scripts": [{"id","title","text","created","source"}]}
稿子库保存多套稿，可随时切换播放；话题库保存话题条目，供 AI 生成新稿时选用。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

_LOCK_HINT = "稿子库"


class Library:
    def __init__(self, path: str | Path = "data/library.json"):
        self.path = Path(path)
        self.data: dict = {"topics": [], "scripts": []}
        if self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.data.setdefault("topics", [])
        self.data.setdefault("scripts", [])

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- topics ----
    def add_topic(self, title: str, note: str = "") -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("话题标题不能为空")
        for t in self.data["topics"]:
            if t["title"] == title:
                t["note"] = note or t.get("note", "")
                self._save()
                return t
        item = {"id": uuid.uuid4().hex[:8], "title": title, "note": note.strip(),
                "created": time.time(), "used": 0}
        self.data["topics"].insert(0, item)
        self._save()
        return item

    def remove_topic(self, tid: str) -> bool:
        before = len(self.data["topics"])
        self.data["topics"] = [t for t in self.data["topics"] if t["id"] != tid]
        self._save()
        return len(self.data["topics"]) < before

    # ---- scripts ----
    def add_script(self, title: str, text: str, source: str = "手动保存") -> dict:
        title = (title or "").strip() or "未命名稿"
        if not (text or "").strip():
            raise ValueError("稿子内容为空")
        item = {"id": uuid.uuid4().hex[:8], "title": title, "text": text,
                "created": time.time(), "source": source,
                "sentences": sum(1 for l in text.splitlines() if l.strip() and not l.strip().startswith("#"))}
        self.data["scripts"].insert(0, item)
        self.data["scripts"] = self.data["scripts"][:100]   # 上限 100 套，防止无限膨胀
        self._save()
        return item

    def remove_script(self, sid: str) -> bool:
        before = len(self.data["scripts"])
        self.data["scripts"] = [s for s in self.data["scripts"] if s["id"] != sid]
        self._save()
        return len(self.data["scripts"]) < before

    def get_script(self, sid: str) -> dict | None:
        for s in self.data["scripts"]:
            if s["id"] == sid:
                return s
        return None

    def snapshot(self) -> dict:
        return {
            "topics": [{k: t.get(k) for k in ("id", "title", "note", "used")} | {"created": t.get("created", 0)}
                       for t in self.data["topics"]],
            "scripts": [{k: s.get(k) for k in ("id", "title", "text", "source", "sentences", "created")}
                        for s in self.data["scripts"]],
        }
