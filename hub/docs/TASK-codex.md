# Task: make agent-hub behave like real agentbox (2026-10-31)

You are working in a fork of devilcoolyue/agentbox (branch `hub`). The upstream
Go backend + TypeScript frontend live in the repo as usual. `hub/` is our own
Python backend ("agent-hub") that serves the **unmodified** upstream frontend
(`internal/web/static`, built from `web/src`) but runs Claude Code sessions
directly on this host (no Docker, single user, sessions via claude-agent-sdk).
Read `hub/README.md` first.

The owner says the web UI "feels weird" (用起来怪怪的) on agent-hub compared to
real agentbox. The hub backend was written by imitating the API shapes, not by
reading the Go server carefully, and the frontend was just upgraded from
upstream ffa7ac6 (2026-10-26) to e19df41 (2026-10-29) — see
`git diff ffa7ac6 e19df41 -- web/src internal/server docs contracts`
(new: running-state, chat window, head-fold, configurable max_parallel_chats,
terminal changes, …).

## What to do

1. Use the upstream code as the source of truth: `internal/server/*.go`
   (chat.go, chat_*.go, ws/stream handlers, threads, history, receipts,
   questions, tasks, workspaces/projects, settings, usage, files, git, term),
   `internal/chat`, `internal/protocol`, `docs/chat-protocol.md`,
   `docs/chat-recovery.md`, `docs/api.md`, `contracts/*.json`,
   `web/src/types.d.ts`, `web/src/contracts/v1.d.ts`, and how `web/src/**`
   actually consumes each response / WS frame.
2. Systematically compare every `/api/...` route and WS message the frontend
   uses against what `hub/*.py` implements: missing routes, wrong field names
   or types, missing fields the UI relies on, wrong status values, wrong event
   ordering (e.g. turn start/end, running/idle, receipts, assistant text vs tool
   rows, partial/streaming deltas, thinking, tool results, cost/usage rows),
   history paging/cursors, unread/last-activity, thread titles, error envelopes
   (`contracts/chat-errors-v1.schema.json`), reconnect/resume semantics.
   List routes the frontend calls that hub does not serve at all (grep
   `web/src` for `api(`/`fetch(`/`/api/`), and decide for each: implement,
   or return the same "feature off" shape upstream returns so the UI degrades
   cleanly (no spinners forever, no error toasts).
3. Fix the mismatches in `hub/*.py`, prioritised by what a user notices most:
   chat (live streaming, tool rows, status/running indicator, stop, queue /
   steer / withdraw receipts, questions/approvals, history scroll-back,
   thread list order/unread/titles, reload mid-turn), then workspace list /
   switching, then files, git, terminal, skills, settings, usage, monitor.
4. Keep these product decisions (do NOT undo):
   - host-direct, single user; no Docker, accounts pool, proxies, tunnels,
     remote browser, extensions, MCP page, skills market, git remotes/push/
     PR/clone, project upload / git import (hidden via `hub/web/hub/hub.css`).
   - web-created threads default to `bypassPermissions`.
   - path virtualization (/workspace, /shared) as described in README.
   - `/hub/...` native API (used by Eta and `agentctl`) must stay compatible.
   - security model (token auth, same-origin rule for cookie writes,
     preview grants) must not get weaker.
5. Do not modify upstream files outside `hub/` unless absolutely unavoidable
   (and then explain why). `hub/web/hub/hub.css|hub.js` may be adjusted (e.g.
   hide new upstream entry points that hub cannot support).

## Hard rules

- NEVER touch the production instance: do not stop/restart/kill the
  `agent-hub` systemd user service or any process listening on port 8765,
  never write to `~/.agent-hub`, never run `pkill`/`killall`. The person you
  are working for is chatting through that production hub right now.
- For live testing start your own instance from this checkout:
  ```sh
  mkdir -p /tmp/hubdev && AGENT_HUB_STATE=/tmp/hubdev AGENT_HUB_HTTP_PORT=18766 \
    AGENT_HUB_HTTP_HOSTS=127.0.0.1 AGENT_HUB_TG=0 \
    /home/ubuntu/ai-work/agent-hub/.venv/bin/python hub/hubd.py > /tmp/hubdev.log 2>&1 &
  ```
  token is in /tmp/hubdev/token (do not print it). Stop only that PID.
  Real Claude turns may not work from inside your sandbox; API/WS shape tests,
  `python -m py_compile`, and `hub/tests` are fine. Avoid spending model money
  in loops.
- Never print secrets/tokens.
- Do not `git commit` or push; leave changes in the working tree.
- Keep code style: Python, aiohttp, Chinese comments like the existing code.

## Deliverable

When finished, write `hub/REVIEW-codex.md` (Chinese) with: the mismatches you
found (route / frontend file:line / upstream file:line / what was wrong), what
you changed, what you deliberately left out and why, and what still needs a
live browser test with real Claude turns.
