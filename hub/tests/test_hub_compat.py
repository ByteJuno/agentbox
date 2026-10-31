"""真实 aiohttp HTTP/WS + 原回合准入的合成回归；绝不连接 Claude、不读写真实 home。

AGENT_HUB_MAX_TURNS=30 AGENT_HUB_TG=0 <venv>/python -m unittest discover -s hub/tests -v
"""
import asyncio
import json
import os
import secrets
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web, FormData, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hubd
import hub_abx
import hub_files
import hub_git
import hub_skills
import hub_spaces
import hub_term
import hub_theme
import hub_workspaces


class CompatTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-compat-")
        self.root = Path(self.temp.name)
        (self.root / "events").mkdir()
        (self.root / "workspace").mkdir()
        (self.root / "other").mkdir()
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
        self.hub = h = hubd.Hub()
        h.workspaces = hub_workspaces.Workspaces(h, str(self.root), str(self.root), hubd.HubError)
        h.files = hub_files.Files(h, str(self.root), str(self.root), hubd.HubError)
        h.git = hub_git.Git(h, str(self.root), hubd.HubError)
        h.skills = hub_skills.Skills(h, str(self.root), str(self.root), hubd.HubError)
        h.terminals = hub_term.Terminals(h, str(self.root), str(self.root / "never-production"))
        h.terminals.kill = AsyncMock()
        h.cli_path = "/bin/echo"
        h.spaces = hub_spaces.Spaces(h, str(self.root), str(self.root), hubd.USAGE_PATH, hubd.PRICES, hubd.HubError)
        self.w = h.workspaces.ensure(str(self.root / "workspace"))
        self.other = h.workspaces.ensure(str(self.root / "other"))
        self.gates = {}
        self.executions = []
        self.finished = {}
        h.run_turn = self.fake_turn
        self.token = secrets.token_hex(24)
        self.app = hubd.http_app(h, self.token)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.auth = {"Authorization": "Bearer " + self.token}
        self.base = "/api/sessions/" + self.w["id"]
        self.c = h.abx

    async def asyncTearDown(self):
        self.hub.stopping = True
        for t in list(self.hub.tasks.values()):
            t.cancel()
        await asyncio.gather(*self.hub.tasks.values(), return_exceptions=True)
        await self.client.close()
        self.tg.stop()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def fake_turn(self, s, prompt, quiet, turn_id=""):
        self.executions.append((s.id, prompt, s.model, s.effort))
        gate = self.gates[s.id] = asyncio.Event()
        self.finished[s.id] = asyncio.Event()
        turn = hubd.Turn(sent_at=time.time(), id=turn_id)
        self.hub.turns[s.id] = turn
        async def interrupt():
            turn.user_interrupted = True
            self.hub.forward_raw(s, "result", {"type": "result", "subtype": "error_during_execution", "is_error": True})
            gate.set()
        self.hub.active[s.id] = types.SimpleNamespace(interrupt=interrupt)
        try:
            await gate.wait()
            status = "interrupted" if turn.user_interrupted else "idle"
            self.hub.touch(s, status=status, started=True, turns=s.turns + 1)
            self.hub.emit(s, "result", status=status, error="", cost=self.hub.ledger.turn_cost(s.id, turn_id) or 0, secs=1)
        finally:
            self.hub.active.pop(s.id, None)
            self.hub.turns.pop(s.id, None)
            self.finished[s.id].set()

    async def request(self, method, path, expected=200, **kw):
        kw.setdefault("headers", self.auth)
        response = await self.client.request(method, path, **kw)
        self.assertEqual(response.status, expected, await response.text() if response.status != expected else "")
        return await response.json()

    def input(self, tid=None, **kw):
        return {"scope": self.c.chat_scope, "thread_id": tid or self.c.active(self.w), "text": "test prompt",
                "model": "claude-opus-5-5", "effort": "high", "effort_control": "effort", "questions": True, **kw}

    async def send(self, tid=None, rid=None, expected=202, **kw):
        rid = rid or secrets.token_hex(16)
        result = await self.request("PUT", self.base + "/chat/requests/" + rid, expected,
                                    json=self.input(tid, **kw))
        await asyncio.sleep(0)
        return result

    async def complete(self, tid):
        self.gates[tid].set()
        await self.finished[tid].wait()
        # 回合 done callback、队列调度需要两个事件循环机会。
        for _ in range(5):
            await asyncio.sleep(0)

    async def frame(self, ws, kind):
        for _ in range(30):
            frame = await ws.receive_json(timeout=2)
            if frame.get("type") == kind:
                return frame
        self.fail("missing WS frame: " + kind)

    async def test_multithread_snapshot_routing_and_reload(self):
        a = self.c.active(self.w)
        await self.send(a)
        b = (await self.request("POST", self.base + "/chat/threads"))["id"]
        await self.send(b, text="second")
        ws = await self.client.ws_connect(self.base + "/chat", headers=self.auth)
        snapshot = await ws.receive_json()
        self.assertEqual(set(snapshot["threads"]), {a, b})
        self.assertEqual(snapshot["max_parallel"], 3)
        hist = await self.request("GET", self.base + "/history?draft_context=1&thread=" + a)
        self.assertEqual(hist["active_thread"], a)
        self.assertEqual(hist["thread"]["id"], a)
        self.assertEqual(self.c.active(self.w), b)
        event = {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer A"}]}}
        self.hub.emit(self.hub.sessions[a], "agent_event", event=event)
        frame = await self.frame(ws, "agent_event")
        self.assertEqual(frame["thread"], a)
        self.assertEqual(frame["event"], event)
        await self.complete(a)
        frame = await self.frame(ws, "turn_cost")
        self.assertEqual(frame["cost"]["cost_micro_usd"], 0)
        self.assertFalse(frame["running"])
        self.assertEqual((await self.frame(ws, "status"))["state"], "idle")
        await ws.close()

    async def test_send_to_nonactive_thread_and_parallel_queue(self):
        a = self.c.active(self.w)
        await self.send(a)
        b = (await self.request("POST", self.base + "/chat/threads"))["id"]
        await self.request("PUT", "/api/settings", json={"max_parallel_chats": 1})
        err = await self.send(b, expected=409)
        self.assertEqual(err["code"], "chat_parallel_limit")
        queued = (await self.send(b, delivery="queue"))["receipt"]
        self.assertEqual(queued["state"], "queued")
        self.assertTrue(queued["turn_id"])
        await self.complete(a)
        self.assertEqual([x[0] for x in self.executions], [a, b])
        self.assertEqual(self.c.receipts.items[queued["request_id"]]["state"], "running")
        self.assertEqual(self.c.receipts.items[queued["request_id"]]["turn_id"], queued["turn_id"])
        # A 不是服务端 active，也能继续发；不会改掉 B 的活动指针。
        q = (await self.send(a, delivery="queue", text="A followup", effort="low"))["receipt"]
        await self.complete(b)
        self.assertEqual(self.executions[-1][0:2], (a, "A followup"))
        self.assertEqual(self.executions[-1][-1], "low")
        self.assertEqual(self.c.active(self.w), b)
        self.assertEqual(self.c.receipts.items[q["request_id"]]["state"], "running")

    async def test_request_id_replay_conflict_and_abandon_fence(self):
        rid = secrets.token_hex(16)
        first = await self.send(rid=rid)
        again = await self.send(rid=rid, expected=200)
        self.assertTrue(again["replayed"])
        self.assertEqual(first["receipt"]["turn_id"], again["receipt"]["turn_id"])
        self.assertEqual(len(self.executions), 1)
        self.assertEqual((await self.send(rid=rid, text="changed", expected=409))["code"], "chat_request_conflict")
        unknown = secrets.token_hex(16)
        await self.request("POST", self.base + "/chat/requests/" + unknown,
                           json={"action": "abandon", "scope": self.c.chat_scope})
        self.assertEqual((await self.send(rid=unknown, expected=410))["code"], "chat_request_gone")

    async def test_queue_withdraw_replay_keeps_original_input(self):
        await self.send()
        queued = (await self.send(delivery="queue", text="take back"))["receipt"]
        path = self.base + "/chat/requests/" + queued["request_id"]
        body = {"scope": self.c.chat_scope, "action": "withdraw", "revision": queued["revision"]}
        withdrawn = (await self.request("POST", path, json=body))["receipt"]
        self.assertEqual(withdrawn["request"]["text"], "take back")
        self.assertEqual(withdrawn["state"], "abandoned")
        self.assertEqual((await self.request("POST", path, json=body))["receipt"], withdrawn)

    async def test_uncertain_blocks_thread_and_review_releases_queue(self):
        a = self.c.active(self.w)
        first = (await self.send(a))["receipt"]
        await self.complete(a)
        self.c.receipt_set(first["request_id"], state="uncertain", error_code="execution_incomplete")
        r = self.c.receipts.items[first["request_id"]]
        queued = (await self.send(a, delivery="queue", text="after review"))["receipt"]
        await self.send(a, expected=409)
        self.assertEqual(len(self.executions), 1)
        ws = await self.client.ws_connect(self.base + "/chat", headers=self.auth)
        await ws.receive_json()
        await ws.send_json({"type": "user_message", "thread": a, "text": "cannot bypass review"})
        await self.frame(ws, "error")
        self.assertEqual(len(self.executions), 1)
        await ws.close()
        path = self.base + "/chat/requests/" + first["request_id"]
        await self.request("POST", path, 409, json={"scope": self.c.chat_scope, "action": "review", "revision": 0})
        body = {"scope": self.c.chat_scope, "action": "review", "revision": r["revision"]}
        await self.request("POST", path, json=body)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.executions[-1][1], "after review")
        self.assertNotEqual(self.c.receipts.items[queued["request_id"]]["state"], "queued")
        await self.request("POST", path, json=body)

    async def test_restart_and_retention_keep_replay_fences(self):
        r = (await self.send())["receipt"]
        recovered = hub_abx.Receipts(self.c.receipts.path)
        self.assertEqual(recovered.items[r["request_id"]]["state"], "uncertain")
        self.assertEqual(recovered.items[r["request_id"]]["error_code"], "server_restarted")
        recovered.items["a" * 32] = {**r, "request_id": "a" * 32, "state": "completed", "created_at": "2000-01-01"}
        recovered.save()
        self.assertIn("a" * 32, recovered.items)
        self.assertIsNone(recovered.items["a" * 32]["request"])

    async def test_all_pendings_and_preferred_thread(self):
        a = (await self.send())["receipt"]
        b = (await self.request("POST", self.base + "/chat/threads"))["id"]
        await self.send(b)
        out = await self.request("GET", self.base + "/chat/requests?pending_only=1&thread=" + a["thread_id"])
        self.assertEqual(len(out["pendings"]), 2)
        self.assertEqual(out["pending"]["request_id"], a["request_id"])
        self.assertEqual(out["requests"], [])

    async def test_history_window_cursors_context_and_stable_append(self):
        r = (await self.send())["receipt"]
        s = self.hub.sessions[r["thread_id"]]
        for i in range(650):
            self.hub.emit(s, "agent_event", event={"type": "assistant", "message": {"content": [{"type": "text", "text": str(i)}]}})
        latest = await self.request("GET", self.base + "/history")
        self.assertGreater(latest["start"], 0)
        self.assertEqual(latest["entries"][0]["kind"], "turn_context")
        self.assertLessEqual(len(latest["entries"]), 601)
        older = await self.request("GET", self.base + "/history?thread=" + s.id + "&before=" + str(latest["start"]))
        self.assertEqual(older["start"], 0)
        self.assertEqual(older["entries"][0]["kind"], "user")
        window = await self.request("GET", self.base + "/history?thread=" + s.id + "&after=0&until=" + str(latest["start"]))
        self.assertEqual(window["end"], latest["start"])
        self.assertTrue(window["has_newer"])
        self.hub.emit(s, "agent_event", event={"type": "assistant", "message": {"content": []}})
        refresh = await self.request("GET", self.base + "/history?thread=" + s.id + "&from=0")
        self.assertEqual(refresh["start"], 0)
        self.assertEqual(refresh["entries"][:len(older["entries"])], older["entries"])
        self.assertEqual(refresh["entries"][0]["turn"]["id"], r["turn_id"])
        await self.request("GET", self.base + "/history?thread=" + s.id + "&after=-1", 400)
        await self.request("GET", self.base + "/history?thread=" + s.id + "&before=9999", 400)

    async def test_live_tool_thinking_history_and_virtual_paths(self):
        r = (await self.send())["receipt"]
        s = self.hub.sessions[r["thread_id"]]
        ws = await self.client.ws_connect(self.base + "/chat", headers=self.auth)
        await ws.receive_json()
        delta = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "think"}}}
        self.hub.forward_raw(s, "stream_event", delta)
        self.assertEqual((await self.frame(ws, "agent_event"))["event"], delta)
        assistant = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "think"},
                    {"type": "tool_use", "id": "tool-a", "name": "Read", "input": {"file_path": s.cwd + "/x.py"}}]}}
        tool = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-a", "content": "hi"}]},
                "tool_use_result": {"file": {"content": "hi", "numLines": 1}}}
        self.hub.forward_raw(s, "assistant", assistant)
        frame = await self.frame(ws, "agent_event")
        self.assertEqual(frame["event"]["message"]["content"][1]["input"]["file_path"], "/workspace/x.py")
        self.hub.forward_raw(s, "user", tool)
        self.assertEqual((await self.frame(ws, "agent_event"))["event"]["tool_use_result"], tool["tool_use_result"])
        now = time.time()
        self.hub.record_usage(s, self.hub.turns[s.id], {"start": now, "first": now, "model": "claude-opus-5-5",
                             "usage": {"input_tokens": 10, "output_tokens": 10}}, None)
        cost = await self.frame(ws, "turn_cost")
        self.assertTrue(cost["running"])
        self.assertGreater(cost["cost"]["cost_micro_usd"], 0)
        hist = await self.request("GET", self.base + "/history")
        self.assertEqual([e["event"]["type"] for e in hist["entries"] if e["kind"] == "event"], ["assistant", "user"])
        self.assertEqual(sum(e["kind"] == "user" for e in hist["entries"]), 1)
        await ws.close()

    async def test_cross_workspace_thread_and_question_access(self):
        r = (await self.send())["receipt"]
        tid = r["thread_id"]
        base = "/api/sessions/" + self.other["id"]
        await self.request("POST", base + "/chat/threads/" + tid + "/activate", 404)
        await self.request("PATCH", base + "/chat/threads/" + tid, 404, json={"title": "wrong"})
        await self.request("DELETE", base + "/chat/threads/" + tid, 404)
        await self.request("GET", base + "/chat/tasks?thread=" + tid, 404)
        waiter = hubd.Waiter("qa", tid, "question", "AskUserQuestion", "", "", "", [], "", time.time(), asyncio.get_running_loop().create_future())
        self.hub.waiters["qa"] = waiter
        await self.request("POST", base + "/chat/questions/qa", 409, json={"skip": True})
        self.assertFalse(waiter.future.done())

    async def test_stop_pauses_queue_and_finalizes_interrupted(self):
        r = (await self.send())["receipt"]
        q = (await self.send(delivery="queue", text="do later"))["receipt"]
        await self.request("POST", self.base + "/chat/requests/" + r["request_id"],
                           json={"scope": self.c.chat_scope, "action": "interrupt", "revision": r["revision"]})
        await self.finished[r["thread_id"]].wait()
        for _ in range(4):
            await asyncio.sleep(0)
        self.assertEqual(self.c.receipts.items[r["request_id"]]["state"], "interrupted")
        self.assertEqual(self.c.receipts.items[q["request_id"]]["state"], "queued")
        self.assertEqual(len(self.executions), 1)
        history = await self.request("GET", self.base + "/history")
        self.assertEqual(next(e for e in history["entries"] if e.get("event", {}).get("type") == "result")["state"], "interrupted")

    async def test_scope_and_type_validation_problem_contract(self):
        for changes in ({"scope": "bad"}, {"effort": []}, {"questions": 1}, {"attachments": ["/etc/passwd"]}, {"model": "other-model"}):
            b = self.input(**changes)
            response = await self.client.put(self.base + "/chat/requests/" + secrets.token_hex(16), headers=self.auth, json=b)
            self.assertIn(response.status, (400, 409))
            data = await response.json()
            self.assertEqual(data["operation_id"], response.headers["X-Agentbox-Operation-ID"])
            self.assertIsInstance(data["retryable"], bool)
            self.assertIn("action", data)
        self.assertEqual(self.executions, [])

    async def test_features_off_and_settings_shapes(self):
        for suffix in ("/client-terminals", "/client-projects", "/git/bindings"):
            self.assertEqual(await self.request("GET", self.base + suffix), [])
        self.assertEqual((await self.request("GET", "/api/delegation/stats"))["stats"], [])
        self.assertEqual((await self.request("GET", "/api/marketplace"))["plugins"], [])
        self.assertEqual((await self.request("GET", "/api/git/operations"))["active"], [])
        self.assertEqual((await self.request("GET", "/api/git/operations"))["rows"], [])
        pricing = await self.request("GET", "/api/pricing")
        self.assertIn("prices", pricing["active"])
        st = await self.request("PUT", "/api/settings", json={"max_parallel_chats": 2, "chat_history_timeout_sec": 60})
        self.assertEqual(st["max_parallel_chats"], 2)
        self.assertEqual((await self.request("GET", "/api/me"))["chat_history_timeout_sec"], 60)
        await self.request("PUT", "/api/settings", 400, json={"max_parallel_chats": True})
        await self.request("PUT", "/api/settings", 400, json={"turn_timeout_min": 10})
        await self.request("POST", self.base + "/client-terminals", 400, json={})

    async def test_settings_group_echo_timezone_and_atomic_rejection(self):
        settings = await self.request("GET", "/api/settings")
        patch = {k: settings[k] for k in ("permission_mode", "max_upload_mb", "listen")}
        patch["chat_history_timeout_sec"] = 65
        await self.request("PUT", "/api/settings", json=patch)
        await self.request("PUT", "/api/settings", json={"timezone": "UTC"})
        self.assertEqual((await self.request("GET", "/api/me"))["timezone"], "UTC")
        self.assertEqual((await self.request("GET", "/api/usage/events"))["timezone"], "UTC")
        await self.request("PUT", "/api/settings", 400, json={**patch, "listen": "0.0.0.0:8765"})
        await self.request("PUT", "/api/settings", 400, json={"max_parallel_chats": 8, "timezone": "bad/zone"})
        self.assertEqual((await self.request("GET", "/api/settings"))["max_parallel_chats"], 3)

    async def test_imported_prefix_keeps_cursors_after_cli_compaction(self):
        r = (await self.send())["receipt"]
        s = self.hub.sessions[r["thread_id"]]
        s.adopted = True
        prefix = [{"type": "user", "timestamp": "2020-01-01T00:00:00Z", "message": {"content": "before adoption"}},
                  {"type": "assistant", "timestamp": "2020-01-01T00:00:01Z", "message": {"content": [{"type": "text", "text": "old answer"}]}}]
        with patch.object(hub_abx, "read_transcript_full", return_value=prefix):
            first = await self.request("GET", self.base + "/history")
        self.hub.emit(s, "agent_event", event={"type": "assistant", "message": {"content": []}})
        with patch.object(hub_abx, "read_transcript_full", side_effect=AssertionError("prefix must be frozen")):
            after = await self.request("GET", self.base + "/history?thread=" + s.id + "&from=0")
        self.assertEqual(after["entries"][:len(first["entries"])], first["entries"])
        self.assertEqual((await self.request("GET", self.base + "/outline"))["items"][1]["line"], 2)

    async def test_legacy_hub_events_use_transcript_and_keep_turn_status(self):
        # 10-27 之前 hub 只存简化事件（工具只有摘要）：那一段要用转录的完整内容，hub 的每轮结束仍按时间插进去。
        r = (await self.send())["receipt"]
        s = self.hub.sessions[r["thread_id"]]
        log = self.hub.log_of(s.id)
        legacy_ts = time.time() - 3600
        events = log.read()
        for e in events:
            if e["kind"] == "user":
                e["ts"] = legacy_ts
        legacy = [{"seq": 0, "kind": "tool_use", "ts": legacy_ts + 1, "id": "t1", "name": "Bash", "summary": "ls"},
                  {"seq": 0, "kind": "result", "ts": legacy_ts + 2, "status": "idle", "secs": 2}]
        stamp = hub_abx.iso(legacy_ts + 1.5)
        prefix = [{"type": "assistant", "timestamp": stamp, "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]}}]
        raw = {"seq": 0, "kind": "agent_event", "ts": time.time(), "event": {"type": "assistant", "message": {"content": []}}}
        with patch.object(log, "read", return_value=events + legacy + [raw]), \
                patch.object(hub_abx, "read_transcript_full", return_value=prefix):
            entries, _ = self.c.build_history(s)
        kinds = [(e["kind"], (e.get("event") or {}).get("type")) for e in entries]
        self.assertIn(("event", "assistant"), kinds[:3])          # 转录里的完整工具调用
        self.assertEqual(entries[[k for k, _ in kinds].index("status")]["ts"], hub_abx.iso(legacy_ts + 2))
        self.assertEqual(sum(1 for k, _ in kinds if k == "user"), 1)   # 同一条用户消息不重复
        self.assertEqual(kinds[-1], ("event", "assistant"))       # 原样事件之后以 hub 事件流为准

    async def test_question_http_answer_and_steer_use_original_turn(self):
        r = (await self.send())["receipt"]
        s = self.hub.sessions[r["thread_id"]]
        waiter = hubd.Waiter("qa", s.id, "question", "AskUserQuestion", "", "", "",
                            [{"question": "选哪个", "options": [{"label": "A"}, {"label": "B"}]}], "",
                            time.time(), asyncio.get_running_loop().create_future())
        self.hub.waiters["qa"] = waiter
        self.hub.emit(s, "question", qid="qa", questions=waiter.questions)
        answered = await self.request("POST", self.base + "/chat/questions/qa", json={"answers": {"选哪个": "A"}})
        self.assertEqual(answered["state"], "answered")
        self.assertEqual(waiter.future.result()["answers"], {"选哪个": "A"})
        transport = types.SimpleNamespace(write=AsyncMock())
        self.hub.active[s.id]._transport = transport
        steer = (await self.send(s.id, text="change direction", delivery="steer"))["receipt"]
        self.assertEqual(steer["state"], "completed")
        self.assertEqual(steer["merged_into"], r["request_id"])
        self.assertEqual(steer["turn_id"], "")
        self.assertEqual(len(self.executions), 1)
        self.assertEqual(json.loads(transport.write.call_args.args[0])["priority"], "next")

    async def test_thread_and_native_workspace_deletion_keep_receipt_fences(self):
        r = (await self.send())["receipt"]
        await self.complete(r["thread_id"])
        pending = (await self.request("POST", self.base + "/chat/threads"))["id"]
        await self.request("POST", self.base + "/chat/threads/" + r["thread_id"] + "/activate")
        await self.request("DELETE", self.base + "/chat/threads/" + pending)
        self.assertEqual(self.c.active(self.w), r["thread_id"])
        await self.request("DELETE", "/hub/workspaces/" + self.w["id"])
        receipt = self.c.receipts.items[r["request_id"]]
        self.assertEqual(receipt["state"], "deleted")
        self.assertIsNone(receipt["request"])
        self.assertTrue((self.root / "workspace").is_dir())

    async def test_creation_replay_conflict_and_deleted_space_fence(self):
        rid = secrets.token_hex(16)
        path = "/api/session-creations/" + rid
        spec = {"agent": "claude", "name": str(self.root / "new-project"), "account_id": hub_spaces.ACCOUNT_ID, "source": "empty"}
        made = await self.request("PUT", path, json=spec)
        self.assertEqual((await self.request("PUT", path, json=spec))["session"]["id"], made["session"]["id"])
        await self.request("PUT", path, 409, json={**spec, "name": str(self.root / "different")})
        await self.request("DELETE", "/api/sessions/" + made["session"]["id"])
        await self.request("PUT", path, 410, json=spec)
        self.assertTrue((self.root / "new-project").is_dir())
        unknown = "/api/session-creations/" + secrets.token_hex(16)
        await self.request("POST", unknown, json={"action": "abandon"})
        await self.request("PUT", unknown, 410, json=spec)
        await self.request("POST", unknown, 410, json={"action": "finish"})

    async def test_real_executor_exit_failure_vs_uncertain_stream(self):
        self.hub.run_turn = types.MethodType(hubd.Hub.run_turn, self.hub)
        self.hub.notify_result = AsyncMock()
        for exit_code, state in ((1, "failed"), (None, "uncertain")):
            tid = self.c.active(self.w)
            client = types.SimpleNamespace(connect=AsyncMock(side_effect=hubd.ProcessError("synthetic failure", exit_code=exit_code)),
                                           disconnect=AsyncMock())
            with patch.object(hubd, "get_session_info", return_value=None), patch.object(hubd, "ClaudeSDKClient", return_value=client), patch.object(hubd.traceback, "print_exc"):
                r = (await self.send(tid))["receipt"]
                task = self.hub.tasks.get(tid)
                if task:
                    await task
                final = (await self.request("GET", self.base + "/chat/requests/" + r["request_id"]))["receipt"]
                self.assertEqual(final["state"], state)
                self.assertEqual(final.get("error_code"), "turn_failed" if exit_code else "execution_incomplete")
                client.disconnect.assert_awaited_once()
            await self.request("POST", self.base + "/chat/threads")

    async def test_attachment_symlinks_and_preview_grant_boundary(self):
        d = self.root / "shared" / ".file"
        d.mkdir(parents=True)
        (d / "ok.txt").write_text("safe")
        (d / "link.txt").symlink_to(self.root / "sessions.json")
        val = await self.request("POST", self.base + "/attachments/validate", json={"paths": ["/shared/.file/ok.txt", "/shared/.file/link.txt"]})
        self.assertEqual(val["valid"], [True, False])
        (self.root / "workspace" / "index.html").write_text("<h1>preview</h1>")
        (self.root / "workspace" / "escape").symlink_to(self.root / "shared", target_is_directory=True)
        grant = await self.request("GET", self.base + "/preview?path=index.html")
        public = await self.client.get(grant["url"])
        self.assertEqual(public.status, 200)
        self.assertIn("sandbox", public.headers["Content-Security-Policy"])
        escape = await self.client.get(grant["url"].rsplit("/", 1)[0] + "/escape/.file/ok.txt")
        self.assertEqual(escape.status, 404)

    async def test_file_and_skill_workflow_and_attachment_upload(self):
        await self.request("POST", self.base + "/files/mkdir", json={"dir": "", "name": "src", "scope": "workspace"})
        await self.request("PUT", self.base + "/file?path=src/demo.txt", data=b"hello")
        await self.request("POST", self.base + "/files/rename", json={"path": "src/demo.txt", "name": "readme.txt"})
        listing = await self.request("GET", self.base + "/files?path=src")
        self.assertEqual(listing[0]["name"], "readme.txt")
        self.assertEqual(listing[0]["size"], 5)
        moved = await self.request("POST", self.base + "/files/move", json={"source_path": "src/readme.txt", "source_scope": "workspace", "destination_scope": "shared", "destination_dir": ""})
        self.assertEqual(moved["path"], "readme.txt")
        fd = FormData()
        fd.add_field("file", b"attachment", filename="readme.txt", content_type="text/plain")
        uploaded = await self.request("POST", self.base + "/images", data=fd)
        self.assertTrue(uploaded["path"].startswith("/shared/.file/"))
        self.assertEqual((await self.request("POST", self.base + "/attachments/validate", json={"paths": [uploaded["path"]]}))["valid"], [True])
        fd = FormData()
        fd.add_field("file", b"---\ndescription: demo skill\n---\nTest instructions", filename="demo.md", content_type="text/markdown")
        await self.request("POST", self.base + "/skills?scope=session", data=fd)
        detail = await self.request("GET", self.base + "/skills/demo")
        self.assertEqual(detail["description"], "demo skill")
        self.assertEqual(detail["entries"][0]["path"], "SKILL.md")
        await self.request("POST", self.base + "/skills/demo/copy", json={"to": "template"})
        await self.request("DELETE", self.base + "/skills/demo")
        self.assertEqual((await self.request("GET", self.base + "/skills"))["skills"][0]["source"], "template")
        await self.request("DELETE", self.base + "/skills/demo?scope=template")
        self.assertEqual((await self.request("GET", self.base + "/skills"))["skills"], [])

    async def test_local_git_status_diff_commit_and_discard(self):
        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}):
            await self.hub.git.run(self.w["path"], "init", "--initial-branch=main")
            path = self.root / "workspace" / "demo.txt"
            path.write_text("one\n")
            status = await self.request("GET", self.base + "/git/status")
            self.assertEqual(status["files"][0]["status"], "??")
            committed = await self.request("POST", self.base + "/git/commit", json={"message": "initial", "repo": ""})
            self.assertTrue(committed["sha"])
            self.assertFalse(committed["pushed"])
            path.write_text("two\n")
            response = await self.client.get(self.base + "/git/diff?path=demo.txt", headers=self.auth)
            self.assertEqual(response.status, 200)
            self.assertIn("+two", await response.text())
            await self.request("POST", self.base + "/git/discard", json={"path": "demo.txt", "repo": ""})
            self.assertEqual(path.read_text(), "one\n")
            self.assertEqual((await self.request("GET", self.base + "/git/status"))["files"], [])
            operations = await self.request("GET", "/api/git/operations")
            self.assertEqual([r["operation"] for r in operations["rows"]], ["discard", "commit"])
            self.assertEqual(operations["next_before"], 0)

    async def test_terminal_binary_resize_and_workspace_start(self):
        # 真 PTY + cat 验证帧协议；不启动 tmux/shell，不接触主机终端或 shell 初始化文件。
        self.w["stopped"] = True
        with patch.object(self.hub.terminals, "ensure", new=AsyncMock()), patch.object(self.hub.terminals, "tmux", return_value=["/bin/cat"]):
            ws = await self.client.ws_connect(self.base + "/term", headers=self.auth)
            await ws.send_json({"type": "resize", "cols": "invalid", "rows": None})
            await ws.send_json({"type": "resize", "cols": 100, "rows": 30})
            await ws.send_bytes(b"terminal-test\n")
            output = b""
            while b"terminal-test" not in output:
                msg = await ws.receive(timeout=3)
                self.assertEqual(msg.type, web.WSMsgType.BINARY)
                output += msg.data
            self.assertFalse(self.w.get("stopped"))
            await ws.close()

    async def test_auth_cookie_origin_and_native_api(self):
        response = await self.client.get("/api/me")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/hub/login", json={"token": self.token})
        self.assertEqual(response.status, 200)
        response = await self.client.put("/api/settings", headers={"Origin": "https://elsewhere.invalid", "Sec-Fetch-Site": "cross-site"}, json={"max_parallel_chats": 1})
        self.assertEqual(response.status, 403)
        with self.assertRaises(WSServerHandshakeError):
            await self.client.ws_connect(self.base + "/chat", headers={**self.auth, "Origin": "https://elsewhere.invalid"})
        native = await self.request("GET", "/hub/sessions")
        self.assertTrue(native["ok"])
        self.assertIsInstance(native["sessions"], list)
        await self.request("POST", "/api/logout")
        self.assertEqual((await self.client.get("/hub/token")).status, 200)
        self.assertEqual((await (await self.client.get("/hub/token")).json())["token"], "")

    async def test_theme_roundtrip_and_reject_unsafe_css(self):
        theme = {"agentbox_theme": 1, "id": "mine", "name": "我的主题", "base": "amber", "dark": {"--bg": "#123456"}}
        result = await self.request("PUT", "/api/themes/user/mine", json=theme)
        self.assertEqual(result["manifest"]["id"], "mine")
        self.assertEqual(result["scope"], "user")
        listing = await self.request("GET", "/api/themes")
        self.assertEqual(listing["user"][0], result)
        self.assertGreater(len(listing["tokens"]), 50)
        for value in ("url(https://bad.invalid)", "#fff; color:red", r"u\72l(x)", "var(--secret)"):
            theme["dark"]["--bg"] = value
            error = await self.request("POST", "/api/themes/validate", 400, json=theme)
            self.assertEqual(error["theme_error"]["code"], "bad_value")

    async def test_diagnostics_protocol_does_not_call_model(self):
        report = await self.request("POST", self.base + "/diagnostics")
        self.assertEqual(report["scope"], "session")
        self.assertTrue(all(c["state"] == "not_checked" for c in report["checks"] if c["id"] == "model"))
        operation = secrets.token_hex(16)
        ws = await self.client.ws_connect(self.base + "/diagnostics/ws?connection_id=" + operation, headers=self.auth)
        frame = await ws.receive_json()
        self.assertEqual(frame["operation_id"], operation)
        self.assertEqual(frame["code"], "websocket_ok")
        await ws.close()
        self.assertEqual(self.executions, [])

    async def test_force_stop_cannot_cancel_a_newer_turn(self):
        old = asyncio.create_task(asyncio.sleep(100))
        newer = asyncio.create_task(asyncio.sleep(100))
        self.hub.tasks["fake"] = newer
        await self.hub.force_stop("fake", 0, old)
        self.assertFalse(newer.cancelled())
        self.assertFalse(newer.cancelling())
        old.cancel()
        newer.cancel()
        await asyncio.gather(old, newer, return_exceptions=True)
        self.hub.tasks.pop("fake")


if __name__ == "__main__":
    unittest.main()
