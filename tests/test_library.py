"""话题库/稿子库 + 生成（本地模板路径，不依赖百炼额度）。"""
from airadio.generate import STARTER_TOPICS, _clean_draft, draft_from_topic
from airadio.library import Library


def test_library_add_and_roundtrip(tmp_path):
    lib = Library(tmp_path / "lib.json")
    t = lib.add_topic("留不住人怎么办", "先做预期管理")
    assert t["id"] and t["title"] == "留不住人怎么办"
    s = lib.add_script("留人话术 第1版", "第一句。\n第二句。\n第三句。", source="测试")
    assert s["sentences"] == 3
    got = lib.get_script(s["id"])
    assert got and got["text"].count("\n") == 2
    snap = lib.snapshot()
    assert len(snap["topics"]) == 1 and len(snap["scripts"]) == 1
    # 持久化：重新打开同一个文件内容还在
    lib2 = Library(tmp_path / "lib.json")
    assert lib2.get_script(s["id"])["title"] == "留人话术 第1版"


def test_library_dedupe_topics_and_delete(tmp_path):
    lib = Library(tmp_path / "lib.json")
    lib.add_topic("同一话题")
    lib.add_topic("同一话题", "补充要点")
    assert len(lib.data["topics"]) == 1
    assert lib.data["topics"][0]["note"] == "补充要点"
    assert lib.remove_topic(lib.data["topics"][0]["id"]) is True
    assert lib.data["topics"] == []
    assert lib.remove_topic("nope") is False


def test_library_script_cap(tmp_path):
    lib = Library(tmp_path / "lib.json")
    for i in range(120):
        lib.add_script(f"稿{i}", "一句话。")
    assert len(lib.data["scripts"]) == 100


def test_local_draft_follows_topic_type():
    text = draft_from_topic("直播间留不住人怎么办", sentences=12)
    lines = [l for l in text.splitlines() if l.strip()]
    assert len(lines) >= 5
    assert "停留" in text or "停留时长" in text      # 命中留人类要点包
    assert not any(l.strip().startswith(("1.", "1、", "-")) for l in lines)
    text2 = draft_from_topic("怎么看直播数据 GPM", sentences=12)
    assert "千次观看成交额" in text2 or "曝光进入率" in text2


def test_clean_draft_strips_numbering_and_markdown():
    raw = "1. 第一句内容\n- 第二句内容\n**第三句内容**\n说明：以上是稿子"
    out = _clean_draft(raw, 10)
    lines = out.splitlines()
    assert lines[0] == "第一句内容"
    assert lines[1] == "第二句内容"
    assert lines[2] == "第三句内容"
    assert "说明" not in out


def test_clean_draft_respects_sentence_limit():
    raw = "\n".join(f"第{i}句内容内容" for i in range(30))
    assert len(_clean_draft(raw, 8).splitlines()) == 8


def test_starter_topics_shape():
    assert len(STARTER_TOPICS) >= 8
    for t in STARTER_TOPICS:
        assert isinstance(t, tuple) and len(t) == 2 and t[0] and t[1]
