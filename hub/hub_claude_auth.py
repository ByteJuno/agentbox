"""Claude 订阅登录入口（2026-11-06）：网页查看 lab 主机上 Claude Code 的登录状态，
需要时由机主本人在 Anthropic 官方页面完成授权，hub 只做「把官方链接交给你、把授权码转给 CLI」。

边界（刻意写死，不给调用方留发挥空间）：
- 只跑固定命令 [claude, auth, login, --claudeai]，PTY 交互；不接受命令、参数或任意 URL。
- 授权链接只认 claude.com /cai/oauth/authorize 与 platform.claude.com /oauth/authorize 的 https 地址；
  链接与授权码只存在内存和这一次响应里，不写日志、不落盘、不进通知。
- 授权码输入框是密码框，提交后立刻清空；不回显、不记录。状态接口只返回白名单字段，email 遮罩。
- 不 logout、不主动 refresh token、不读 ~/.claude/.credentials.json、不调用模型。
- 同一时间只允许一个登录流程；起点、等待授权码、整体有效期都有超时，取消/超时/异常都会结束整个进程组。
- 成功只以 `claude auth status --json` 的 loggedIn 为准，CLI 退出码或屏幕上的字样都不算数。
- 路由挂在 /hub/ 下，复用 hubd 现有的令牌/同源保护（读要令牌，写还要同源或 Bearer）。

环境变量：AGENT_HUB_CLAUDE 可以指定 claude 可执行文件（hubd 也用它），测试里指向 mock。
"""

import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import struct
import termios
import time
import urllib.parse

from aiohttp import web

START_TIMEOUT = 25.0        # 等 CLI 打出官方链接
STATUS_TIMEOUT = 15.0       # claude auth status --json
CODE_TIMEOUT = 120.0        # 提交授权码后等 CLI 给结果
FLOW_TTL = 600.0            # 一条登录链接最多活 10 分钟
KEEP_ENDED = 120.0          # 结束的流程在内存里留两分钟，够页面刷新到结果
STATUS_CACHE = 3.0          # 状态缓存，避免页面轮询把 CLI 起爆
BUF_MAX = 256 * 1024

# 官方 OAuth authorize 的两个入口（CLI 自己会用第一个；platform 那个给别的登录方式）
OFFICIAL = {
    "claude.com": ("/cai/oauth/authorize",),
    "platform.claude.com": ("/oauth/authorize",),
}
URL_RE = re.compile(r"https://[^\s\x1b\x07\"'<>]+")
CODE_RE = re.compile(r"^[\x21-\x7e]{8,2048}$")   # 可见 ASCII，cli 要求 code#state

# CLI 在“授权码不对”时仍停在输入提示上，可以重贴；出现下面这些词就退回等待状态。
INVALID_CODE = "Invalid code."
WRONG_SIGN_IN = "not from this sign-in"
CODE_REFUSED = ("wasn't accepted", "was not accepted")


class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def mask_email(value):
    """a***@example.com；不是邮箱就整体藏掉。"""
    text = str(value or "").strip()
    if "@" not in text:
        return "***" if text else None
    local, domain = text.split("@", 1)
    if not domain:
        return "***"
    return (local[:1] or "*") + "***@" + domain


def official_url(url):
    """只放行官方 authorize 链接；不合格返回空串，调用方不得把原 URL 写进错误。"""
    if not url or len(url) > 4096:
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in OFFICIAL:
            return ""
        if parts.username or parts.password or parts.port not in (None, 443):
            return ""
        if parts.path not in OFFICIAL[parts.hostname]:
            return ""
        if not urllib.parse.parse_qs(parts.query).get("state"):
            return ""
    except ValueError:
        return ""
    # 末尾的标点来自终端句子，不属于 URL。
    return url.rstrip(".,;:!?)]}'")


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_HUB")}
    env.update(TERM="xterm-256color", NO_COLOR="1", BROWSER="/bin/false")
    env.setdefault("LANG", "C.UTF-8")
    return env


def _set_size(fd, rows=40, cols=120):
    try:
        import fcntl
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (ImportError, OSError):
        pass


