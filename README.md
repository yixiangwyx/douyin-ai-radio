# 抖音 AI 电台直播系统（airadio）

读稿 AI 主播：把直播稿用**阿里百炼 CosyVoice** 流式合成播放，同时实时监听**抖音直播间互动**（进场/关注/点赞/礼物/评论），在**句子边界**随机定时插播互动话术。音频从扬声器外放（或网页播放页），用手机麦克风进直播间——一个人一部备用手机就能开播。

```
抖音直播间(webcast WSS) → 互动事件队列 → 调度器(句边界+随机窗口)
                                      ↑
直播稿(.txt, 带标记) → 句子编译 → 逐句 TTS(百炼) → 音频块 → 扬声器/播放页 → 开播手机麦克风
```

## 快速开始

```bash
uv sync                                   # 装依赖
export DASHSCOPE_API_KEY=sk-xxxx          # 阿里百炼 API Key

# 1) 验证直播稿标记（不连直播间）
uv run pytest tests/ -q

# 2) 全流程试运行（不合成音频，只验证采集+调度）
uv run python -m airadio --room <直播间号> --script scripts/demo_script.txt \
    --sink dry --window 8,15 --min-gap 6 --loops 1

# 3) 正式运行：合成音频写到 out/chunks，浏览器播放页外放
uv run python -m airadio --room <直播间号> --script scripts/demo_script.txt

# 4) 另一终端：播放页（手机/电脑浏览器打开 http://本机IP:8000，点"开始连续播放"）
uv run python web/serve_player.py --port 8000

# 或者本机直接外放（有 ffplay 的机器）
uv run python -m airadio --room <直播间号> --script scripts/demo_script.txt --sink ffplay
```

`--room` 填 `live.douyin.com/` 后面的数字或直播间分享链接。程序会自动：
解析真实 room_id → 生成 webcast 签名（vendor/sign.js，mini-racer）→ 连 `wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/` → gzip+protobuf 解析互动消息（房间未开播时自动轮询重试）。

## 直播稿标记语法

```text
普通的一行一句。              ← 按。！？…切句；句与句之间才是插播点
[[no interrupt]]报价、政策、金句不许被打断。[[/no interrupt]]
[[pause:800]]                ← 定点停顿 0.8 秒
[[待插:感谢{nick}的支持]]     ← 固定位置必播话术（{nick}/{gift} 占位符取最近互动）
# 小节标题                    ← 渲染成"接下来，小节标题。"
一整行不写句号就是一整句     ← 整段不可插播打断（另一种"不可插播"写法）
```

核心保证：**句子中间永远不会被插播打断**——插播只发生在句边界；保护块内的句子边界也被禁用。

## 插播调度参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--window 60,150` | 60–150s | 每个互动事件分配"计划插播时刻"=事件时间+随机(60~150s)，到点后的句边界才插 → 天然随机、不机械 |
| `--min-gap 45` | 45s | 两次插播最小间隔 |
| 礼物插队 | ≥100钻石 | 高价值礼物（火箭/嘉年华等）等 12s 左右即可插队，不等窗口 |
| 单次合并 | 3 条 | 一次最多合并 3 条互动（欢迎+关注+谢礼物连播） |
| 兜底话术 | 180s | 长时间无互动时播一段引导点赞关注的兜底话 |
| 事件时效 | 90s | 只念 90 秒内的新事件；重复事件签名去重，绝不重播 |

## 音频进直播间的三种接法（按落地成本排序）

1. **一部手机开播 + 另一台设备外放**（推荐起步）：跑本系统的电脑或**第二部手机**开播放页外放，开播手机靠近扬声器，麦克风直接收音。零硬件成本，马上能用。
2. **同一台电脑开播+播放，走虚拟声卡**：装 VB-Cable/Voicemeeter，抖音开播客户端的麦克风源选虚拟声卡，本系统 `--sink ffplay` 输出到该声卡。音质干净、无环境串音。
3. **两部手机**：一部跑系统外放、一部开播收音（或开播用手机、第二部跑播放页）。两设备物理隔离，最稳，适合长时间直播。

服务端不能把音频直接推入抖音开播端，所以"播放设备→麦克风"是正确通路；外放是最快落地，双手机是最终形态。

## 技术要点

- **互动消息**：抖音网页端直播间的 websocket 协议（webcast/im/push/v2，protobuf + gzip），事件含进场(Social)、关注、点赞、礼物、弹幕。字段号用真实抓包核对（`scripts/sample_frames.json`），协议回归测试在 `tests/test_timeline.py::test_captured_samples_parse`。
- **签名**：webcast 连接需要 sign.js 生成的 signature（`src/airadio/sign.py`，mini-racer 执行 vendor/sign.js），路线与开源 DouyinLiveWebFetcher 一致。
- **TTS**：dashscope `tts_v2.SpeechSynthesizer`，cosyvoice-v2 流式合成（一次句/插播 = 一次 task），`--voice` 换音色（longxiaochun_v2 女 / longcheng_v2 男等），`--rate` 语速 0.5–2。
- **离线可开发**：`--sink dry` 不需要百炼 Key，可完整验证采集与调度。

## 已知限制

- 抖音 webcast 是网页端逆向协议，非官方 API：抖音改版可能需要更新 room_id 解析或签名脚本（`scripts/capture_frames.py` 可重抓样本回归）。
- 直播间必须开播中；未开播时程序每 30s 自动重试解析。
- 评论插播默认截断到 40 字（避免长弹幕拖节奏），`ScheduleConfig.chat_max_len` 可调。

## 目录

```
src/airadio/     live.py(采集) timeline.py(编译+调度) tts.py(百炼) orchestrator.py(编排) player.py(输出)
web/             player.html(手机/浏览器播放页) serve_player.py
scripts/         demo_script.txt(示例稿) capture_frames.py(抓协议样本)
tests/           9 项测试：标记语法/调度语义/真实协议样本回归
vendor/          sign.js(签名脚本, 来自 DouyinLiveWebFetcher)
```
