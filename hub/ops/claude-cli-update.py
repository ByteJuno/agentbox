#!/usr/bin/env python3
"""Claude Code CLI 自动更新（agent-hub 专用，2026-10-30）。

安装方式：ubuntu 自己的 npm 前缀（~/.npm-global），入口
/home/ubuntu/.npm-global/bin/claude → ~/.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe。
更新只走官方流程：claude update（package-manager 装法由 CLI 自己交给 npm），不 curl 任何安装脚本；
不动 OAuth 凭据（~/.claude/.credentials.json），不动 ~/.claude/settings.json 里的模型 / effort；
不发模型请求：只问 npm registry 要版本号、读 GitHub 上的 CHANGELOG。

部署：~/.config/systemd/user/claude-cli-update.{service,timer}，每 6 小时一次（ubuntu 已开 linger）。
有 agent-hub 会话处于 running/waiting、或还有 claude 子进程在跑时只检查不更新，state 记 deferred，下一轮再试。
更新成功 / 更新失败 / 连续取不到版本 时经 @BaihahaBot 私聊（/opt/homelab-report/ops_notify.py）；
版本没变不发消息。

  --dry    只检查和打印，不更新、不推送
  --force  跳过会话占用检查（人在旁边看着的时候用）
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

HOME = pathlib.Path("/home/ubuntu")
CLAUDE_BIN = HOME / ".npm-global/bin/claude"
PKG = "@anthropic-ai/claude-code"
NPM = "/usr/bin/npm"
HUB = HOME / "ai-work/agent-hub"
AGENTCTL = HUB / "agentctl"
SDK_LIB = HUB / ".venv/lib"
STATE = HOME / ".agent-hub/claude-cli-update.json"
LOG = HUB / "ops/logs/claude-cli-update.log"
LOG_MAX = 2 * 1024 * 1024
OPS_DIR = "/opt/homelab-report"
UNIT = "agent-hub.service"
ACTIVE = {"running", "waiting"}          # hubd.py 的 ACTIVE_STATES
FAIL_ALERT = 2                           # 连续查不到状态 / 版本时第几次提醒
CHANGELOG_RAW = "https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md"
CHANGELOG_PAGE = "https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md"
RELEASE_TAG = "https://github.com/anthropics/claude-code/releases/tag/v{}"
PICK = ("-p ", "print", "sdk", "resume", "effort", "compact", "headless", "usage limit",
        "rate limit", "permission", "exit code", "session", "memory", "crash", "gateway")
PICK_MAX = 8


def log(msg):
    line = "%s %s" % (time.strftime("%F %T"), msg)
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > LOG_MAX:
            os.replace(LOG, LOG.with_name(LOG.name + ".1"))
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as ex:
        print("写日志失败：%s" % type(ex).__name__, file=sys.stderr, flush=True)


def run(cmd, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def vkey(v):
    parts = re.findall(r"\d+", v or "")[:3]
    return tuple(int(p) for p in (parts + ["0", "0", "0"])[:3])


def cli_version(path=None):
    path = str(path or CLAUDE_BIN)
    out = run([path, "--version"], timeout=120)
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    m = re.match(r"(\d+\.\d+\.\d+)", text)
    if out.returncode or not m:
        raise RuntimeError("%s --version 失败（rc=%s）：%s" % (path, out.returncode, text[:200]))
    return m.group(1)


def npm_versions():
    p = run([NPM, "view", PKG, "dist-tags", "--json"], timeout=180)
    if p.returncode:
        raise RuntimeError("npm view 失败：%s" % ((p.stderr or p.stdout).strip()[:200]))
    tags = json.loads(p.stdout)
    return str(tags.get("latest") or ""), str(tags.get("stable") or "")


def whitelist(raw):
    """只取 PATH / AGENT_HUB_CLAUDE 两个键，别的一律不读不打印。"""
    env = {}
    for item in raw.split(b"\0"):
        s = item.decode("utf-8", "replace") if isinstance(item, bytes) else item
        if "=" in s:
            k, v = s.split("=", 1)
            if k in ("PATH", "AGENT_HUB_CLAUDE"):
                env[k] = v
    return env


def hub_env():
    """返回 (白名单环境, 来源说明)。

    优先读运行中 hubd 进程的 /proc/<pid>/environ —— 这是服务实际拿到的值；
    读不到再退回 unit 定义（systemctl --user show）。
    """
    notes = []
    p = run(["/usr/bin/pgrep", "-u", "ubuntu", "-f", "hubd.py"], timeout=30)
    if p.returncode == 0 and (p.stdout or "").strip():
        pid = (p.stdout or "").split()[0]
        try:
            env = whitelist(pathlib.Path("/proc/%s/environ" % pid).read_bytes())
            if env:
                return env, "运行中进程 pid=%s 的实际环境" % pid
        except OSError as ex:
            notes.append("读 /proc 失败（%s）" % type(ex).__name__)
    p = run(["systemctl", "--user", "show", UNIT, "-p", "Environment"], timeout=60)
    if p.returncode == 0:
        raw = (p.stdout.split("=", 1)[1].strip() if "=" in p.stdout else "").encode()
        env = whitelist(raw)
        for item in raw.decode().split():
            if "=" in item:
                k, v = item.split("=", 1)
                if k in ("PATH", "AGENT_HUB_CLAUDE"):
                    env[k] = v
        return env, "unit 定义（systemctl --user show）"
    raise RuntimeError("拿不到 hub 环境：%s %s" % ("；".join(notes), (p.stderr or p.stdout).strip()[:160]))


def resolved_cli(env):
    """按 hubd.py 的算法复现：AGENT_HUB_CLAUDE 优先，其次 PATH 里的 claude，最后回退 ~/.npm-global/bin/claude。"""
    pinned = env.get("AGENT_HUB_CLAUDE", "")
    if pinned:
        return pinned, "AGENT_HUB_CLAUDE（unit）"
    for d in env.get("PATH", "").split(":"):
        if not d:
            continue
        cand = pathlib.Path(d) / "claude"
        if cand.exists() and os.access(cand, os.X_OK):
            return str(cand), "PATH（unit）"
    return str(CLAUDE_BIN), "默认回退"


def bundled_cli():
    for lib in sorted(SDK_LIB.glob("python3*/site-packages/claude_agent_sdk/_bundled")):
        for name in ("claude", "claude.exe"):
            p = lib / name
            if p.exists():
                try:
                    return str(p), cli_version(p)
                except Exception as ex:                       # noqa: BLE001
                    return str(p), "取不到（%s）" % type(ex).__name__
    return "", "没有捆绑 CLI"


def sdk_cli_path(pinned):
    """用 venv 里的 SDK 按 hub 的传参方式构造 options，确认 SDK 真会执行哪条路径（不启动会话、不发请求）。"""
    code = (
        "import json\n"
        "from claude_agent_sdk import ClaudeAgentOptions\n"
        "from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport\n"
        "opts = ClaudeAgentOptions(cli_path=%r)\n"
        "t = SubprocessCLITransport('x', opts)\n"
        "print(json.dumps({'cli_path': t._cli_path, 'bundled': t._find_bundled_cli()}))\n" % pinned
    )
    p = run([str(HUB / ".venv/bin/python"), "-c", code], timeout=120)
    if p.returncode:
        tail = ((p.stderr or p.stdout or "").strip().splitlines() or ["?"])[-1]
        raise RuntimeError("SDK 解析 cli_path 失败：%s" % tail[:200])
    return json.loads(p.stdout.strip().splitlines()[-1])["cli_path"]


def live_sessions():
    """返回 (活跃会话/进程清单, 取状态失败原因)。拿不到状态时由调用方按「忙」处理。"""
    try:
        p = run([str(AGENTCTL), "--json", "list"], timeout=90)
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).strip()[:200])
        data = json.loads(p.stdout)
        live = ["%s(%s)" % (s.get("name") or str(s.get("id"))[:8], s.get("status"))
                for s in data.get("sessions", []) if s.get("status") in ACTIVE]
    except Exception as ex:                                   # noqa: BLE001
        return [], "%s: %s" % (type(ex).__name__, str(ex)[:160])
    try:
        ps = run(["/usr/bin/ps", "-eo", "args="], timeout=30)
        for line in (ps.stdout or "").splitlines():
            line = line.strip()
            if ("claude.exe" in line or "_bundled/claude" in line) and "--version" not in line:
                live.append("进程: " + line[:100])
    except Exception as ex:                                   # noqa: BLE001
        return live, "%s: %s" % (type(ex).__name__, str(ex)[:160])
    return live, ""


def changelog_between(old, new):
    try:
        req = urllib.request.Request(CHANGELOG_RAW, headers={"User-Agent": "agent-hub-claude-cli-update"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception as ex:                                   # noqa: BLE001
        return None, "CHANGELOG 取不到（%s）" % type(ex).__name__
    sections, cur, want = [], None, False
    for line in body.splitlines():
        m = re.match(r"^##\s+(v?\d[^\s]*)", line)
        if m:
            ver = m.group(1).lstrip("v")
            cur = {"version": ver, "items": []}
            want = vkey(old) < vkey(ver) <= vkey(new)
            if want:
                sections.append(cur)
            continue
        if want and line.startswith("- ") and cur is not None:
            cur["items"].append(line[2:].strip())
    total = sum(len(s["items"]) for s in sections)
    picks = []
    for s in sections:
        for item in s["items"]:
            low = item.lower()
            if any(k in low for k in PICK) and item not in picks:
                picks.append(item)
    return {"sections": sections, "total": total, "picks": picks[:PICK_MAX]}, ""


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE.with_name(STATE.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def notify(text, dry):
    if dry:
        print("---- 推送 ----\n%s\n--------------" % text, flush=True)
        return None
    if OPS_DIR not in sys.path:
        sys.path.insert(0, OPS_DIR)
    from ops_notify import send_private
    return send_private(text)


def main():
    ap = argparse.ArgumentParser(description="Claude Code CLI 检查 / 更新（无模型请求）")
    ap.add_argument("--dry", action="store_true", help="只检查打印，不更新不推送")
    ap.add_argument("--force", action="store_true", help="跳过会话占用检查")
    a = ap.parse_args()

    state = load_state()
    now = time.time()

    try:
        env, env_src = hub_env()
        resolved, source = resolved_cli(env)
        pinned = env.get("AGENT_HUB_CLAUDE", "")
        current = cli_version(CLAUDE_BIN)
        real = os.path.realpath(str(CLAUDE_BIN))
        bundled_path, bundled_ver = bundled_cli()
        latest, stable = npm_versions()
        sdk_path = sdk_cli_path(pinned) if pinned else resolved

        log("当前 %s（解析=%s，来源=%s，realpath=%s）" % (current, resolved, source, real))
        log("环境取自 %s；AGENT_HUB_CLAUDE=%s；SDK 实际 cli_path=%s" %
            (env_src, pinned or "未设置", sdk_path))
        log("SDK 捆绑 CLI %s = %s" % (bundled_path or "-", bundled_ver))
        log("npm latest=%s stable=%s" % (latest or "?", stable or "?"))

        diverged = []
        if bundled_path and vkey(bundled_ver) != vkey(current):
            diverged.append("捆绑 CLI %s ≠ 用户 CLI %s" % (bundled_ver, current))
        if os.path.realpath(resolved) != real:
            diverged.append("unit 解析到 %s，实际入口是 %s" % (resolved, real))
        if sdk_path and os.path.realpath(sdk_path) != real:
            diverged.append("SDK cli_path %s 与实际入口 %s 不一致" % (sdk_path, real))

        state.update({"current": current, "latest": latest or "?", "stable": stable or "?",
                      "path": str(CLAUDE_BIN), "realpath": real, "resolved": resolved,
                      "resolved_source": source, "env_source": env_src, "pinned": pinned,
                      "sdk_cli_path": sdk_path,
                      "bundled": bundled_ver, "diverged": diverged})

        if not latest:
            raise RuntimeError("npm 没给出 latest 版本号")
        if not pathlib.Path(resolved).exists():
            raise RuntimeError("解析出来的 CLI 路径不存在：%s" % resolved)

        if vkey(latest) <= vkey(current):
            log("已是最新（%s），不动" % current)
            state.update({"result": "up_to_date", "fail": 0, "deferred": ""})
            state.pop("error", None)
            state.pop("error_at", None)
            state.pop("deferred_since", None)
            state.pop("alerted_fail", None)
            save_state(state)
            return 0

        live, why = live_sessions()
        if why:
            log("查会话状态失败，按忙处理：%s" % why)
        if (live or why) and not a.force:
            when = state.setdefault("deferred_since", now)
            hours = (now - when) / 3600
            log("有会话在跑，本轮不更新（从 %s 起积压 %.1f 小时）：%s" %
                (time.strftime("%F %T", time.localtime(when)), hours, "、".join(live) or why))
            state.update({"result": "deferred", "latest": latest, "deferred": "、".join(live) or why,
                          "fail": 0})
            if hours >= 24 and not state.get("deferred_alert_at") and not a.dry:
                # 一直被会话挡着就别沉默：一天提醒一次，说明卡在哪
                try:
                    state["deferred_alert_at"] = now
                    state["notification"] = {"at": now, "kind": "deferred_alert", "message_id": notify(
                        ("⏳ Claude Code 已 %.1f 天没更新到 %s：%s 一直有会话在跑（%s）。"
                         "\n不打断会话所以先不动，等空闲的 6 小时窗口自动更新。日志：%s") %
                        (hours, latest, UNIT, "、".join(live) or why, LOG), False)}
                except Exception as ex:                       # noqa: BLE001
                    log("积压提醒没发出去：%s" % type(ex).__name__)
            save_state(state)
            return 0

        state.pop("deferred_since", None)
        state.pop("deferred_alert_at", None)
        state["deferred"] = ""
        if a.dry:
            log("--dry：只检查，不更新（本该 %s → %s）" % (current, latest))
            state.update({"result": "would_update", "from": current, "to": latest})
            save_state(state)
            return 0
        state["last_attempt"] = now
        log("开始更新 %s → %s（活跃会话 %d）" % (current, latest, len(live)))
        try:
            up = run([str(CLAUDE_BIN), "update"], timeout=1800)
        except subprocess.TimeoutExpired:
            raise RuntimeError("claude update 超时（30 分钟）")
        if up.returncode:
            raise RuntimeError("claude update 失败（rc=%d）：%s" %
                               (up.returncode, ((up.stdout or "") + (up.stderr or "")).strip()[-300:]))
        tail = ((up.stdout or "") + (up.stderr or "")).strip().splitlines()[-3:]
        log("claude update 输出：%s" % " / ".join(tail))

        after = cli_version(CLAUDE_BIN)
        help_out = run([str(CLAUDE_BIN), "--help"], timeout=180)
        help_ok = help_out.returncode == 0 and "Usage: claude" in ((help_out.stdout or "") + (help_out.stderr or ""))
        sdk_after = sdk_cli_path(pinned) if pinned else resolved
        real_after = os.path.realpath(str(CLAUDE_BIN))
        log("更新后 %s；--help=%s；realpath=%s；SDK cli_path=%s" %
            (after, "OK" if help_ok else "失败", real_after, sdk_after))
        problems = []
        if vkey(after) != vkey(latest):
            problems.append("版本是 %s，npm latest 是 %s" % (after, latest))
        if not help_ok:
            problems.append("claude --help 不正常")
        if not real_after.startswith(str(HOME / ".npm-global/lib/node_modules")):
            problems.append("入口指向别处：%s" % real_after)
        if problems:
            # 验收不过就不能把 hub 弄瘸：用官方 npm 包把升级前的版本装回去
            log("验收不过（%s），回滚到 %s" % ("；".join(problems), current))
            rb = run([NPM, "install", "-g", "%s@%s" % (PKG, current)], timeout=1800)
            try:
                back = cli_version(CLAUDE_BIN)
            except Exception as ex:                       # noqa: BLE001
                back = "取不到（%s）" % type(ex).__name__
            state.update({"result": "rolled_back", "rollback_to": current, "rollback_version": back,
                          "rollback_rc": rb.returncode})
            save_state(state)
            raise RuntimeError("更新后验收失败（%s）；已回滚到 %s（现为 %s，npm rc=%s）：%s" %
                               ("；".join(problems), current, back, rb.returncode,
                                ((rb.stdout or "") + (rb.stderr or "")).strip()[-200:]))

        notes, why_notes = changelog_between(current, after)
        if notes is None:
            log("改动说明未知：%s" % why_notes)
        else:
            log("changelog 区间共 %d 条，节选 %d 条" % (notes["total"], len(notes["picks"])))

        lines = ["✅ Claude Code 已更新 %s → %s" % (current, after),
                 "时间：%s（lab）" % time.strftime("%F %T"),
                 "路径：%s（npm-global，ubuntu 自己装的；SDK cli_path 也指向这里）" % CLAUDE_BIN,
                 "验收：--version=%s、--help 正常、SDK 实际 cli_path=%s" % (after, sdk_after),
                 ("没有中断会话：升级前活跃会话与 claude 子进程都是 0；每轮新起 CLI，下一轮自动用新版本，不用重启 hub"
                  if not live else
                  "注意：本次是 --force 在 %d 个活跃会话下更新的：%s" % (len(live), "、".join(live)))]
        if diverged:
            lines.append("注意：" + "；".join(diverged))
        if bundled_ver and vkey(bundled_ver) != vkey(after):
            lines.append("SDK 捆绑 CLI 仍是 %s（hub 显式传 cli_path，不用它）" % bundled_ver)
        if notes:
            vers = "、".join(s["version"] for s in notes["sections"]) or "?"
            lines.append("改动（%s 共 %d 条，节选，官方 CHANGELOG）：" % (vers, notes["total"]))
            lines += ["· " + p for p in notes["picks"]]
        else:
            lines.append("改动说明：未知（%s），只能给链接" % why_notes)
        lines.append("release：" + RELEASE_TAG.format(after))
        lines.append("changelog：" + CHANGELOG_PAGE)
        receipt = notify("\n".join(lines)[:3900], a.dry)
        state.update({"result": "updated", "from": current, "to": after, "fail": 0,
                      "notification": {"at": now, "message_id": receipt}})
        state.pop("error", None)
        state.pop("error_at", None)
        state.pop("alerted_fail", None)
        save_state(state)
        log("更新完成，已通知（message_id=%s）" % receipt)
        return 0
    except Exception as ex:                                   # noqa: BLE001
        detail = "%s: %s" % (type(ex).__name__, str(ex)[:400])
        state["fail"] = int(state.get("fail") or 0) + 1
        state.update({"result": "failed", "error": detail, "error_at": now, "last_check": now})
        log("失败（第 %d 次）：%s" % (state["fail"], detail))
        save_state(state)
        if state["fail"] >= FAIL_ALERT and state.get("alerted_fail") != state["fail"]:
            text = ("⚠️ Claude Code 检查/更新失败（第 %d 次）\n%s\n当前 %s，npm latest %s\n日志：%s" %
                    (state["fail"], detail, state.get("current", "?"), state.get("latest", "?"), LOG))
            try:
                receipt = notify(text[:3900], a.dry)
                state["alerted_fail"] = state["fail"]
                state["notification"] = {"at": now, "message_id": receipt, "kind": "error"}
                save_state(state)
            except Exception as ex2:                          # noqa: BLE001
                log("错误提醒没发出去：%s" % type(ex2).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
