"""「从头重播 / 跳句 / 暂停」必须立刻生效：不能等当前这一段播完。

用户实测的抱怨：点「从头开始」后"还要等半天"。原因是播放节拍 = 真实音频时长
（一组最长几十秒），而旧实现用 time.sleep() 睡觉，控制命令只能等它睡完才被处理。
"""
import threading
import time

from airadio.orchestrator import Orchestrator

LONG_LINE = "这一句非常长" * 40 + "。"   # 估算节拍 ~15 秒（dry 模式没有真实音频长度）


class FakeTTS:
    def __init__(self):
        self.calls = 0
        self.last_error = ""

    def synth(self, texts, timeout_ms=30000):
        self.calls += 1
        return b""    # dry：没有真实音频长度 → 走估算节拍（故意让节拍很长）


def _mk(tmp_path, script="第一行。\n第二行。\n第三行。"):
    o = Orchestrator("0", script, sink="dry", out_dir=str(tmp_path))
    o.tts = FakeTTS()
    o.trim_silence = False
    o.merge_sentences = 1
    o.prefetch_ahead = 0
    o.status = "connected"
    o.queue.stats = {"chat": 1}      # 让 run() 认为可以播
    return o


def _enter_beat(o):
    """把播放线程跑到'正在等这一段节拍'的状态（_play_pass 卡在真实时长等待里）。"""
    t = threading.Thread(target=o._play_pass, daemon=True)
    t.start()
    time.sleep(0.6)
    return t


def test_rewind_interrupts_current_beat(tmp_path):
    o = _mk(tmp_path, LONG_LINE + "\n第二行。")
    t = _enter_beat(o)
    assert o._seg_i >= 1                       # 已经在播第一句、正在睡节拍
    epoch0 = o.snapshot()["playback_epoch"]

    started = time.time()
    o.rewind()
    time.sleep(0.5)
    assert not t.is_alive(), "点『从头重播』后还在等当前段播完（旧 bug：要等半天）"
    assert time.time() - started < 1.5
    assert o.snapshot()["playback_epoch"] == epoch0 + 1
    assert o._seg_i == 0                       # 回到稿首


def test_skip_interrupts_current_beat(tmp_path):
    o = _mk(tmp_path, LONG_LINE + "\n第二行。\n第三行。")
    t = _enter_beat(o)
    o.skip_sentence()
    # 跳句后当前这一段（约 15 秒）必须被叫停：整条 _play_pass 远快于一整段节拍
    started = time.time()
    t.join(timeout=30)
    assert not t.is_alive()
    elapsed = time.time() - started
    assert elapsed < 10, f"跳句没有打断当前节拍（整条播放用了 {elapsed:.1f}s）"
    assert o._seg_i >= 2                       # 跳过的那一句不再等它播完


def test_pause_interrupts_current_beat(tmp_path):
    o = _mk(tmp_path, LONG_LINE + "\n第二行。")
    t = _enter_beat(o)
    o.pause()
    time.sleep(0.5)
    assert not t.is_alive(), "暂停也要立刻结束当前节拍"
    assert o.paused


def test_snapshot_exposes_playback_epoch_and_start(tmp_path):
    o = _mk(tmp_path)
    snap = o.snapshot()
    assert snap["playback_epoch"] == 0
    assert snap["play_start_chunk"] == 0
    o.rewind()
    snap = o.snapshot()
    assert snap["playback_epoch"] == 1
    # 新起点对应的块下标：前端据此清掉已排好的队列、从正确位置接播
    assert snap["play_start_chunk"] == len(o.chunk_writer.manifest)
