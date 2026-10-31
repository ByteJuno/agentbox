"""前端可选功能的降级接口：没有实现的写操作明确拒绝，不能假报成功。

项目终端 / MCP / 市场 / 远程 Git 在主机版隐藏，读取使用上游的空列表形状。
诊断只检查本实例，不启动 CLI、不读取账号令牌、不调用模型。
"""
import os
import secrets
import time

from aiohttp import web

from hub_abx import js, problem


def register(app, api):
    hub = api.hub

    def workspace(request):
        if "id" in request.match_info:
            hub.workspaces.get(request.match_info["id"])

    async def empty(request):
        workspace(request)
        return js([], headers={"Cache-Control": "no-store"})

    async def disabled(request):
        workspace(request)
        return problem(400, "invalid_request", "主机版未启用此功能，请使用对话或主终端操作")

    async def mcp(request):
        workspace(request)
        return js({"revision": 0, "user_revision": 0, "items": [], "external": [], "project_names": []})

    async def market(request):
        return js({"plugins": [], "categories": [], "updated_at": "", "source": ""})

    async def operations(request):
        # 本地 commit/discard 已有进程内记录；不伪造活动中的异步远程任务。
        try:
            before = int(request.query.get("before", "0"))
            if before < 0:
                raise ValueError
        except ValueError:
            return problem(400, "invalid_request", "Git 记录游标无效")
        rows = [r for r in hub.git.ops if not before or r["id"] < before]
        page = rows[:100]
        return js({"active": [], "rows": page, "next_before": page[-1]["id"] if len(rows) > 100 else 0})

    async def default(request):
        workspace(request)
        return js({"connection_id": ""})

    async def activity(request):
        return js({"online": [], "workspaces": [], "now": int(time.time() * 1000)})

    async def diagnostics(request):
        workspace(request)
        scope = "session" if "id" in request.match_info else "instance"
        checks = []
        if scope == "instance":
            checks.append({"id": "configuration", "state": "passed", "code": "config_ok"})
        root = hub.workspaces.get(request.match_info["id"])["path"] if scope == "session" else hub.files.state_dir
        checks.append({"id": "workspace_permissions" if scope == "session" else "data_permissions",
                       "state": "passed" if os.access(root, os.R_OK | os.W_OK | os.X_OK) else "failed",
                       "code": "workspace_permissions_ok" if scope == "session" else "data_permissions_ok"})
        # 没有 Docker，也没真的调用模型；上游词表没有 host-direct，诚实标未检查。
        checks.extend({"id": name, "state": "not_checked", "code": "model_not_checked" if name == "model" else "not_requested"}
                      for name in ("docker", "agent_image", "account_configuration", "model"))
        return js({"version": 1, "scope": scope, "checked_at": int(time.time() * 1000),
                   "operation_id": secrets.token_hex(16), "checks": checks}, headers={"Cache-Control": "no-store"})

    async def probe(request):
        workspace(request)
        operation = request.query.get("connection_id", "")
        if len(operation) != 32 or any(c not in "0123456789abcdef" for c in operation):
            operation = secrets.token_hex(16)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "diagnostic", "id": "websocket", "state": "passed",
                            "code": "websocket_ok", "operation_id": operation})
        await ws.close()
        return ws

    r = app.router
    for path in ("/api/sessions/{id}/client-projects", "/api/sessions/{id}/client-terminals",
                 "/api/sessions/{id}/git/bindings", "/api/sessions/{id}/git/terminal-grants",
                 "/api/git/oauth/apps"):
        r.add_get(path, empty)
    r.add_get("/api/marketplace", market)
    r.add_get("/api/mcp", mcp)
    r.add_get("/api/sessions/{id}/mcp", mcp)
    r.add_get("/api/git/operations", operations)
    r.add_get("/api/sessions/{id}/git/default", default)
    r.add_get("/api/announcement/activity", activity)
    for path in ("/api/diagnostics", "/api/sessions/{id}/diagnostics"):
        r.add_post(path, diagnostics)
        r.add_get(path + "/ws", probe)

    # 这些入口在 hub.css 中隐藏；直接调用仍返回 JSON 错误，不执行隐含的远程/账号操作。
    for path in ("/api/sessions/{id}/client-projects", "/api/sessions/{id}/client-projects/{project}",
                 "/api/sessions/{id}/client-terminals", "/api/sessions/{id}/client-terminals/{terminal}",
                 "/api/sessions/{id}/client-terminals/{terminal}/stream",
                 "/api/sessions/{id}/skills/market", "/api/mcp/{tail:.*}", "/api/mcp",
                 "/api/sessions/{id}/mcp/{tail:.*}", "/api/sessions/{id}/mcp",
                 "/api/session-creations/{request}/upload", "/api/session-creations/{request}/git",
                 "/api/git/connections/{tail:.*}", "/api/git/connections", "/api/git/oauth/{tail:.*}",
                 "/api/git/operations/{tail:.*}", "/api/me/git/default", "/api/sessions/{id}/git/default",
                 "/api/sessions/{id}/git/bindings", "/api/sessions/{id}/git/terminal-grants/{tail:.*}",
                 "/api/sessions/{id}/git/terminal-grants", "/api/sessions/{id}/git/branches",
                 "/api/sessions/{id}/git/branches/{tail:.*}", "/api/sessions/{id}/git/remotes",
                 "/api/sessions/{id}/git/remotes/{tail:.*}", "/api/sessions/{id}/git/reviews",
                 "/api/sessions/{id}/git/reviews/{tail:.*}", "/api/sessions/{id}/git/fetch",
                 "/api/sessions/{id}/git/pull", "/api/sessions/{id}/git/push",
                 "/api/sessions/{id}/git/push-preview", "/api/sessions/{id}/git/clone",
                 "/api/pricing/check", "/api/pricing/apply", "/api/pricing/restore", "/api/icons", "/api/announcement",
                 "/api/users/{name}", "/api/users/{name}/{action}", "/api/proxies/{tail:.*}",
                 "/api/tunnel/probe", "/api/tunnel/clients/{tail:.*}",
                 "/api/extensions/{tail:.*}", "/api/sessions/{id}/browser/clipboard",
                 "/api/sessions/{id}/browser/desktop",
                 "/api/sessions/{id}/delegations/{tail:.*}", "/api/themes/site/{tid}"):
        # aiohttp 按注册顺序匹配；先注册的实际 GET/写处理器始终优先。
        r.add_route("*", path, disabled)
