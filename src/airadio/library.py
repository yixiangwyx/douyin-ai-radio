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

    def rotate_source(self) -> list[dict]:
        """给 orchestrator 轮换用：只返回内容非空的稿（顺序与稿子库一致）。"""
        return [{"title": s.get("title", ""), "text": s.get("text", "")}
                for s in self.data["scripts"] if (s.get("text") or "").strip()]

    # ---- 话术/昵称/报时配置（前端可编辑，重启后还在） ----
    def get_phrases(self) -> dict:
        return dict(self.data.get("phrases") or {})

    def save_phrases(self, patch: dict) -> dict:
        cur = self.data.get("phrases") or {}
        for key, value in (patch or {}).items():
            if value is not None:
                cur[key] = value
        self.data["phrases"] = cur
        self._save()
        return dict(cur)

    # ---- 上次用的模型/音色（重启后自动恢复，不用每次重选） ----
    def save_runtime(self, patch: dict) -> dict:
        cur = self.data.get("runtime") or {}
        for key, value in (patch or {}).items():
            if value:
                cur[key] = value
        self.data["runtime"] = cur
        self._save()
        return dict(cur)

    def get_runtime(self) -> dict:
        return dict(self.data.get("runtime") or {})

    # ---- 百炼 Key（音色管理/生成必须在你还没开播时也能用，所以落一份到本用户库） ----
    def save_bailian_key(self, key: str) -> None:
        key = (key or "").strip()
        if key:
            self.data["bailian_key"] = key
            self._save()

    def get_bailian_key(self) -> str:
        return (self.data.get("bailian_key") or "").strip()

    # ---- 抖音登录 Cookie（有它才有礼物数据；存在该用户目录，不进 Git） ----
    def save_douyin_cookie(self, cookie: str) -> None:
        self.data["douyin_cookie"] = (cookie or "").strip()
        self._save()

    def get_douyin_cookie(self) -> str:
        return (self.data.get("douyin_cookie") or "").strip()

    def snapshot(self) -> dict:
        return {
            "phrases": self.get_phrases(),
            "topics": [{k: t.get(k) for k in ("id", "title", "note", "used")} | {"created": t.get("created", 0)}
                       for t in self.data["topics"]],
            "scripts": [{k: s.get(k) for k in ("id", "title", "text", "source", "sentences", "created")}
                        for s in self.data["scripts"]],
            "gen_templates": [{k: t.get(k) for k in ("id", "title", "text")}
                              for t in self.data.get("gen_templates") or []],
            "gen_cfg": self.get_gen_cfg(),
            "gen_queue": [{"title": g.get("title", ""), "model": g.get("model", "")}
                          for g in self.data.get("gen_queue") or []],
            "bailian_key_set": bool(self.get_bailian_key()),
        }

    # ---- 直播内容框架提示词（可保存多套，开播时选一套生成新稿） ----
    def add_gen_template(self, title: str, text: str) -> dict:
        title = (title or "").strip() or "未命名框架"
        if not (text or "").strip():
            raise ValueError("提示词内容为空")
        for t in self.data.setdefault("gen_templates", []):
            if t["title"] == title:
                t["text"] = text.strip()
                self._save()
                return t
        item = {"id": uuid.uuid4().hex[:8], "title": title, "text": text.strip()}
        self.data["gen_templates"].insert(0, item)
        self.data["gen_templates"] = self.data["gen_templates"][:30]
        self._save()
        return item

    def remove_gen_template(self, tid: str) -> bool:
        before = len(self.data.get("gen_templates") or [])
        self.data["gen_templates"] = [t for t in self.data.get("gen_templates") or [] if t["id"] != tid]
        self._save()
        return len(self.data["gen_templates"]) < before

    def get_gen_template(self, tid: str) -> dict | None:
        for t in self.data.get("gen_templates") or []:
            if t["id"] == tid:
                return t
        return None

    # ---- 对接的大模型配置（生成新稿用；和 TTS 的百炼 Key 是两回事） ----
    def save_gen_cfg(self, patch: dict) -> dict:
        cur = self.data.get("gen_cfg") or {}
        for key in ("backend", "model", "base_url", "api_key", "prompt", "lines",
                    "auto_generate", "auto_generate_at"):
            if key in patch and patch[key] is not None:
                cur[key] = patch[key]
        self.data["gen_cfg"] = cur
        self._save()
        return dict(cur)

    def get_gen_cfg(self) -> dict:
        return dict(self.data.get("gen_cfg") or {})

    # ---- AI 稿队列：生成好但还没播的稿（只用一次，播完即弃，不进稿子库） ----
    def enqueue_gen_script(self, title: str, text: str, model: str = "") -> dict:
        item = {"id": uuid.uuid4().hex[:8], "title": title, "text": text,
                "model": model, "created": time.time()}
        self.data.setdefault("gen_queue", []).append(item)
        self.data["gen_queue"] = self.data["gen_queue"][-8:]
        self._save()
        return item

    def pop_gen_script(self) -> dict | None:
        q = self.data.get("gen_queue") or []
        if not q:
            return None
        item = q.pop(0)
        self.data["gen_queue"] = q
        self._save()
        return item

    def clear_gen_queue(self) -> None:
        self.data["gen_queue"] = []
        self._save()

    # ---- 自建音色记录（谁生成的、用什么提示词生成的、给它的名字） ----
    # 百炼 list_voice 只回 voice_id/status，名字要自己记着才认得出来
    def add_voice_record(self, voice_id: str, *, model: str = "", label: str = "",
                         prompt: str = "", mode: str = "design") -> dict:
        voice_id = (voice_id or "").strip()
        if not voice_id:
            raise ValueError("没有音色 ID")
        for v in self.data.setdefault("voices", []):
            if v["voice_id"] == voice_id:
                v["model"] = model or v.get("model", "")
                v["label"] = label or v.get("label", "")
                v["prompt"] = prompt or v.get("prompt", "")
                v["mode"] = mode or v.get("mode", "")
                self._save()
                return v
        item = {"voice_id": voice_id, "model": model, "label": label, "prompt": prompt,
                "mode": mode, "created": time.time()}
        self.data["voices"].insert(0, item)
        self.data["voices"] = self.data["voices"][:200]
        self._save()
        return item

    def get_voices(self) -> list[dict]:
        return list(self.data.get("voices") or [])

    def find_voice(self, voice_id: str) -> dict | None:
        for v in self.data.get("voices") or []:
            if v["voice_id"] == voice_id:
                return v
        return None

    def set_voice_label(self, voice_id: str, label: str) -> bool:
        v = self.find_voice(voice_id)
        if not v:
            return False
        v["label"] = (label or "").strip()[:60]
        self._save()
        return True

    def remove_voice_record(self, voice_id: str) -> bool:
        before = len(self.data.get("voices") or [])
        self.data["voices"] = [v for v in self.data.get("voices") or []
                               if v["voice_id"] != voice_id]
        self._save()
        return len(self.data["voices"]) < before
