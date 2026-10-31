"""
技能（2026-10-27）：agentbox 前端的「技能」页和官方插件市场。接口格式照 agentbox 的
internal/server/skills.go。

两个范围换成 lab 上 Claude Code 自己认的两个技能目录：
- 本空间（session）：项目目录下的 .claude/skills，只在这个项目里生效。
- 我的模板（template）：~/.claude/skills，所有项目都生效（agentbox 里是「启动时同步到各空间」，这里本来就是共用的）。
本空间的列表里也会带上「我的模板」里的技能（标成我的模板），因为它们在这个项目里同样生效。

官方插件市场 10-27 按机主的意思删了（入口藏在 web/hub/hub.css）；装插件在终端里 claude plugin install。
"""
import asyncio
import datetime
import json
import os
import re
import shutil
import stat
import tempfile

from aiohttp import web

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MANIFEST = "SKILL.md"
MAX_READ = 256 << 10
MAX_ENTRIES = 2000


def iso(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat() if ts else "0001-01-01T00:00:00Z"


def parse_description(text):
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return ""
    body = text[4:]
    end = body.find("\n---")
    if end >= 0:
        body = body[:end]
    for line in body.split("\n"):
        if line.startswith("description:"):
            return line[len("description:"):].strip().strip("\"'")
    return ""


class Skills:
    def __init__(self, hub, state_dir, home, error):
        self.hub = hub
        self.Error = error
        self.user_root = os.path.join(home, ".claude", "skills")

    def root(self, w, scope):
        if scope in (None, "", "session"):
            return os.path.join(w["path"], ".claude", "skills")
        if scope == "template":
            return self.user_root
        raise self.Error("invalid scope")

    def skill_dir(self, w, scope, name, fallback=True):
        """技能目录。本空间范围里找不到时，退到「我的模板」（它在本空间里同样生效）。"""
        if not NAME_RE.match(name or ""):
            raise self.Error("技能名无效")
        d = os.path.join(self.root(w, scope), name)
        if not os.path.isdir(d) and fallback and scope in (None, "", "session"):
            alt = os.path.join(self.user_root, name)
            if os.path.isdir(alt):
                return alt, "template"
        if not os.path.isdir(d):
            raise self.Error("技能不存在", status=404)
        return d, ("template" if scope == "template" else "session")

    @staticmethod
    def describe(d, name, source):
        info = {"name": name, "description": "", "files": 0, "bytes": 0, "updated_at": iso(0), "source": source}
        try:
            with open(os.path.join(d, MANIFEST), encoding="utf-8", errors="replace") as f:
                info["description"] = parse_description(f.read(8 << 10))
        except OSError:
            pass
        latest = 0
        for base, dirs, files in os.walk(d):
            for n in files:
                try:
                    st = os.stat(os.path.join(base, n))
                except OSError:
                    continue
                info["files"] += 1
                info["bytes"] += st.st_size
                latest = max(latest, st.st_mtime)
        info["updated_at"] = iso(latest)
        return info

    def list(self, w, scope):
        out, seen = [], set()
        roots = [(self.root(w, scope), "template" if scope == "template" else "session")]
        if scope in (None, "", "session") and os.path.realpath(roots[0][0]) != os.path.realpath(self.user_root):
            roots.append((self.user_root, "template"))
        for root, source in roots:
            try:
                names = sorted(os.listdir(root))
            except OSError:
                continue
            for n in names:
                d = os.path.join(root, n)
                # 只列真正的技能（带 SKILL.md）；~/.claude/skills/synced 是 Claude Code 从账号同步的，它自己管
                if n in seen or not NAME_RE.match(n) or not os.path.isfile(os.path.join(d, MANIFEST)):
                    continue
                seen.add(n)
                out.append(self.describe(d, n, source))
        out.sort(key=lambda s: s["name"])
        return out

    def detail(self, w, scope, name):
        d, source = self.skill_dir(w, scope, name)
        det = {**self.describe(d, name, source), "content": "", "truncated": False, "entries": [], "more": False}
        try:
            with open(os.path.join(d, MANIFEST), "rb") as f:
                raw = f.read(MAX_READ + 1)
            if len(raw) > MAX_READ:
                raw, det["truncated"] = raw[:MAX_READ], True
            det["content"] = raw.decode(errors="replace")
        except OSError:
            pass
        for base, dirs, files in os.walk(d):
            dirs.sort()
            for n in sorted(dirs) + sorted(files):
                p = os.path.join(base, n)
                if len(det["entries"]) >= MAX_ENTRIES:
                    det["more"] = True
                    return det
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                is_dir, link = stat.S_ISDIR(st.st_mode), stat.S_ISLNK(st.st_mode)
                if not is_dir and not link and not stat.S_ISREG(st.st_mode):
                    continue
                e = {"path": os.path.relpath(p, d).replace(os.sep, "/"), "size": st.st_size, "mtime": iso(st.st_mtime)}
                if is_dir:
                    e["dir"] = True
                if link:
                    e["link"] = True
                if not is_dir and st.st_mode & 0o111:
                    e["exec"] = True
                det["entries"].append(e)
        return det

    def file_path(self, w, scope, name, rel):
        d, _ = self.skill_dir(w, scope, name)
        rel = (rel or "").replace("\\", "/")
        norm = os.path.normpath(rel) if rel else ""
        if not rel or os.path.isabs(rel) or norm == ".." or norm.startswith("../"):
            raise self.Error("路径不对")
        p = os.path.join(d, norm)
        if not os.path.isfile(p):
            raise self.Error("文件或目录不存在", status=404)
        return p, norm

    def remove(self, w, scope, name):
        if not NAME_RE.match(name or ""):
            raise self.Error("技能名无效")
        d = os.path.join(self.root(w, scope), name)
        if not os.path.isdir(d):
            if scope in (None, "", "session") and os.path.isdir(os.path.join(self.user_root, name)):
                raise self.Error("这是「我的模板」里的技能（~/.claude/skills），所有项目共用；要删请切到「我的模板」")
            raise self.Error("技能不存在", status=404)
        shutil.rmtree(d) if not os.path.islink(d) else os.unlink(d)

    @staticmethod
    def replace_dir(src, dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = tempfile.mkdtemp(dir=os.path.dirname(dst), prefix=".hub-skill-")
        try:
            target = os.path.join(tmp, "new")
            shutil.copytree(src, target, symlinks=False, ignore=shutil.ignore_patterns(".git"))
            old = None
            if os.path.lexists(dst):
                old = os.path.join(tmp, "old")
                os.rename(dst, old)
            os.rename(target, dst)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def copy(self, w, scope, name, to):
        src, _ = self.skill_dir(w, scope, name)
        if to not in ("session", "template"):
            raise self.Error("invalid target scope")
        dst = os.path.join(self.root(w, to), name)
        if os.path.realpath(src) == os.path.realpath(dst):
            raise self.Error("源和目标是同一个范围")
        self.replace_dir(src, dst)

    # ---- 上传安装 ----

    def stage(self, tmp, filename, name, stage):
        lower = filename.lower()
        if lower.endswith((".zip", ".tar.gz", ".tgz", ".tar")):
            out = os.path.join(stage, "unpacked")
            os.makedirs(out)
            self.hub.files.extract(tmp, filename, out)
            if os.path.isfile(os.path.join(out, MANIFEST)):
                base = os.path.basename(filename)
                for ext in (".tar.gz", ".tgz", ".zip", ".tar"):
                    if base.lower().endswith(ext):
                        base = base[:-len(ext)]
                        break
                return out, name or base
            dirs = [e for e in os.listdir(out) if os.path.isdir(os.path.join(out, e))]
            if len(dirs) == 1 and os.path.isfile(os.path.join(out, dirs[0], MANIFEST)):
                return os.path.join(out, dirs[0]), name or dirs[0]
            raise self.Error(f"压缩包里没找到 {MANIFEST}（技能包应当是技能目录本身，或只含一层同名目录）")
        if not lower.endswith(".md"):
            raise self.Error("只支持 .md（单文件技能）或 .zip/.tar.gz（技能目录打包）")
        d = os.path.join(stage, "single")
        os.makedirs(d)
        shutil.copyfile(tmp, os.path.join(d, MANIFEST))
        return d, name or os.path.splitext(os.path.basename(filename))[0]


def register(app, api):
    hub = api.hub
    sk: Skills = hub.skills
    Error = api.Error

    def js(data, status=200):
        return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def ws(request):
        return hub.workspaces.get(request.match_info["id"])

    async def list_(request):
        return js({"skills": await asyncio.to_thread(sk.list, ws(request), request.query.get("scope"))})

    async def get(request):
        return js(await asyncio.to_thread(sk.detail, ws(request), request.query.get("scope"),
                                          request.match_info["name"]))

    async def file(request):
        p, rel = sk.file_path(ws(request), request.query.get("scope"), request.match_info["name"],
                              request.query.get("path"))
        st = os.stat(p)
        if request.query.get("raw") == "1":
            if request.query.get("dl") == "1":
                from hub_files import disposition
                return web.FileResponse(p, headers={"Content-Disposition": disposition(os.path.basename(p)),
                                                    "Content-Type": "application/octet-stream"})
            from hub_files import content_type, SCRIPTABLE
            ct = content_type(p)
            headers = {"Content-Type": ct, "X-Content-Type-Options": "nosniff"}
            if ct.split(";")[0] in SCRIPTABLE:
                headers["Content-Security-Policy"] = "sandbox"
            return web.FileResponse(p, headers=headers)
        with open(p, "rb") as f:
            raw = f.read(MAX_READ + 1)
        out = {"path": rel, "size": st.st_size, "mode": stat.filemode(st.st_mode), "exec": bool(st.st_mode & 0o111),
               "mtime": iso(st.st_mtime), "content": "", "truncated": False, "binary": False}
        if len(raw) > MAX_READ:
            raw, out["truncated"] = raw[:MAX_READ], True
        if b"\x00" in raw:
            out["binary"] = True
        else:
            try:
                out["content"] = raw.decode() if not out["truncated"] else raw.decode(errors="ignore")
            except UnicodeDecodeError:
                out["binary"] = True
        return js(out)

    async def delete(request):
        await asyncio.to_thread(sk.remove, ws(request), request.query.get("scope"), request.match_info["name"])
        return js({"ok": True})

    async def copy(request):
        b = await api.body(request)
        name = request.match_info["name"]
        await asyncio.to_thread(sk.copy, ws(request), request.query.get("scope"), name, b.get("to"))
        return js({"ok": True, "name": name})

    async def install(request):
        w = ws(request)
        root = sk.root(w, request.query.get("scope"))
        os.makedirs(hub.files.state_dir, exist_ok=True)
        stage = tempfile.mkdtemp(dir=hub.files.state_dir, prefix="skill-")
        try:
            tmp, orig, fields = await hub.files.save_upload(request, stage)
            src, name = await asyncio.to_thread(sk.stage, tmp, orig, (fields.get("name") or "").strip(), stage)
            if not NAME_RE.match(name):
                raise Error("技能名无效（只能用字母、数字、. _ -，且不超过 64 字符）")
            await asyncio.to_thread(sk.replace_dir, src, os.path.join(root, name))
            return js({"ok": True, "name": name})
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    r = app.router
    r.add_get("/api/sessions/{id}/skills", list_)
    r.add_post("/api/sessions/{id}/skills", install)
    r.add_get("/api/sessions/{id}/skills/{name}", get)
    r.add_get("/api/sessions/{id}/skills/{name}/file", file)
    r.add_delete("/api/sessions/{id}/skills/{name}", delete)
    r.add_post("/api/sessions/{id}/skills/{name}/copy", copy)
