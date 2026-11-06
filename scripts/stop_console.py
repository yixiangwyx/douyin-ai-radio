import os
import signal
import subprocess

out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
me = os.getpid()
killed = []
for line in out.splitlines():
    args = line.split(None, 1)
    if len(args) < 2:
        continue
    if not args[0].isdigit():
        continue            # 跳过 ps 的表头行
    pid = int(args[0])
    a = args[1]
    # 只杀真正的控制台进程（python + console.py），不匹配本脚本自身
    if "web/console.py" in a and ("python" in a.split()[0].lower()):
        if pid != me:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except Exception as e:
                print("err", pid, e)
print("killed:", killed)
