"""事件日志增量读取、历史逐条虚拟化缓存、使用趋势分桶的回归；不连 Claude、不碰真实 home。

AGENT_HUB_TG=0 <venv>/python -m unittest discover -s hub/tests -v
"""
import datetime
import json
import os
import sys
import tempfile
import types
import unittest
import zoneinfo
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hubd
import hub_abx
import hub_spaces


class EventLogRowsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-rows-")
        self.p = patch.object(hubd, "EVENTS_DIR", self.temp.name)
        self.p.start()
        self.log = hubd.EventLog("s1")

    def tearDown(self):
        self.p.stop()
        hubd._ROWS.pop(self.log.path, None)
        self.temp.cleanup()

    def write(self, raw: bytes):
        with open(self.log.path, "ab") as f:
            f.write(raw)

    def test_reads_only_complete_lines_and_keeps_returned_lists_stable(self):
        self.write(json.dumps({"seq": 1, "kind": "user", "text": "a b"}, ensure_ascii=False).encode() + b"\n")
        first = self.log.read()
        self.assertEqual([r["seq"] for r in first], [1])
        self.assertEqual(first[0]["text"], "a b")   # U+2028 不是行分隔
        line = json.dumps({"seq": 2, "kind": "status"}).encode()
        self.write(line[:5])                               # 写到一半的行先不解析
        self.assertEqual([r["seq"] for r in self.log.read()], [1])
        self.write(line[5:] + b"\n")
        second = self.log.read()
        self.assertEqual([r["seq"] for r in second], [1, 2])
        self.assertEqual(len(first), 1)                   # 之前返回的列表没有被原地追加
        self.assertIs(second[0], first[0])                # 旧行不重解析
        self.assertEqual([r["seq"] for r in self.log.read(after=1)], [2])
        self.assertEqual([r["seq"] for r in self.log.read(limit=1)], [2])

    def test_deleted_or_shorter_file_is_read_again(self):
        self.write(b'{"seq": 1, "kind": "user"}\n{"seq": 2, "kind": "user"}\n')
        self.assertEqual(len(self.log.read()), 2)
        os.unlink(self.log.path)
        self.assertEqual(self.log.read(), [])
        self.write(b'{"seq": 7, "kind": "status"}\n')
        self.assertEqual([r["seq"] for r in self.log.read()], [7])


class VirtualEntriesTest(unittest.TestCase):
    def setUp(self):
        hub = types.SimpleNamespace(files=types.SimpleNamespace(shared="/srv/shared"))
        self.c = hub_abx.Compat.__new__(hub_abx.Compat)
        self.c.hub = hub
        self.c.virtual_memo = {}
        self.s = types.SimpleNamespace(id="s1", cwd="/home/u/proj")

    def whole(self, entries):
        """改动前的做法：整段序列化、替换、再解析。"""
        return json.loads(self.c.virtualize(hub_abx.dumps(entries), self.s.cwd))

    def test_matches_whole_dump_and_reuses_unchanged_events(self):
        ev = {"type": "assistant", "message": {"content": [{"type": "tool_use", "input": {"file_path": "/home/u/proj/a.py"}}]}}
        entries = [{"kind": "user", "text": "看看 /srv/shared/.images/x.png", "ts": "t1"},
                   {"kind": "event", "event": ev, "ts": "t2"}]
        out, sizes = self.c.virtual_entries(self.s, entries)
        self.assertEqual(out, self.whole(entries))
        self.assertEqual(sizes, [len(hub_abx.dumps(e).encode()) for e in out])
        self.assertEqual(out[1]["event"]["message"]["content"][0]["input"]["file_path"], "/workspace/a.py")
        again, _ = self.c.virtual_entries(self.s, [dict(entries[0]), {"kind": "event", "event": ev, "ts": "t2"}])
        self.assertIs(again[1], out[1])                  # 同一个事件对象：直接复用
        changed = [{"kind": "event", "event": ev, "ts": "t2", "state": "interrupted"}]
        third, _ = self.c.virtual_entries(self.s, changed)
        self.assertEqual(third[0]["state"], "interrupted")   # 状态变了要重新生成


class UsageTrendTest(unittest.TestCase):
    TZ = "Asia/Shanghai"

    def setUp(self):
        self.sp = hub_spaces.Spaces.__new__(hub_spaces.Spaces)
        self.sp.prefs = lambda: {"timezone": self.TZ}
        self.sp.hub = types.SimpleNamespace(workspaces=types.SimpleNamespace(items={}, by_path=lambda p: None))
        self.sp.prices = {}
        self.rows = []
        self.sp.usage_all = lambda: self.rows

    def ts(self, text):
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M").replace(tzinfo=zoneinfo.ZoneInfo(self.TZ)).timestamp()

    def row(self, when, cost=0.5, inp=10):
        self.rows.append({"_id": len(self.rows) + 1, "ts": self.ts(when), "cost": cost, "input": inp, "output": 1})

    def test_empty_range_has_no_buckets(self):
        self.assertEqual(self.sp.usage_trend({}), {"granularity": "hour", "buckets": [], "timezone": self.TZ})

    def test_hourly_buckets_stop_at_now_and_sum_to_the_table_total(self):
        self.row("2026-11-03T08:10", 0.1)
        self.row("2026-11-03T08:50", 0.2)
        self.row("2026-11-03T10:05", 0.3)
        q = {"since": "2026-11-03T00:00", "until": "2026-11-04T00:00"}
        out = self.sp.usage_trend(q, now=self.ts("2026-11-03T11:30"))
        self.assertEqual(out["granularity"], "hour")
        self.assertEqual([b["start"] for b in out["buckets"]][:2], ["2026-11-03T00:00", "2026-11-03T01:00"])
        self.assertEqual(out["buckets"][-1]["start"], "2026-11-03T11:00")   # 只画到现在
        by = {b["start"]: b for b in out["buckets"]}
        self.assertEqual(by["2026-11-03T08:00"]["requests"], 2)
        self.assertEqual(by["2026-11-03T09:00"]["requests"], 0)              # 空时段补空桶
        total = self.sp.usage_events(dict(q, limit="1"))["total"]
        self.assertEqual(sum(b["cost_micro_usd"] for b in out["buckets"]), total["cost_micro_usd"])
        self.assertEqual(sum(b["input_tokens"] for b in out["buckets"]), total["input_tokens"])

    def test_longer_ranges_switch_to_days_then_months(self):
        self.row("2026-10-27T09:00")
        self.row("2026-11-02T23:59")
        out = self.sp.usage_trend({}, now=self.ts("2026-11-03T12:00"))
        self.assertEqual(out["granularity"], "day")
        self.assertEqual(out["buckets"][0]["start"], "2026-10-27T00:00")     # 「全部」从第一条记录开始
        self.assertEqual(out["buckets"][-1]["start"], "2026-11-03T00:00")
        self.row("2025-12-31T23:00")
        out = self.sp.usage_trend({}, now=self.ts("2026-11-03T12:00"))
        self.assertEqual(out["granularity"], "month")
        self.assertEqual(out["buckets"][0]["start"], "2025-12-01T00:00")
        self.assertEqual(out["buckets"][1]["start"], "2026-01-01T00:00")
        self.assertEqual(sum(b["requests"] for b in out["buckets"]), 3)


if __name__ == "__main__":
    unittest.main()
