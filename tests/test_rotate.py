"""多稿轮换：一轮播完自动切下一套稿；不足 2 套不切；切换后按新稿预生成。"""
import time

from airadio.library import Library
from airadio.orchestrator import Orchestrator


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.last_error = ""

    def synth(self, texts, timeout_ms=30000):
        self.calls += 1
        return b"audio:" + " ".join(texts).encode()


def _mk(tmp_path, script, lib: Library):
    o = Orchestrator("0", script, sink="dry", out_dir=str(tmp_path))
    o.tts = FakeTTS()
    o.trim_silence = False
    o.merge_sentences = 1
    o.prefetch_ahead = 0
    o.script_source = lib.rotate_source
    return o


def _lib(tmp_path):
    lib = Library(tmp_path / "lib.json")
    lib.add_script("A稿", "A第一句。\nA第二句。", source="测试")
    lib.add_script("B稿", "B第一句。\nB第二句。", source="测试")
    return lib


def test_rotate_off_keeps_looping_same_script(tmp_path):
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。\nA第二句。", lib)
    o.set_rotate(False, lib.rotate_source)
    for _ in range(3):
        o._play_pass()
        o._seg_i = 0        # 模拟 run() 的循环复位（自然播完从头再来）
    assert "A第一句。" in o.script_text
    assert "B第一句。" not in o.script_text
    assert o.tts.calls == 2      # 同一稿循环 3 次只合成一次（走音频缓存）
    assert o.snapshot()["cache_hits"] == 4


def test_rotate_switches_to_next_script_after_full_pass(tmp_path):
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。\nA第二句。", lib)
    idx_a = next(i for i, s in enumerate(lib.data["scripts"]) if s["title"] == "A稿")
    o.set_rotate(True, lib.rotate_source, idx=idx_a)
    o._play_pass()
    assert o._maybe_rotate() is True
    assert "B第一句。" in o.script_text
    assert o.script_title == "B稿"
    assert o._seg_i == 0
    o._play_pass()
    assert o._maybe_rotate() is True     # 再一轮切回 A
    assert "A第一句。" in o.script_text


def test_rotate_needs_two_scripts(tmp_path):
    lib = Library(tmp_path / "lib.json")
    lib.add_script("只有一套", "只有一句。")
    o = _mk(tmp_path, "第一句。\n第二句。", lib)
    o.set_rotate(True, lib.rotate_source)
    assert o._maybe_rotate() is False    # 不足 2 套不切


def test_rotate_skips_empty_scripts(tmp_path):
    lib = _lib(tmp_path)
    lib.data["scripts"].append({"id": "zz", "title": "空稿", "text": "   "})   # 直接塞进库，模拟脏数据
    o = _mk(tmp_path, "A第一句。", lib)
    o.set_rotate(True, lib.rotate_source, idx=0)
    texts = [s["text"] for s in lib.rotate_source()]
    assert all(t.strip() for t in texts)          # 空稿不进轮换源
    assert len(texts) == 2
    assert o._maybe_rotate() is True


def test_rotate_after_time_budget_switches_at_boundary(tmp_path):
    """时间预算：这套稿播够 N 秒就在下一个段边界切，不必等整稿播完。"""
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。\nA第二句。", lib)
    o.script_title = "A稿"
    o.set_rotate(True, lib.rotate_source, after_s=1)   # 1 秒就切
    o._script_started_at = time.time() - 100            # 假装已经播了 100 秒
    o._play_pass()
    assert "B第一句。" in o.script_text                 # 播完这段就切了
    assert o.script_title == "B稿"


def test_rotate_time_budget_not_reached_keeps_script(tmp_path):
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。\nA第二句。", lib)
    o.script_title = "A稿"
    o.set_rotate(True, lib.rotate_source, after_s=600)
    o._script_started_at = time.time()
    o._play_pass()
    assert "A第一句。" in o.script_text                 # 没到时间不切


def test_rotate_next_follows_current_script_title(tmp_path):
    """手动切稿后，自动轮换从当前这套往后切，不会切回同一套。"""
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "B第一句。\nB第二句。", lib)
    o.script_title = "B稿"
    o.set_rotate(True, lib.rotate_source, idx=99)       # 故意给一个错索引
    assert o._maybe_rotate() is True
    assert o.script_title == "A稿"                       # 从 B 切到 A，而不是 B→B


def test_rotate_after_lines_switches_at_line_boundary(tmp_path):
    """按稿子行切换：播够 N 行就在行边界切下一套（一行可能含多段）。"""
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。A第二句。\nA第三句。\nA第四句。", lib)
    o.script_title = "A稿"
    o._lines_played = 2                       # 已播到第 3 行
    o.set_rotate(True, lib.rotate_source, after_lines=3)
    o._play_pass()                            # 播第 3 行（1 段）后计数=3 → 切
    assert o.script_title == "B稿"
    assert o._lines_played == 0               # 切稿后重新计数


def test_rotate_triggers_prefetch_for_new_script(tmp_path):
    lib = _lib(tmp_path)
    o = _mk(tmp_path, "A第一句。\nA第二句。", lib)
    o.prefetch_ahead = 2
    idx_a = next(i for i, s in enumerate(lib.data["scripts"]) if s["title"] == "A稿")
    o.set_rotate(True, lib.rotate_source, idx=idx_a)
    o._maybe_rotate()
    o._prefetch_thread.join(timeout=10)
    snap = o.snapshot()
    assert snap["cache_ready"] == 2 and snap["cache_total"] == 2   # 新稿自动预生成
