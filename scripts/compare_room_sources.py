"""三路对照：页面 HTML 里的 roomId / web/enter 接口 / app 端 reflow 接口。

目的：语音(电台)房开播时，哪个途径还能拿到房间数据。
"""
import re
import sys
import time
import urllib.parse

sys.path.insert(0, "src")
from airadio.live import UA, _session  # noqa: E402

RID = sys.argv[1] if len(sys.argv) > 1 else "54235570430"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

PATTERNS = (r'\\\\"roomId\\\\":\\\\"(\d{15,20})', r'"roomId":"(\d{15,20})"',
            r'\\\\"enter_room_id\\\\":\\\\"(\d{15,20})')


def main():
    s = _session()
    s.get("https://live.douyin.com/", timeout=15)
    for i in range(ROUNDS):
        ts = time.strftime("%H:%M:%S")
        # 1) 页面 HTML
        r = s.get(f"https://live.douyin.com/{RID}", timeout=20)
        html_room = None
        for pat in PATTERNS:
            m = re.search(pat, r.text)
            if m:
                html_room = m.group(1)
                break
        page_closed = "直播已结束" in r.text or "server-error" in r.text
        # 2) web/enter
        p = {"aid": "6383", "app_name": "douyin_web", "live_id": "1", "device_platform": "web",
             "language": "zh-CN", "browser_language": "zh-CN", "browser_platform": "Win32",
             "browser_name": "Chrome", "browser_version": "141.0.0.0",
             "enter_from": "page_refresh", "web_rid": RID}
        e = s.get("https://live.douyin.com/webcast/room/web/enter/?" + urllib.parse.urlencode(p),
                  timeout=20)
        try:
            data = (e.json() or {}).get("data") or {}
        except ValueError:
            data = {}
        enter_rooms = [(x.get("id_str"), x.get("status")) for x in (data.get("data") or [])]
        # 3) reflow（对已知 room_id 校验真实状态）
        cand = html_room or (enter_rooms[0][0] if enter_rooms else "")
        reflow = {}
        if cand:
            q = {"type_id": "0", "live_id": "1", "room_id": cand, "sec_user_id": "",
                 "version_code": "99.99.99", "app_id": "1128", "device_platform": "webcast"}
            rr = s.get("https://webcast.amemv.com/webcast/room/reflow/info/?" + urllib.parse.urlencode(q),
                       timeout=20)
            try:
                reflow = (rr.json() or {}).get("data") or {}
                reflow = reflow.get("room") or {}
            except ValueError:
                reflow = {}
        print(f"{ts} HTML room={html_room} closed_text={page_closed} | "
              f"ENTER rooms={enter_rooms} room_status={data.get('room_status')} | "
              f"REFLOW status={reflow.get('status')} audio={reflow.get('live_type_audio')} "
              f"users={reflow.get('user_count')} finish={reflow.get('finish_time')}", flush=True)
        time.sleep(20)


if __name__ == "__main__":
    main()
