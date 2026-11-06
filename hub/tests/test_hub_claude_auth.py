"""Claude 登录入口的离线回归：mock 掉官方 CLI，不连 Anthropic、不碰真实 ~/.claude。

AGENT_HUB_TG=0 <venv>/python -m unittest discover -s hub/tests -v
"""

import asyncio
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hub_claude_auth
import hub_files
import hub_git
import hub_skills
import hub_spaces
import hub_term
import hub_workspaces
import hubd


MOCK_CLI = r'''#!/usr/bin/env python3
import json, os, sys, time
state = os.environ["MOCK_STATE"]
mode = os.environ.get("MOCK_MODE", "ok")

def load():
    try:
        with open(state) as f:
            return json.load(f)
    except Exception:
        return {}

def save(data):
    with open(state, "w") as f:
        json.dump(data, f)

def log_code(code):
    with open(state + ".codes", "a") as f:
        f.write(code + "\n")

args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    if mode == "badstatus":
        time.sleep(30)
    if mode == "badjson":
        print("not json")
        sys.exit(1)
    data = load()
    print(json.dumps({"loggedIn": bool(data.get("loggedIn")),
                      "authMethod": "claudeai" if data.get("loggedIn") else "none",
                      "apiProvider": "firstParty",
                      "loginExpiresAt": data.get("expiresAt"),
                      "email": data.get("email") if data.get("loggedIn") else None}))
    sys.exit(0 if data.get("loggedIn") else 1)

if args[:2] == ["auth", "login"]:
    if mode == "exitearly":
        print("error: no tty; run claude auth login from a terminal")
        sys.exit(1)
    if mode == "silent":
        time.sleep(60)
        sys.exit(0)
    url = ("https://claude.com/cai/oauth/authorize?client_id=mock&state=st123"
           "&code_challenge=x&code_challenge_method=S256"
           "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
           "&response_type=code&scope=user%3Ainference")
    if mode == "evil":
        url = "https://evil.example/oauth/authorize?state=st123"
    print("Opening browser to sign in...")
    print("If the browser didn't open, visit: " + url)
    print("Paste code here if prompted > ", end="", flush=True)
    while True:
        line = sys.stdin.readline()
        if not line:
            sys.exit(1)
        code = line.strip()
        if not code:
            print("Paste code here if prompted > ", end="", flush=True)
            continue
        log_code(code)
        if code == "goodcode#st123":
            data = load()
            data["loggedIn"] = True
            data.setdefault("email", "alice@example.com")
            save(data)
            print("Login successful.")
            sys.exit(0)
        if mode == "refuse_once":
            print("Invalid code. Please make sure the full code was copied.")
            print("Paste code here if prompted > ", end="", flush=True)
            mode = "ok"
            continue
        if mode == "refused_final":
            print("That code wasn't accepted. It may be incomplete, expired, already used, or from an earlier sign-in attempt.")
            sys.exit(1)
        print("That code is not from this sign-in. Paste the code shown after opening the address above.")
        print("Paste code here if prompted > ", end="", flush=True)

sys.exit(2)
'''


class AuthFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-claude-auth-")
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.state.write_text(json.dumps({"loggedIn": False}))
        self.mock = root / "mock-claude"
        self.mock.write_text(MOCK_CLI)
        self.mock.chmod(self.mock.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.env = patch.dict(os.environ, {"MOCK_STATE": str(self.state), "MOCK_MODE": "ok"})
        self.env.start()
        self.auth = None

    async def asyncTearDown(self):
        if self.auth is not None:
            await self.auth.shutdown()
        self.env.stop()
        self.temp.cleanup()

    def make(self, **kw):
        self.auth = hub_claude_auth.ClaudeAuth(str(self.mock), **kw)
        return self.auth

    async def wait_phase(self, phase, timeout=8.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.auth.flow is not None and self.auth.flow.phase == phase:
                return self.auth.flow
            await asyncio.sleep(0.05)
        self.fail(f"flow 没有进入 {phase}，当前 {self.auth.flow and self.auth.flow.view()}")

    async def test_status_logged_out_then_masks_email(self):
        auth = self.make()
        snap = await auth.status(force=True)
        self.assertTrue(snap["available"])
        self.assertFalse(snap["loggedIn"])
        self.assertIsNone(snap["account"])
        self.state.write_text(json.dumps({"loggedIn": True, "email": "alice@example.com",
                                          "expiresAt": int(time.time() * 1000) + 3600000}))
        snap = await auth.status(force=True)
        self.assertTrue(snap["loggedIn"])
        self.assertEqual(snap["account"], "a***@example.com")
        self.assertNotIn("alice", json.dumps(snap))
        self.assertNotIn("Token", json.dumps(snap))

    async def test_start_returns_official_url_once_and_never_in_flow(self):
        auth = self.make()
        flow = await auth.start()
        self.assertTrue(flow.url.startswith("https://claude.com/cai/oauth/authorize?"))
        view = flow.view()
        self.assertNotIn("http", json.dumps(view))
        self.assertEqual(flow.phase, "waiting")
        # 第二次拿链接：必须拒绝，不能有两个登录进程
        with self.assertRaises(hub_claude_auth.AuthError) as caught:
            await auth.start()
        self.assertEqual(caught.exception.status, 409)
        await auth.cancel()

    async def test_cancel_kills_process_group(self):
        auth = self.make()
        flow = await auth.start()
        pid = flow.pid
        self.assertTrue(pid > 0)
        self.assertTrue(await auth.cancel())
        self.assertIsNotNone(flow.proc.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(await auth.cancel())

    async def test_code_success_verified_by_auth_status(self):
        auth = self.make()
        flow = await auth.start()
        await auth.submit_code("goodcode#st123")
        await self.wait_phase("succeeded")
        self.assertTrue(self.state.read_text() and json.loads(self.state.read_text())["loggedIn"])
        snap = await auth.status(force=True)
        self.assertTrue(snap["loggedIn"])
        self.assertEqual(flow.error, "")

    async def test_malformed_code_never_reaches_cli(self):
        auth = self.make()
        await auth.start()
        with self.assertRaises(hub_claude_auth.AuthError):
            await auth.submit_code("no-separator")
        self.assertEqual(auth.flow.phase, "waiting")
        self.assertFalse(Path(str(self.state) + ".codes").exists())
        await auth.cancel()

    async def test_refused_code_returns_to_waiting_and_allows_retry(self):
        os.environ["MOCK_MODE"] = "refuse_once"
        auth = self.make()
        await auth.start()
        await auth.submit_code("wrongcode#st123")
        flow = await self.wait_phase("waiting")
        self.assertIn("不属于", flow.error)
        self.assertNotIn("wrongcode", json.dumps(flow.view()))
        await auth.submit_code("goodcode#st123")
        await self.wait_phase("succeeded")

    async def test_final_refusal_fails_without_claiming_success(self):
        os.environ["MOCK_MODE"] = "refused_final"
        auth = self.make()
        await auth.start()
        await auth.submit_code("expired#st123")
        flow = await self.wait_phase("failed")
        self.assertTrue(flow.error)
        snap = await auth.status(force=True)
        self.assertFalse(snap["loggedIn"])

    async def test_start_timeout_kills_child(self):
        os.environ["MOCK_MODE"] = "silent"
        auth = self.make(start_timeout=0.4)
        with self.assertRaises(hub_claude_auth.AuthError) as caught:
            await auth.start()
        self.assertEqual(caught.exception.status, 504)
        self.assertIsNotNone(auth.flow.proc.returncode)

    async def test_unofficial_url_is_rejected_without_echo(self):
        os.environ["MOCK_MODE"] = "evil"
        auth = self.make(start_timeout=1.0)
        with self.assertRaises(hub_claude_auth.AuthError) as caught:
            await auth.start()
        self.assertNotIn("evil.example", str(caught.exception))
        self.assertNotIn("evil.example", json.dumps(auth.flow.view()))
        self.assertIsNotNone(auth.flow.proc.returncode)

    async def test_flow_ttl_expires(self):
        auth = self.make(ttl=0.4)
        flow = await auth.start()
        await self.wait_phase("failed", timeout=4)
        self.assertIn("超时", flow.error)
        self.assertIsNotNone(flow.proc.returncode)

    async def test_exit_before_url_fails(self):
        os.environ["MOCK_MODE"] = "exitearly"
        auth = self.make(start_timeout=2.0)
        with self.assertRaises(hub_claude_auth.AuthError) as caught:
            await auth.start()
        self.assertEqual(caught.exception.status, 502)

    async def test_status_unreadable_or_timeout_is_reported_not_faked(self):
        auth = self.make(status_timeout=0.5)
        os.environ["MOCK_MODE"] = "badjson"
        snap = await auth.status(force=True)
        self.assertFalse(snap["available"])
        self.assertIsNone(snap["loggedIn"])
        os.environ["MOCK_MODE"] = "badstatus"
        started = time.time()
        snap = await auth.status(force=True)
        self.assertFalse(snap["available"])
        self.assertLess(time.time() - started, 3)


class OfficialUrlTest(unittest.TestCase):
    def test_only_official_authorize(self):
        good = "https://claude.com/cai/oauth/authorize?state=x&code_challenge=y"
        self.assertEqual(hub_claude_auth.official_url(good), good)
        self.assertTrue(hub_claude_auth.official_url(
            "https://platform.claude.com/oauth/authorize?state=x"))
        for bad in (
                good.replace("https://", "http://"),
                good.replace("claude.com", "claude.com.evil.example"),
                good.replace("claude.com", "user@claude.com"),
                good.replace("claude.com", "claude.com:8443"),
                good.replace("/cai/oauth/authorize", "/cai/oauth/authorize/evil"),
                "https://claude.com/cai/oauth/authorize?client_id=1",
                "https://claude.com/other?state=x"):
            self.assertEqual(hub_claude_auth.official_url(bad), "", bad)

    def test_mask_email(self):
        self.assertEqual(hub_claude_auth.mask_email("alice@example.com"), "a***@example.com")
        self.assertEqual(hub_claude_auth.mask_email("a@b"), "a***@b")
        self.assertEqual(hub_claude_auth.mask_email(""), None)
        self.assertEqual(hub_claude_auth.mask_email("not-an-email"), "***")


class HttpGuardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-claude-http-")
        self.root = Path(self.temp.name)
        (self.root / "events").mkdir()
        self.patches = []
        for key, value in {"STATE_DIR": str(self.root), "EVENTS_DIR": str(self.root / "events"),
                           "SESSIONS_PATH": str(self.root / "sessions.json"),
                           "RECEIPTS_PATH": str(self.root / "receipts.json"),
                           "USAGE_PATH": str(self.root / "usage.jsonl")}.items():
            p = patch.object(hubd, key, value)
            p.start()
            self.patches.append(p)
        self.tg = patch.object(hubd, "load_tg", return_value=None)
        self.tg.start()
        self.state = self.root / "state.json"
        self.state.write_text(json.dumps({"loggedIn": False}))
        self.mock = self.root / "mock-claude"
        self.mock.write_text(MOCK_CLI)
        self.mock.chmod(self.mock.stat().st_mode | stat.S_IXUSR)
        self.env = patch.dict(os.environ, {"MOCK_STATE": str(self.state), "MOCK_MODE": "ok"})
        self.env.start()
        h = self.hub = hubd.Hub()
        h.workspaces = hub_workspaces.Workspaces(h, str(self.root), str(self.root), hubd.HubError)
        h.files = hub_files.Files(h, str(self.root), str(self.root), hubd.HubError)
        h.git = hub_git.Git(h, str(self.root), hubd.HubError)
        h.skills = hub_skills.Skills(h, str(self.root), str(self.root), hubd.HubError)
        h.terminals = hub_term.Terminals(h, str(self.root), str(self.root / "never-production"))
        h.cli_path = str(self.mock)
        h.spaces = hub_spaces.Spaces(h, str(self.root), str(self.root), hubd.USAGE_PATH, hubd.PRICES, hubd.HubError)
        h.workspaces.ensure(str(self.root))
        self.token = secrets.token_hex(24)
        self.client = TestClient(TestServer(hubd.http_app(h, self.token)))
        await self.client.start_server()
        self.auth = {"Authorization": "Bearer " + self.token}

    async def asyncTearDown(self):
        self.hub.stopping = True
        for task in list(self.hub.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.hub.tasks.values(), return_exceptions=True)
        await self.client.close()
        self.env.stop()
        self.tg.stop()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def test_status_requires_token_and_never_caches(self):
        response = await self.client.get("/hub/claude-auth/status")
        self.assertEqual(response.status, 401)
        response = await self.client.get("/hub/claude-auth/status", headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        data = await response.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["loggedIn"])
        self.assertIsNone(data["flow"])

    async def test_post_requires_token_or_same_origin(self):
        response = await self.client.post("/hub/claude-auth/start", json={})
        self.assertEqual(response.status, 401)
        response = await self.client.post("/hub/claude-auth/start", json={}, headers={
            "Cookie": "agent_hub=" + self.token, "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/hub/claude-auth/flow", headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["flow"], None)


if __name__ == "__main__":
    unittest.main()
