"""快速找几个正在直播的房间：对候选 web_rid 逐个查 status==2（live）。"""
import sys

sys.path.insert(0, "src")

from airadio.live import resolve_room  # noqa: E402

for rid in sys.argv[1:]:
    try:
        r = resolve_room(rid)
        print(f"{rid}\tstatus={r.status}\troom_id={r.room_id}\ttitle={(r.title or '')[:40]}")
    except Exception as e:
        print(f"{rid}\tERR {type(e).__name__}: {e}")
