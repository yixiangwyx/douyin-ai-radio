"""音频去重与可永久缓存：循环重播不重复写盘、客户端不必重复下载同样的字节。"""
import json

from airadio.player import ChunkWriter, audio_hash


def _mp3(n_frames=2):
    """构造最小合法 MP3（MPEG2 LayerIII，带 CRC），供 duration_ms 解析。"""
    frames = b""
    for _ in range(n_frames):
        header = bytes([0xFF, 0xF3, 0xC0, 0x00])         # MPEG2 L3 + CRC, 24kHz 32kbps
        frame_len = int(576 / 8.0 * 32000 / 24000)       # 已含 4 字节帧头
        frames += header + bytes(frame_len - len(header))
    return frames


def test_same_audio_content_writes_one_file(tmp_path):
    w = ChunkWriter(tmp_path)
    audio = _mp3()
    h = audio_hash(audio)
    w.write("speech", "同一句话", audio)
    w.write("speech", "同一句话（第二轮循环）", audio)
    files = list((tmp_path / "audio").glob("*.mp3"))
    assert len(files) == 1 and files[0].name == f"{h}.mp3"
    st = w.reuse_stats()
    assert st["blocks_written"] == 1 and st["blocks_reused"] == 1
    assert st["bytes_written"] == st["bytes_reused"] == len(audio)


def test_manifest_points_at_content_hashed_url(tmp_path):
    w = ChunkWriter(tmp_path)
    audio = _mp3()
    w.write("speech", "话术A", audio)
    w.write("speech", "话术B", _mp3(3))       # 不同内容 → 不同文件
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert len(m) == 2
    assert m[0]["url"].endswith(".mp3") and m[0]["h"] == audio_hash(audio)
    assert m[1]["url"] != m[0]["url"]
    # 两个地址都是可永久缓存的内容哈希地址（文件名即内容指纹）
    for e in m:
        assert "/chunks/audio/" in e["url"]
        assert (tmp_path / "audio" / e["url"].split("/")[-1]).is_file()


def test_gap_and_duration_survive_dedup(tmp_path):
    """复用文件不影响每块自己的时长与句间停顿（接缝仍按 manifest 对齐）。"""
    w = ChunkWriter(tmp_path)
    audio = _mp3()
    w.write("speech", "第一句", audio, gap_before_ms=0)
    w.write("speech", "第一句（重播）", audio, gap_before_ms=150)
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert m[0]["gap_before_ms"] == 0 and m[1]["gap_before_ms"] == 150
    assert m[0]["url"] == m[1]["url"]                     # 同一份字节
    assert m[0]["duration_ms"] == m[1]["duration_ms"] > 0  # 时长仍正确
    w.add_gap(400)
    w.write("speech", "第二句", _mp3(3))
    m2 = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert m2[2]["gap_before_ms"] == 400                  # [[pause:]] 仍生效


def test_empty_audio_does_not_create_file(tmp_path):
    w = ChunkWriter(tmp_path)
    p = w.write("speech", "dry 模式没有音频", b"")
    assert p is None
    m = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert m[0]["url"] == "" and m[0]["duration_ms"] == 0
    assert not list((tmp_path / "audio").glob("*.mp3"))
