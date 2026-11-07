"""探针：web_rid → enter 接口拿 room_id → reflow 接口拿真实状态/是否语音(电台)房。

用来确认「手机电台开播」的房间在 web 接口里的真实表现：
- enter 返回的房间 status 与 reflow 的 status 是否一致（谁滞后）
- reflow 的 live_type_audio / stream_url(radio m3u8) 是否为真
"""
import json
import sys
import time
import urllib.parse
import urllib.request

import requests

sys.path.insert(0, "src")
from airadio.live import UA, _session  # noqa: E402

TOTAL = int(sys.argv[1]) if len(sys.argv) > 1 else 240
RID = sys.argv[2] if len(sys.argv) > 2 else "54235570430"


def enter(s, web_rid):
    p = {"aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
         "language": "zh-CN", "browser_language": "zh-CN", "browser_platform": "Win32",
         "browser_name": "Chrome", "browser_version": UA.split("Chrome/")[1],
         "enter_from": "page_refresh", "web_rid": web_rid}
    r = s.get("https://live.douyin.com/webcast/room/web/enter/?" + urllib.parse.urlencode(p),
             timeout=15)
    try:
        return r.json()
    except ValueError:
        return {}


def reflow(s, room_id):
    p = {"type_id": "0", "live_id": "1", "room_id": room_id, "sec_user_id": "",
         "version_code": "99.99.99", "app_id": "1128", "device_platform": "webcast"}
    r = s.get("https://webcast.amemv.com/webcast/room/reflow/info/?" + urllib.parse.urlencode(p),
             timeout=15)
    try:
        return r.json()
    except ValueError:
        return {}


def main():
    s = _session()
    s.get("https://live.douyin.com/", timeout=15)   # 预热拿 ttwid
    deadline = time.time() + TOTAL
    while time.time() < deadline:
        d = enter(s, RID)
        data = d.get("data") or {}
        rooms = data.get("data") or []
        ts = time.strftime("%H:%M:%S")
        print(f"{ts} ENTER room_status={data.get('room_status')} "
              f"enter_room_id={data.get('enter_room_id')} rooms="
              f"{[(r.get('id_str'), r.get('status')) for r in rooms]}")
        for r in rooms:
            rd = (reflow(s, r.get("id_str") or "").get("data") or {}).get("room") or {}
            su = rd.get("stream_url") or {}
            hls = (su.get("hls_pull_url") or "")
            print(f"   REFLOW {r.get('id_str')} status={rd.get('status')} "
                  f"audio={rd.get('live_type_audio')} mode={rd.get('live_room_mode')} "
                  f"users={rd.get('user_count')} total={(rd.get('stats') or {}).get('total_user')} "
                  f"created={rd.get('create_time')} finish={rd.get('finish_time')} "
                  f"radio={'/radio/' in hls} title={(rd.get('title') or '')[:20]}")
        time.sleep(20)


if __name__ == "__main__":
    main()
