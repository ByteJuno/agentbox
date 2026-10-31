"""
网页终端（2026-10-27）：agentbox 前端的「终端」页。每个工作空间一个 tmux 会话，直接跑在 lab 主机上，
shell 跟 SSH 登录进来的一样（登录 shell）。

- tmux 用 hub 自己的 socket（-L agent-hub，配置在 tmux.conf），跟你在 SSH 里开的 tmux 互不相干。
- tmux 服务进程用 systemd-run --scope 放进单独的 cgroup：hub 重启时 systemd 会杀掉服务里的全部进程，
  放出去之后终端里跑着的程序还在，重连回来接着用。
- 协议照 agentbox（internal/server/termws.go）：二进制帧是终端字节（两个方向都是），文本帧是 JSON 控制消息
  {"type":"resize","cols":..,"rows":..}。tmux 客户端退出（敲了 exit，或者被另一个页面顶掉）时用 1000 关闭，
  前端看到 1000 就不自动重连，免得两个页面来回抢；起不来时用 4001 关闭并带上原因，前端会把原因显示出来。
"""
import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import struct
import sys
import termios
import time

from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "tmux.conf")
# systemd 给服务进程的变量，带进 shell 里没用还会误导别的程序
DROP_ENV = {"INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MANAGERPID", "NOTIFY_SOCKET",
            "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES", "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE"}


def set_size(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def close_reason(text):
    """WebSocket 关闭原因最多 123 字节：按 UTF-8 截断，不切坏汉字。"""
    raw = text.encode()
    if len(raw) <= 120:
        return raw
    return raw[:120].decode(errors="ignore").encode()


async def write_all(loop, fd, data):
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:   # pty 缓冲满了（程序没在读）：等可写再写
            fut = loop.create_future()
            loop.add_writer(fd, lambda: fut.done() or fut.set_result(None))
            try:
                await fut
            finally:
                loop.remove_writer(fd)


class Terminals:
    def __init__(self, hub, state_dir, default_state_dir):
        state_dir = os.path.realpath(state_dir)
        # 测试实例（别的状态目录）用别的 socket，不会连到正式 hub 的终端
        suffix = "" if state_dir == os.path.realpath(default_state_dir) else \
            "-" + hashlib.sha1(state_dir.encode()).hexdigest()[:6]
        self.socket = "agent-hub" + suffix
        self.hub = hub
        self.lock = asyncio.Lock()
        self.open: set[web.WebSocketResponse] = set()

    def tmux(self, *args):
        return ["tmux", "-L", self.socket, "-f", CONF, "-u", *args]

    @staticmethod
    def env():
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_HUB") and k not in DROP_ENV}
        env.update(TERM="xterm-256color", COLORTERM="truecolor")
        env.setdefault("LANG", "C.UTF-8")
        return env

    async def run(self, *cmd):
        p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                 env=self.env())
        out, _ = await p.communicate()
        return p.returncode, out.decode(errors="replace").strip()

    @staticmethod
    def name(wid):
        return "ws-" + wid

    async def ensure(self, wid, cwd):
        """这个空间的 tmux 会话不在就建一个（后台），在就什么都不做。"""
        name = self.name(wid)
        async with self.lock:
            if (await self.run(*self.tmux("has-session", "-t", "=" + name)))[0] == 0:
                return
            create = self.tmux("new-session", "-d", "-s", name, "-c", cwd, "-x", "200", "-y", "50")
            server_up = (await self.run(*self.tmux("list-sessions")))[0] == 0
            if not server_up and shutil.which("systemd-run"):
                unit = f"{self.socket}-tmux-{int(time.time() * 1000)}"
                rc, out = await self.run("systemd-run", "--user", "--scope", "--quiet", "--collect",
                                         f"--unit={unit}", *create)
                if rc == 0:
                    return
                print(f"term: systemd-run failed ({out}); starting tmux inside the hub", file=sys.stderr, flush=True)
            rc, out = await self.run(*create)
            if rc != 0:
                raise RuntimeError(f"tmux 起不来：{out or rc}")

    async def kill(self, wid):
        """空间删掉或停掉：结束它的终端会话（里面的程序一起结束）。"""
        await self.run(*self.tmux("kill-session", "-t", "=" + self.name(wid)))

    async def has(self, wid):
        return (await self.run(*self.tmux("has-session", "-t", "=" + self.name(wid))))[0] == 0

    async def handle(self, request, w):
        loop = asyncio.get_running_loop()
        cwd = w["path"] if os.path.isdir(w["path"]) else os.path.expanduser("~")
        wsr = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await wsr.prepare(request)
        self.open.add(wsr)
        master = proc = None
        try:
            try:
                await self.ensure(w["id"], cwd)
                master, slave = os.openpty()
                set_size(master, 24, 80)
                try:
                    # setsid -c：新会话并把 pty 设成控制终端，改窗口大小时 tmux 客户端才收得到 SIGWINCH
                    proc = await asyncio.create_subprocess_exec(
                        "setsid", "-c", *self.tmux("attach-session", "-d", "-t", "=" + self.name(w["id"])),
                        stdin=slave, stdout=slave, stderr=slave, env=self.env(), cwd=cwd)
                finally:
                    os.close(slave)
            except Exception as e:
                await wsr.close(code=4001, message=close_reason(f"终端起不来：{e}"))
                return wsr
            os.set_blocking(master, False)
            out = asyncio.Queue()
            paused = False

            def readable():
                nonlocal paused
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    return
                except OSError:   # 客户端退出后读 pty 是 EIO
                    data = b""
                if not data:
                    loop.remove_reader(master)
                    out.put_nowait(None)
                    return
                out.put_nowait(data)
                if out.qsize() > 64 and not paused:   # 浏览器收得慢：先停读，让 tmux 那边自己缓着
                    paused = True
                    loop.remove_reader(master)

            async def sender():
                nonlocal paused
                while True:
                    data = await out.get()
                    if data is None:
                        await wsr.close(code=1000, message=b"process exited")
                        return
                    await wsr.send_bytes(data)
                    if paused and out.qsize() < 16:
                        paused = False
                        loop.add_reader(master, readable)

            loop.add_reader(master, readable)
            send_task = asyncio.create_task(sender())
            try:
                async for msg in wsr:
                    if msg.type == web.WSMsgType.BINARY:
                        await write_all(loop, master, msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        try:
                            ctl = json.loads(msg.data)
                        except ValueError:
                            continue
                        if isinstance(ctl, dict) and ctl.get("type") == "resize":
                            cols, rows = ctl.get("cols"), ctl.get("rows")
                            if type(cols) is not int or type(rows) is not int:
                                continue
                            if 0 < cols <= 1000 and 0 < rows <= 1000:
                                set_size(master, rows, cols)
            finally:
                send_task.cancel()
                await asyncio.gather(send_task, return_exceptions=True)
        finally:
            self.open.discard(wsr)
            if master is not None:
                loop.remove_reader(master)
                os.close(master)   # tmux 客户端收到挂断自己退出，会话留在 tmux 里
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), 3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        return wsr

    async def close_all(self):
        for wsr in list(self.open):
            await wsr.close(code=1001, message=b"hub restarting")


def register(app, api):
    hub = api.hub
    terms = hub.terminals

    async def term(request):
        w = hub.workspaces.get(request.match_info["id"])
        hub.spaces.start(w)
        return await terms.handle(request, w)

    async def on_shutdown(app):
        await terms.close_all()

    app.router.add_get("/api/sessions/{id}/term", term)
    app.on_shutdown.append(on_shutdown)
