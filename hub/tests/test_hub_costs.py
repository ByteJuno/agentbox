"""agent-hub 费用/上限/上下文的单测：不连模型、不碰真实状态目录。

跑法：cd /home/ubuntu/ai-work/agent-hub && .venv/bin/python -m unittest discover -s tests -v
"""
import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import hubd  # noqa: E402


def usage_row(sid, turn, cost, model="claude-opus-5-5"):
    return {"ts": 1.0, "sid": sid, "session": "t", "cwd": "/tmp", "turn": turn, "model": model, "sub": False,
            "input": 10, "output": 100, "cache_read": 1000, "cache_write": 0, "cost": cost}


def result(**kw):
    """假的 ResultMessage：只带 classify_result 会看的字段。"""
    base = {"subtype": "success", "is_error": False, "terminal_reason": "completed", "errors": None,
            "api_error_status": None, "total_cost_usd": None, "result": "done", "num_turns": 3}
    base.update(kw)
    return types.SimpleNamespace(**base)


def make_hub(ledger_path):
    hub = hubd.Hub.__new__(hubd.Hub)   # 只要账本，不启动 hub（不读 telegram、不开 socket）
    hub.ledger = hubd.UsageLedger(ledger_path)
    return hub


class UsageLedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "usage.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def write(self, rows, extra=""):
        with open(self.path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.write(extra)

    def test_sums_by_turn_and_session(self):
        self.write([usage_row("a", "t1", 0.5), usage_row("a", "t1", 0.25), usage_row("a", "t2", 1.0),
                    usage_row("b", "t9", 2.0)], extra="{bad json\n\n")
        led = hubd.UsageLedger(self.path)
        self.assertEqual(led.rows, 4)                       # 坏行跳过，不炸
        self.assertAlmostEqual(led.turn_cost("a", "t1"), 0.75)
        self.assertAlmostEqual(led.turn_cost("a", "t2"), 1.0)
        self.assertAlmostEqual(led.total("a"), 1.75)
        self.assertAlmostEqual(led.total("b"), 2.0)
        self.assertIsNone(led.turn_cost("a", "nope"))
        self.assertIsNone(led.total("nope"))
        self.assertEqual(led.last_turn["a"], "t2")

    def test_reload_is_stable(self):
        self.write([usage_row("a", "t1", 0.3)])
        led = hubd.UsageLedger(self.path)
        led.reload()
        led.reload()
        self.assertAlmostEqual(led.total("a"), 0.3)         # 重复 reload 不会把同一行算两次

    def test_missing_file(self):
        led = hubd.UsageLedger(os.path.join(self.dir.name, "nope.jsonl"))
        self.assertEqual(led.rows, 0)
        self.assertIsNone(led.total("a"))


class RepairTest(unittest.TestCase):
    """老数据里 total_cost 是按 SDK 累计值逐轮加出来的：这里要能按 usage 重算且幂等。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.usage = os.path.join(self.dir.name, "usage.jsonl")
        self.sessions = os.path.join(self.dir.name, "sessions.json")
        rows = [
            {"id": "sid-a", "name": "a", "cwd": "/tmp", "total_cost": 100.0, "last_cost": 100.0, "turns": 3},
            {"id": "sid-b", "name": "b", "cwd": "/tmp", "total_cost": 7.0, "last_cost": None, "turns": 0},
            {"id": "sid-c", "name": "c", "cwd": "/tmp", "total_cost": 0.5, "last_cost": 0.5, "turns": 1},
        ]
        with open(self.sessions, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        with open(self.usage, "w", encoding="utf-8") as f:
            for row in [usage_row("sid-a", "t1", 0.5), usage_row("sid-a", "t1", 0.25),
                        usage_row("sid-a", "t2", 0.75), usage_row("sid-c", "t1", 1.0)]:
                f.write(json.dumps(row) + "\n")

    def tearDown(self):
        self.dir.cleanup()

    def load(self):
        with open(self.sessions, encoding="utf-8") as f:
            return {row["id"]: row for row in json.load(f)}

    def test_repairs_totals_and_is_idempotent(self):
        led = hubd.UsageLedger(self.usage)
        changes, before, after = hubd.repair_sessions_costs(led, self.sessions)
        self.assertEqual(len(changes), 2)                  # sid-b 没有用量记录：不动
        self.assertAlmostEqual(before, 107.5)
        self.assertAlmostEqual(after, 9.5)          # sid-a 1.5 + sid-b 7.0（不变）+ sid-c 1.0
        rows = self.load()
        self.assertAlmostEqual(rows["sid-a"]["total_cost"], 1.5)
        self.assertAlmostEqual(rows["sid-a"]["last_cost"], 0.75)   # 最后一轮 t2
        self.assertAlmostEqual(rows["sid-c"]["total_cost"], 1.0)
        self.assertEqual(rows["sid-b"]["total_cost"], 7.0)
        self.assertEqual(len(rows), 3)                     # 一个会话都不删
        backups = [n for n in os.listdir(self.dir.name) if ".bak-" in n]
        self.assertEqual(len(backups), 1)
        again, _, _ = hubd.repair_sessions_costs(hubd.UsageLedger(self.usage), self.sessions)
        self.assertEqual(again, [])                        # 幂等
        backups = [n for n in os.listdir(self.dir.name) if ".bak-" in n]
        self.assertEqual(len(backups), 1)                  # 没变化就不再备份

    def test_dry_run_writes_nothing(self):
        before_mtime = os.stat(self.sessions).st_mtime_ns
        changes, _, _ = hubd.repair_sessions_costs(hubd.UsageLedger(self.usage), self.sessions, dry_run=True)
        self.assertEqual(len(changes), 2)
        self.assertEqual(os.stat(self.sessions).st_mtime_ns, before_mtime)
        self.assertAlmostEqual(self.load()["sid-a"]["total_cost"], 100.0)
        self.assertEqual([n for n in os.listdir(self.dir.name) if ".bak-" in n], [])


class TurnCostTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.usage = os.path.join(self.dir.name, "usage.jsonl")
        with open(self.usage, "w", encoding="utf-8") as f:
            for row in [usage_row("sid-a", "t1", 0.4), usage_row("sid-a", "t1", 0.1), usage_row("sid-a", "t2", 0.5)]:
                f.write(json.dumps(row) + "\n")
        self.hub = make_hub(self.usage)
        self.s = hubd.Session(id="sid-a", name="a", cwd="/tmp")
        self.turn = hubd.Turn(sent_at=0.0, id="t2")

    def tearDown(self):
        self.dir.cleanup()

    def test_prefers_usage_rows(self):
        last, total = self.hub.turn_cost(self.s, self.turn, result(total_cost_usd=99.0))
        self.assertAlmostEqual(last, 0.5)      # 只算 t2 的请求
        self.assertAlmostEqual(total, 1.0)     # 全部用量求和，不理会 SDK 的 99.0
        self.s.total_cost = total
        last2, total2 = self.hub.turn_cost(self.s, self.turn, result(total_cost_usd=99.0))
        self.assertAlmostEqual(last2, 0.5)
        self.assertAlmostEqual(total2, 1.0)    # 幂等：再算一遍还是 1.0，没翻倍

    def test_falls_back_to_delta_without_usage_rows(self):
        turn = hubd.Turn(sent_at=0.0, id="t3")
        self.s.total_cost = 1.0
        self.s.sdk_cost_total = 1.0
        last, total = self.hub.turn_cost(self.s, turn, result(total_cost_usd=1.4))
        self.assertAlmostEqual(last, 0.4)
        self.assertAlmostEqual(total, 1.0)     # 有会话用量记录：累计以 usage 为准
        self.s.sdk_cost_total = 1.4
        last2, total2 = self.hub.turn_cost(self.s, turn, result(total_cost_usd=1.4))
        self.assertIsNone(last2)               # 同一个累计值不会再加一次
        self.assertAlmostEqual(total2, 1.0)

    def test_no_sdk_total_keeps_previous(self):
        # 没有 SDK 累计值、这一轮也没有用量行：累计保持不变（不会凭空变小或变大）
        turn = hubd.Turn(sent_at=0.0, id="t4")
        other = hubd.Session(id="sid-nolegacy", name="x", cwd="/tmp")
        other.total_cost = 3.0
        last, total = self.hub.turn_cost(other, turn, result(total_cost_usd=None))
        self.assertIsNone(last)
        self.assertAlmostEqual(total, 3.0)

    def test_ledger_total_wins_over_stale_total(self):
        # 有用量记录时累计以 usage 为准：外面临时塞进来的 3.0 不参与
        turn = hubd.Turn(sent_at=0.0, id="t4")
        self.s.total_cost = 3.0
        last, total = self.hub.turn_cost(self.s, turn, result(total_cost_usd=None))
        self.assertIsNone(last)
        self.assertAlmostEqual(total, 1.0)


class ClassifyResultTest(unittest.TestCase):
    def test_plain_success(self):
        v = hubd.classify_result(result())
        self.assertEqual((v.status, v.error, v.pause_queue, v.mismatch), ("idle", "", False, False))

    def test_false_positive_is_error_without_evidence(self):
        v = hubd.classify_result(result(is_error=True))
        self.assertEqual(v.status, "idle")
        self.assertEqual(v.error, "")
        self.assertTrue(v.mismatch)            # 记一行日志，方便复查

    def test_api_error_with_status_is_real(self):
        v = hubd.classify_result(result(is_error=True, api_error_status=429, terminal_reason="api_error"))
        self.assertEqual(v.status, "error")
        self.assertIn("429", v.error)
        self.assertNotIn("success", v.error)
        self.assertFalse(v.mismatch)

    def test_api_error_without_status_is_real(self):
        v = hubd.classify_result(result(is_error=True, terminal_reason="api_error"))
        self.assertEqual(v.status, "error")
        self.assertIn("API", v.error)

    def test_errors_list_wins(self):
        v = hubd.classify_result(result(is_error=True, errors=["boom"]))
        self.assertEqual((v.status, v.error), ("error", "boom"))

    def test_error_subtype(self):
        v = hubd.classify_result(result(is_error=True, subtype="error_during_execution"))
        self.assertEqual(v.status, "error")
        self.assertIn("error_during_execution", v.error)

    def test_max_turns_pauses_queue(self):
        v = hubd.classify_result(result(is_error=True, subtype="error_max_turns", terminal_reason="max_turns"), 30)
        self.assertEqual(v.status, "error")
        self.assertTrue(v.pause_queue)
        self.assertIn("max_turns=30", v.error)
        self.assertIn("接着跑", v.error)

    def test_aborted_is_interrupted(self):
        v = hubd.classify_result(result(terminal_reason="aborted_streaming"))
        self.assertEqual(v.status, "interrupted")
        self.assertFalse(v.pause_queue)


class ContextNoticeTest(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(hubd.context_notice(49.9), (None, ""))
        level, note = hubd.context_notice(50)
        self.assertEqual(level, "info")
        self.assertIn("50%", note)
        level, note = hubd.context_notice(69.9)
        self.assertEqual(level, "info")
        level, note = hubd.context_notice(70)
        self.assertEqual(level, "warn")
        self.assertIn("70%", note)
        self.assertIn("压缩", note)
        self.assertEqual(hubd.context_notice(None), (None, ""))
        self.assertEqual(hubd.context_notice("bad"), (None, ""))

    def test_no_auto_compact(self):
        # 这里只提醒：确认没人偷偷调 compact 之类的方法（源码层面守住，防止以后加回来）
        src = (ROOT / "hubd.py").read_text(encoding="utf-8")
        self.assertNotIn("client.compact", src)
        self.assertNotIn("auto_compact", src)


class TurnOptionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.empty_usage = os.path.join(self.dir.name, "usage.jsonl")
        self.hub = make_hub(self.empty_usage)
        self.s = hubd.Session(id="11111111-2222-3333-4444-555555555555", name="n", cwd="/tmp",
                              mode="acceptEdits", model="claude-opus-5-5", effort="high")

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults(self):
        opts = self.hub.turn_options(self.s)
        self.assertEqual(opts.max_turns, 30)
        self.assertEqual(opts.max_turns, hubd.MAX_TURNS)
        self.assertEqual(opts.extra_args["append-system-prompt"], hubd.SYSTEM_APPEND)
        self.assertIn("Bash", hubd.SYSTEM_APPEND)
        self.assertIn("tail", hubd.SYSTEM_APPEND)
        self.assertNotIn("prompt-suggestions", opts.extra_args)     # 关掉这个额外请求
        self.assertIsNone(opts.system_prompt)                       # 原系统提示没被替换
        # 模型 / 思考强度 / 权限：跟会话里存的一样，没有被改动
        self.assertEqual(opts.model, "claude-opus-5-5")
        self.assertEqual(opts.effort, "high")
        self.assertEqual(opts.permission_mode, "acceptEdits")
        self.assertEqual(opts.session_id, self.s.id)
        self.assertIsNone(opts.resume)

    def test_resume_and_append_switch(self):
        self.s.started = True
        opts = self.hub.turn_options(self.s)
        self.assertEqual(opts.resume, self.s.id)
        self.assertIsNone(opts.session_id)
        old = hubd.SYSTEM_APPEND
        hubd.SYSTEM_APPEND = ""
        try:
            opts = self.hub.turn_options(self.s)
            self.assertNotIn("append-system-prompt", opts.extra_args)
        finally:
            hubd.SYSTEM_APPEND = old

    def test_cli_argv(self):
        """真的把 SDK 拼出来的命令行拿出来看：追加方式是 --append-system-prompt，原提示参数没变。"""
        from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
        opts = self.hub.turn_options(self.s)
        cmd = SubprocessCLITransport("hi", opts)._build_command()
        self.assertEqual(cmd.count("--append-system-prompt"), 1)
        self.assertEqual(cmd[cmd.index("--append-system-prompt") + 1], hubd.SYSTEM_APPEND)
        self.assertNotIn("--system-prompt-file", cmd)
        self.assertNotIn("--prompt-suggestions", cmd)
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "30")
        # 原来 SDK 就带的 --system-prompt ""：保持不动，没换成别的提示
        self.assertIn("--system-prompt", cmd)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "")
        self.assertNotIn("--dangerously-skip-permissions", cmd)      # 没顺手放开权限
        self.assertNotIn("--disallowedTools", cmd)


class RecordUsageTest(unittest.TestCase):
    """record_usage 要同时做三件事：写 usage.jsonl、记进账本、推 usage 事件。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.old = (hubd.USAGE_PATH, hubd.EVENTS_DIR)
        hubd.USAGE_PATH = os.path.join(self.dir.name, "usage.jsonl")
        hubd.EVENTS_DIR = self.dir.name
        self.hub = hubd.Hub.__new__(hubd.Hub)
        self.hub.ledger = hubd.UsageLedger(hubd.USAGE_PATH)
        self.hub.logs = {}
        self.hub.global_subs = set()
        self.hub.listeners = []
        self.s = hubd.Session(id="sid-a", name="a", cwd="/tmp")
        self.turn = hubd.Turn(sent_at=1.0, id="t1")

    def tearDown(self):
        hubd.USAGE_PATH, hubd.EVENTS_DIR = self.old
        self.dir.cleanup()

    def test_records_and_sums(self):
        req = {"start": 1.0, "first": 1.5, "model": "claude-opus-5-5",
               "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 5000,
                         "cache_creation_input_tokens": 0}}
        self.hub.record_usage(self.s, self.turn, req, None)
        with open(hubd.USAGE_PATH, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn"], "t1")
        self.assertGreater(rows[0]["cost"], 0)
        self.assertAlmostEqual(self.hub.ledger.turn_cost("sid-a", "t1"), rows[0]["cost"])
        last, total = self.hub.turn_cost(self.s, self.turn, result(total_cost_usd=5.0))
        self.assertAlmostEqual(last, rows[0]["cost"])
        self.assertAlmostEqual(total, rows[0]["cost"])   # 不是 SDK 的 5.0


class MaxTurnsEnvTest(unittest.TestCase):
    def test_env_override(self):
        old = os.environ.get("AGENT_HUB_MAX_TURNS")
        os.environ["AGENT_HUB_MAX_TURNS"] = "7"
        try:
            mod = importlib.reload(hubd)
            self.assertEqual(mod.MAX_TURNS, 7)
        finally:
            if old is None:
                os.environ.pop("AGENT_HUB_MAX_TURNS", None)
            else:
                os.environ["AGENT_HUB_MAX_TURNS"] = old
            importlib.reload(hubd)

    def test_bad_env_falls_back(self):
        old = os.environ.get("AGENT_HUB_MAX_TURNS")
        os.environ["AGENT_HUB_MAX_TURNS"] = "abc"
        try:
            mod = importlib.reload(hubd)
            self.assertEqual(mod.MAX_TURNS, 30)
        finally:
            if old is None:
                os.environ.pop("AGENT_HUB_MAX_TURNS", None)
            else:
                os.environ["AGENT_HUB_MAX_TURNS"] = old
            importlib.reload(hubd)


class RepairLockTest(unittest.TestCase):
    """服务在跑的时候不能改 sessions.json（内存里那份会把改完的覆盖掉）。"""

    def setUp(self):
        import fcntl
        self.dir = tempfile.TemporaryDirectory()
        self.state = self.dir.name
        self.old = (hubd.STATE_DIR, hubd.SESSIONS_PATH, hubd.USAGE_PATH)
        hubd.STATE_DIR = self.state
        hubd.SESSIONS_PATH = os.path.join(self.state, "sessions.json")
        hubd.USAGE_PATH = os.path.join(self.state, "usage.jsonl")
        with open(hubd.SESSIONS_PATH, "w", encoding="utf-8") as f:
            json.dump([{"id": "sid-a", "name": "a", "total_cost": 9.0}], f)
        with open(hubd.USAGE_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(usage_row("sid-a", "t1", 0.2)) + "\n")
        self.lock = open(os.path.join(self.state, "hub.lock"), "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def tearDown(self):
        self.lock.close()
        hubd.STATE_DIR, hubd.SESSIONS_PATH, hubd.USAGE_PATH = self.old
        self.dir.cleanup()

    def test_refuses_while_running(self):
        self.assertEqual(hubd.repair_costs_cli(["--repair-costs"]), 3)
        with open(hubd.SESSIONS_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["total_cost"], 9.0)

    def test_runs_when_free(self):
        self.lock.close()
        with open(hubd.SESSIONS_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["total_cost"], 9.0)
        self.assertEqual(hubd.repair_costs_cli(["--repair-costs"]), 0)
        with open(hubd.SESSIONS_PATH, encoding="utf-8") as f:
            self.assertAlmostEqual(json.load(f)[0]["total_cost"], 0.2)


if __name__ == "__main__":
    unittest.main()