class _Flow:
    """一次 claude auth login 子进程 + 它的 PTY 输出缓冲。"""

    def __init__(self, auth, cli):
        self.auth = auth
        self.cli = cli
        self.id = secrets.token_hex(8)
        self.phase = "starting"
        self.started = time.time()
        self.expires = self.started + auth.ttl
        self.url = ""            # 只在内存；start 响应里交给页面一次
        self.code_prompt = False
        self.error = ""
        self.proc = None
        self.pid = 0
        self.master = None
        self.buf = bytearray()
        self.scan = 0
        self.eof = False
        self.event = asyncio.Event()
        self.exit_event = asyncio.Event()
        self.tasks = set()

    # ---- 进程和 PTY ----

    async def spawn(self):
        master, slave = os.openpty()
        _set_size(master)
        try:
            self.proc = await asyncio.create_subprocess_exec(
                self.cli, "auth", "login", "--claudeai",
                stdin=slave, stdout=slave, stderr=slave, env=_env(),
                cwd=os.path.expanduser("~"), start_new_session=True)
        finally:
            os.close(slave)   # 子进程已经 dup 走，父进程这端关掉才不会一直挂住
        self.master = master
        self.pid = self.proc.pid
        os.set_blocking(master, False)
        asyncio.get_running_loop().add_reader(master, self._readable)
        self.tasks.add(asyncio.create_task(self._wait_exit()))

    def _readable(self):
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:   # 子进程退出后读 pty 是 EIO
            data = b""
        if data:
            self.buf.extend(data)
            if len(self.buf) > BUF_MAX:   # 不保留整段会话，只留最近一半
                cut = len(self.buf) - BUF_MAX // 2
                del self.buf[:cut]
                self.scan = max(0, self.scan - cut)
        else:
            self.eof = True
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except (KeyError, ValueError):
                pass
        self.event.set()

    async def _wait_exit(self):
        try:
            await self.proc.wait()
        finally:
            self.exit_event.set()
            self.event.set()

    def text(self, since=0):
        return bytes(self.buf[since:]).decode("utf-8", "replace")

    def find_url(self):
        for raw in URL_RE.findall(self.text()):
            url = official_url(raw.rstrip(".,;:!?)]}'"))
            if url:
                return url
        return ""

    async def write_code(self, code):
        """把授权码写进 pty；先关终端回显，免得码被 CLI 原样打回缓冲。"""
        if self.master is None:
            raise RuntimeError("no pty")
        try:
            attrs = termios.tcgetattr(self.master)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(self.master, termios.TCSANOW, attrs)
        except (termios.error, OSError):
            pass
        view = memoryview((code + "\r").encode())
        loop = asyncio.get_running_loop()
        while view:
            try:
                view = view[os.write(self.master, view):]
            except BlockingIOError:
                fut = loop.create_future()
                loop.add_writer(self.master, lambda: fut.done() or fut.set_result(None))
                try:
                    await fut
                finally:
                    loop.remove_writer(self.master)

    def close(self):
        if self.master is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.master)
            except (KeyError, ValueError):
                pass
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None

    async def kill(self):
        proc = self.proc
        if proc is not None and proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    pass
        self.close()
        for task in list(self.tasks):
            if task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*[t for t in self.tasks if t is not asyncio.current_task()],
                             return_exceptions=True)

    def view(self):
        """给页面的流程视图：永远不含链接、授权码。"""
        return {"id": self.id, "phase": self.phase,
                "startedAt": int(self.started * 1000), "expiresAt": int(self.expires * 1000),
                "codePrompt": self.code_prompt, "error": self.error}


