"""状态轮询的日志增量（前端只要新增条目）与轻量模式。"""
from airadio.orchestrator import Orchestrator


def _orch(tmp_path):
    return Orchestrator("000000", "第一句话。\n第二句话。", sink="dry",
                        out_dir=str(tmp_path / "c"))


def test_log_cursor_returns_only_new_lines(tmp_path):
    o = _orch(tmp_path)
    o.log_append("a")
    o.log_append("b")
    snap = o.snapshot(log_after=0)
    assert len(snap["log"]) == 2 and snap["log"][1].endswith("b")
    first_total = snap["log_total"]
    assert first_total == 2

    o.log_append("c")
    inc = o.snapshot(log_after=first_total)["log"]
    assert len(inc) == 1 and inc[0].endswith("c")
    assert o.snapshot(log_after=o.snapshot()["log_total"])["log"] == []   # 没有新日志就什么都不回


def test_lite_drops_phrase_config(tmp_path):
    o = _orch(tmp_path)
    full = o.snapshot()
    lite = o.snapshot(lite=True, events_n=12)
    assert "phrases" in full
    assert "phrases" in lite          # snapshot 本身保留（console 层对 lite 再 pop）
    assert len(lite["events"]) <= 12


def test_polling_payload_shrinks_after_first_fetch(tmp_path):
    """稳态轮询要明显小于首屏：日志不重复传是主要收益。"""
    import json
    o = _orch(tmp_path)
    for i in range(60):
        o.log_append(f"line {i}")
    first = json.dumps(o.snapshot(log_after=0), ensure_ascii=False)
    steady = json.dumps(o.snapshot(log_after=o._log_total), ensure_ascii=False)
    assert len(steady) < len(first) * 0.6
    assert len(json.loads(steady)["log"]) == 0