class ClaudeAuth:
    def __init__(self, cli=None, *, start_timeout=START_TIMEOUT, status_timeout=STATUS_TIMEOUT,
                 code_timeout=CODE_TIMEOUT, ttl=FLOW_TTL):
        self.cli = cli
        self.start_timeout = start_timeout
        self.status_timeout = status_timeout
        self.code_timeout = code_timeout
        self.ttl = ttl
        self.flow = None
        self._lock = asyncio.Lock()
        self._status_lock = asyncio.Lock()
        self._status_cache = (0.0, None)
        self._reaps = set()

    # ---- 找 CLI / 读状态 ----

    def _cli(self):
        candidates = [self.cli, os.environ.get("AGENT_HUB_CLAUDE"), shutil.which("claude"),
                      os.path.expanduser("~/.npm-global/bin/claude")]
        for path in candidates:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return os.path.realpath(path)
        return None

    async def _run_status(self):
        cli = self._cli()
        checked = int(time.time() * 1000)
        if not cli:
            return {"available": False, "loggedIn": None, "authMethod": None, "loginExpiresAt": None,
                    "account": None, "checkedAt": checked, "error": "hub 上找不到 claude 命令"}
        try:
            proc = await asyncio.create_subprocess_exec(
                cli, "auth", "status", "--json",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=_env(), cwd=os.path.expanduser("~"))
        except OSError:
            return {"available": False, "loggedIn": None, "authMethod": None, "loginExpiresAt": None,
                    "account": None, "checkedAt": checked, "error": "启动 claude 失败"}
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.status_timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return {"available": False, "loggedIn": None, "authMethod": None, "loginExpiresAt": None,
                    "account": None, "checkedAt": checked, "error": "读取登录状态超时"}
        try:
            data = json.loads(out.decode("utf-8", "replace"))
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return {"available": False, "loggedIn": None, "authMethod": None, "loginExpiresAt": None,
                    "account": None, "checkedAt": checked, "error": "登录状态不是可读的 JSON"}
        expires = data.get("loginExpiresAt")
        if isinstance(expires, bool):
            expires = None
        elif isinstance(expires, (int, float)):
            expires = int(expires * 1000) if expires < 100000000000 else int(expires)
        elif isinstance(expires, str):
            expires = expires[:40]
        else:
            expires = None
        # 只认白名单字段；凭据、组织、token 一律不出这个函数。
        return {"available": True, "loggedIn": bool(data.get("loggedIn")),
                "authMethod": str(data.get("authMethod") or "none")[:32],
                "loginExpiresAt": expires,
                "account": mask_email(data.get("email") or data.get("accountEmail")),
                "checkedAt": checked, "error": ""}

    async def status(self, force=False):
        async with self._status_lock:
            now = time.time()
            cached_at, cached = self._status_cache
            if not force and cached and now - cached_at < STATUS_CACHE:
                return dict(cached)
            snap = await self._run_status()
            self._status_cache = (now, snap)
            return dict(snap)

    # ---- 流程 ----

    def _active(self):
        flow = self.flow
        return flow if flow is not None and flow.phase in ("starting", "waiting", "verifying") else None

    def flow_view(self):
        return self.flow.view() if self.flow is not None else None

    async def _wait(self, flow, predicate, timeout):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if predicate():
                return True
            remain = deadline - asyncio.get_running_loop().time()
            if remain <= 0:
                return False
            flow.event.clear()
            try:
                await asyncio.wait_for(flow.event.wait(), min(remain, 0.25))
            except asyncio.TimeoutError:
                pass

    async def _finish(self, flow, phase, error=""):
        if self.flow is not flow or flow.phase in ("cancelled", "succeeded", "failed", "expired"):
            return
        flow.phase = phase
        flow.error = error
        await flow.kill()
        self._reap(flow)

    async def _fail(self, flow, message):
        await self._finish(flow, "failed", message)

    def _reap(self, flow):
        async def clear():
            await asyncio.sleep(KEEP_ENDED)
            if self.flow is flow and flow.phase not in ("starting", "waiting", "verifying"):
                self.flow = None
        task = asyncio.create_task(clear())
        self._reaps.add(task)
        task.add_done_callback(self._reaps.discard)

    async def start(self):
        async with self._lock:
            if self._active():
                raise AuthError("已经有一个登录流程在进行，请先取消或等它结束", 409)
            cli = self._cli()
            if not cli:
                raise AuthError("hub 上找不到 claude 命令，无法发起登录", 503)
            flow = _Flow(self, cli)
            self.flow = flow
        try:
            await flow.spawn()
        except Exception:
            flow.phase = "failed"
            flow.error = "登录进程没有起来"
            self._reap(flow)
            raise AuthError("登录进程没有起来，请稍后重试", 503)

        try:
            await self._wait(flow, lambda: bool(flow.find_url()) or flow.exit_event.is_set(),
                             self.start_timeout)
            if flow.phase == "cancelled":
                raise AuthError("登录已取消", 409)
            url = flow.find_url()
            if not url:
                if flow.exit_event.is_set():
                    raise AuthError("登录进程提前退出，没有给出官方链接", 502)
                raise AuthError("官方登录链接没有在预期时间内出现，请重试", 504)
            flow.url = url
            flow.code_prompt = "paste code" in flow.text().lower()
            flow.phase = "waiting"
            if self.flow is not flow or flow.phase != "waiting":
                raise AuthError("登录已取消", 409)
            flow.tasks.add(asyncio.create_task(self._watch(flow)))
            return flow
        except AuthError as e:
            await self._fail(flow, str(e))
            raise
        except Exception:
            await self._fail(flow, "发起登录失败，请重试")
            raise AuthError("发起登录失败，请重试", 502)

    async def submit_code(self, code):
        async with self._lock:
            flow = self._active()
            if flow is None:
                raise AuthError("没有正在进行的登录，请先获取登录链接", 409)
            if flow.phase != "waiting":
                raise AuthError("登录流程正在处理中，请稍候", 409)
            value = (code or "").strip()
            if not CODE_RE.match(value) or "#" not in value:
                raise AuthError("授权码格式不对：请粘贴完整的一行（通常是 code#state），不要带空格或换行", 400)
            flow.scan = len(flow.buf)
            try:
                await flow.write_code(value)
            except Exception:
                await self._fail(flow, "授权码没能送进登录进程，请重新获取链接")
                raise AuthError("授权码没能送进登录进程，请重新获取链接", 502)
            value = ""   # 不留在栈上更久
            flow.phase = "verifying"
            flow.error = ""
            flow.tasks.add(asyncio.create_task(self._verify(flow)))
            return flow

    async def _verify(self, flow):
        deadline = time.time() + self.code_timeout
        while self.flow is flow and flow.phase == "verifying":
            if flow.exit_event.is_set():
                await self._verify_status(flow)
                return
            text = flow.text(flow.scan)
            if INVALID_CODE in text or WRONG_SIGN_IN in text:
                flow.phase = "waiting"   # CLI 还停在提示上，可以重贴
                flow.error = "授权码不属于这次登录或复制不完整，请重新粘贴完整的 code#state"
                return
            if any(word in text for word in CODE_REFUSED):
                await self._fail(flow, "授权码没有被接受（可能已过期或已经用过），请重新获取登录链接")
                return
            if time.time() >= deadline:
                await self._fail(flow, "等待授权结果超时，请重新获取登录链接")
                return
            await self._sleep_event(flow, 0.25)

    async def _verify_status(self, flow):
        snap = await self.status(force=True)
        if snap.get("available") and snap.get("loggedIn"):
            await self._finish(flow, "succeeded", "")
        else:
            await self._fail(flow, "登录进程已结束，但 auth status 还没显示已登录；请刷新状态确认")

    async def _watch(self, flow):
        remaining = max(0.0, flow.expires - time.time())
        try:
            await asyncio.wait_for(flow.exit_event.wait(), remaining)
        except asyncio.TimeoutError:
            await self._fail(flow, "登录超时，请重新获取登录链接")
            return
        if flow.phase == "waiting":
            await self._fail(flow, "登录进程已退出，请重新获取登录链接")

    async def _sleep_event(self, flow, seconds):
        flow.event.clear()
        try:
            await asyncio.wait_for(flow.event.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def cancel(self):
        async with self._lock:
            flow = self.flow
            if flow is None or flow.phase in ("cancelled", "succeeded", "failed", "expired"):
                return False
            flow.phase = "cancelled"
            flow.error = "已取消"
        await flow.kill()
        self._reap(flow)
        return True

    async def shutdown(self):
        flow = self.flow
        if flow is not None:
            await flow.kill()
        for task in list(self._reaps):
            task.cancel()
        await asyncio.gather(*self._reaps, return_exceptions=True)
        self._reaps.clear()


def register(app, api):
    auth = ClaudeAuth(getattr(api.hub, "cli_path", None))
    api.hub.claude_auth = auth

    def reply(data):
        return web.json_response({"ok": True, **data}, dumps=lambda o: json.dumps(o, ensure_ascii=False),
                                 headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def status(request):
        snap = await auth.status(force=request.query.get("force") == "1")
        return reply({**snap, "flow": auth.flow_view()})

    async def flow_view(request):
        return reply({"flow": auth.flow_view()})

    async def start(request):
        try:
            flow = await auth.start()
        except AuthError as e:
            raise api.Error(str(e), e.status)
        # 链接只在这里出现一次；页面拿到后放在内存里，不写 localStorage。
        return reply({"flow": flow.view(), "authorizeUrl": flow.url})

    async def code(request):
        body = await api.body(request)
        try:
            flow = await auth.submit_code(body.get("code"))
        except AuthError as e:
            raise api.Error(str(e), e.status)
        return reply({"flow": flow.view()})

    async def cancel(request):
        cancelled = await auth.cancel()
        return reply({"cancelled": cancelled, "flow": auth.flow_view()})

    router = app.router
    router.add_get("/hub/claude-auth/status", status)
    router.add_get("/hub/claude-auth/flow", flow_view)
    router.add_post("/hub/claude-auth/start", start)
    router.add_post("/hub/claude-auth/code", code)
    router.add_post("/hub/claude-auth/cancel", cancel)

    async def on_shutdown(app):
        await auth.shutdown()

    app.on_shutdown.append(on_shutdown)
