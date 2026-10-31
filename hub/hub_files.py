"""
文件（2026-10-27）：agentbox 前端的「文件」页、聊天附件上传、HTML 预览直链、输入框里的 @ 文件搜索。
接口和返回格式照 agentbox 的 internal/server/files.go、preview.go、images.go、file_mentions.go。

两个范围，跟 agentbox 一样：
- workspace：工作空间目录本身，就是 lab 上的真实项目目录。页面上显示成 /workspace。
- shared：跨空间共享的目录 ~/.agent-hub/shared。聊天里粘贴的图片、附件放在它下面的 .images / .file，
  页面上显示成 /shared/…；发给 Claude 之前换成真实路径（hub_abx 里做），Claude 才读得到。

agentbox 的文件区是容器写的，所以它处处防符号链接；这里文件本来就是你自己的（网页上还有终端），
只挡请求里的路径跳出范围（.. 和绝对路径），目录里的符号链接照常跟随。
"""
import asyncio
import datetime
import mimetypes
import os
import queue
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import threading
import time
import urllib.parse
import zipfile

from aiohttp import web

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
ATTACH_EXT = re.compile(r"^\.[a-z0-9]{1,10}$")
ATTACH_PATH = re.compile(r"^/shared/\.(images|file)/[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
MAX_UPLOAD = 512 << 20
MAX_PUT = 16 << 20
PREVIEW_TTL = 30 * 60
PREVIEW_MAX_INJECT = 8 << 20
SCRIPTABLE = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml"}
TEXTISH = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".sh", ".md", ".json", ".yaml", ".yml", ".toml",
           ".ini", ".cfg", ".conf", ".kt", ".kts", ".java", ".c", ".h", ".cpp", ".hpp", ".css", ".txt", ".log",
           ".env", ".gradle", ".properties", ".sql", ".lua", ".rb", ".php", ".vue", ".svelte", ".service"}


def iso_local(ts):
    return datetime.datetime.fromtimestamp(ts).astimezone().isoformat()


def is_archive(name):
    n = name.lower()
    return n.endswith((".zip", ".tar.gz", ".tgz", ".tar"))


def content_type(path):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext in TEXTISH:
        return "text/plain; charset=utf-8"
    ct = mimetypes.guess_type(name)[0]
    if ct:
        return ct + ("; charset=utf-8" if ct.startswith("text/") else "")
    try:   # 没扩展名：看开头像不像文本（跟 Go 的 DetectContentType 一个意思）
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return "application/octet-stream"
    if b"\x00" in head:
        return "application/octet-stream"
    low = head.lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html")):
        return "text/html; charset=utf-8"
    try:
        head.decode()
    except UnicodeDecodeError as e:
        if e.start < len(head) - 4:   # 末尾可能是被截断的多字节字符
            return "application/octet-stream"
    return "text/plain; charset=utf-8"


def disposition(name):
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


# 预览页里的原型脚本跑在没有同源权限的沙箱里，localStorage、history.pushState 一碰就抛错。
# 照 agentbox preview.go 的 previewShim 换成内存实现（Apache-2.0）。
PREVIEW_SHIM = """<script>
(function () {
  "use strict";
  var need = false;
  try { void window.localStorage; } catch (e) { need = true; }
  if (need) {
    var mem = function () {
      var map = new Map();
      var api = {
        getItem: function (k) { k = String(k); return map.has(k) ? map.get(k) : null; },
        setItem: function (k, v) { map.set(String(k), String(v)); },
        removeItem: function (k) { map.delete(String(k)); },
        clear: function () { map.clear(); },
        key: function (i) { var ks = Array.from(map.keys()); i = Number(i); return i >= 0 && i < ks.length ? ks[i] : null; }
      };
      var own = function (p) { return typeof p === "string" && Object.prototype.hasOwnProperty.call(api, p); };
      return new Proxy(api, {
        get: function (t, p) {
          if (p === "length") return map.size;
          if (own(p) || typeof p !== "string") return t[p];
          return map.has(p) ? map.get(p) : undefined;
        },
        set: function (t, p, v) {
          if (typeof p === "string" && p !== "length" && !own(p)) map.set(p, String(v));
          return true;
        },
        has: function (t, p) { return (typeof p === "string" && map.has(p)) || p in t; },
        deleteProperty: function (t, p) { if (typeof p === "string") map.delete(p); return true; },
        ownKeys: function () { return Array.from(map.keys()); },
        getOwnPropertyDescriptor: function (t, p) {
          if (typeof p === "string" && map.has(p)) {
            return { value: map.get(p), writable: true, enumerable: true, configurable: true };
          }
          return undefined;
        }
      });
    };
    ["localStorage", "sessionStorage"].forEach(function (n) {
      try { Object.defineProperty(window, n, { value: mem(), configurable: true }); } catch (e) {}
    });
  }
  ["pushState", "replaceState"].forEach(function (m) {
    var orig = window.history && window.history[m];
    if (typeof orig !== "function") return;
    try {
      window.history[m] = function () {
        try { return orig.apply(window.history, arguments); } catch (e) {}
      };
    } catch (e) {}
  });
})();
</script>""".encode()


def inject_shim(raw):
    head = raw[:65536].lower()
    for tag in (b"<head", b"<html"):
        i = head.find(tag)
        while i >= 0 and i + len(tag) < len(head) and head[i + len(tag):i + len(tag) + 1] not in (b">", b" ", b"\t",
                                                                                                    b"\n", b"\r", b"/"):
            i = head.find(tag, i + 1)   # <head 也是 <header 的前缀
        if i >= 0:
            j = head.find(b">", i)
            if j >= 0:
                return raw[:j + 1] + PREVIEW_SHIM + raw[j + 1:]
    return PREVIEW_SHIM + raw


class Files:
    def __init__(self, hub, state_dir, home, error):
        self.hub = hub
        self.Error = error
        self.home = home
        self.state_dir = os.path.realpath(state_dir)
        self.shared = os.path.join(self.state_dir, "shared")
        self.code_dir = os.path.dirname(os.path.realpath(__file__))
        self.previews: dict[str, tuple[str, float]] = {}   # 通行证 → (目录, 过期时间)

    # ---- 范围与路径 ----

    def root(self, w, scope):
        if scope in (None, "", "workspace"):
            return w["path"]
        if scope == "shared":
            os.makedirs(self.shared, exist_ok=True)
            return self.shared
        raise self.Error("invalid scope")

    def resolve(self, root, rel, allow_root=True):
        rel = (rel or "").replace("\\", "/")
        if "\x00" in rel:
            raise self.Error("路径不对")
        norm = os.path.normpath(rel) if rel.strip("/") else "."
        if os.path.isabs(rel) or norm == ".." or norm.startswith("../"):
            raise self.Error("路径不能跳出当前范围")
        if norm == ".":
            if not allow_root:
                raise self.Error("不能对根目录这样操作")
            return root
        return os.path.join(root, norm)

    @staticmethod
    def rel_of(root, path):
        return os.path.relpath(path, root).replace(os.sep, "/")

    def leaf(self, name):
        name = (name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            raise self.Error("名字不对：不能为空，也不能带 / 或 \\")
        return name

    def not_found(self):
        return self.Error("文件或目录不存在", status=404)

    # ---- 列目录、读写 ----

    def entry(self, d, name):
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            try:
                st = os.lstat(p)   # 断掉的符号链接也列出来
            except OSError:
                return None
        return {"name": name, "is_dir": stat.S_ISDIR(st.st_mode), "size": st.st_size,
                "mode": stat.filemode(st.st_mode), "mtime": iso_local(st.st_mtime)}

    def listdir(self, root, rel):
        d = self.resolve(root, rel)
        try:
            names = os.listdir(d)
        except FileNotFoundError:
            raise self.not_found()
        except NotADirectoryError:
            raise self.Error("不是目录")
        except PermissionError:
            raise self.Error("没有权限读这个目录", status=403)
        out = [e for e in (self.entry(d, n) for n in names) if e]
        out.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return out

    def write(self, path, data):
        path = os.path.realpath(path)   # 符号链接：写到它指向的文件，链接本身不动
        if os.path.isdir(path):
            raise self.Error("这是目录，不能当文件写")
        mode = 0o644
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            pass
        d = os.path.dirname(path)
        if not os.path.isdir(d):
            raise self.Error("上级目录不存在", status=404)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".hub-save-")   # 同目录写临时文件再替换：写一半断了原文件还在
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        return os.stat(path)

    def remove(self, root, rel):
        p = self.resolve(root, rel, allow_root=False)
        if not os.path.lexists(p):
            raise self.not_found()
        if os.path.isdir(p) and not os.path.islink(p):
            self.guard_tree(p)
            shutil.rmtree(p)
        else:
            os.unlink(p)

    def guard_tree(self, p):
        """整棵删 / 清空之前：home、hub 自己的代码和状态目录不许动。"""
        real = os.path.realpath(p)
        for keep in (self.home, self.code_dir, self.state_dir):
            if real == keep or (keep + os.sep).startswith(real + os.sep):
                raise self.Error(f"{p} 太要紧了，不在网页上删", status=403)

    def move(self, src_root, src_rel, dst_root, dst_dir):
        src = self.resolve(src_root, src_rel, allow_root=False)
        dst_parent = self.resolve(dst_root, dst_dir)
        if not os.path.lexists(src):
            raise self.not_found()
        if not os.path.isdir(dst_parent):
            raise self.Error("目标目录不存在", status=404)
        target = os.path.join(dst_parent, os.path.basename(src))
        if os.path.realpath(src) == os.path.realpath(target):
            raise self.Error("已经在这个目录里了")
        if os.path.isdir(src) and (os.path.realpath(dst_parent) + os.sep).startswith(os.path.realpath(src) + os.sep):
            raise self.Error("不能把目录移进它自己里面")
        if os.path.lexists(target):
            raise self.Error("目标目录中已存在同名文件或目录", status=409)
        shutil.move(src, target)
        return self.rel_of(dst_root, target)

    # ---- 上传 ----

    async def save_upload(self, request, dest):
        """multipart 里的 file 字段存到 dest 目录下的临时文件，返回 (临时路径, 原文件名, 其余表单字段)。"""
        reader = await request.multipart()
        fields, tmp, orig = {}, None, ""
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "file":
                orig = os.path.basename((part.filename or "upload").replace("\\", "/")) or "upload"
                fd, tmp = tempfile.mkstemp(dir=dest, prefix=".hub-upload-")
                size = 0
                with os.fdopen(fd, "wb") as f:
                    while True:
                        chunk = await part.read_chunk(1 << 20)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_UPLOAD:
                            f.close()
                            os.unlink(tmp)
                            raise self.Error(f"文件太大（上限 {MAX_UPLOAD >> 20}MB）", status=413)
                        f.write(chunk)
            else:
                fields[part.name] = (await part.text())[:200]
        if tmp is None:
            raise self.Error("missing 'file' field")
        return tmp, orig, fields

    def extract(self, archive, orig, stage):
        """解压到 stage（临时目录）。挡掉跳出目录的条目、符号链接和特殊文件。返回文件数。"""
        count = 0

        def safe(name):
            norm = os.path.normpath(name.replace("\\", "/")).lstrip("/")
            if not norm or norm == "." or norm == ".." or norm.startswith("../"):
                return None
            return os.path.join(stage, norm)

        if orig.lower().endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    out = safe(info.filename)
                    if out is None:
                        continue
                    if info.is_dir():
                        os.makedirs(out, exist_ok=True)
                        continue
                    if stat.S_ISLNK(info.external_attr >> 16):
                        continue
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with z.open(info) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode:
                        os.chmod(out, mode | 0o600)
                    count += 1
        else:
            with tarfile.open(archive) as t:
                for m in t:
                    out = safe(m.name)
                    if out is None:
                        continue
                    if m.isdir():
                        os.makedirs(out, exist_ok=True)
                    elif m.isfile():
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with t.extractfile(m) as src, open(out, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
                        os.chmod(out, (m.mode & 0o777) | 0o600)
                        count += 1
        return count

    def merge(self, src, dst):
        """上传解好的内容并进目标目录：同名文件覆盖，同名目录逐层合并。"""
        for name in os.listdir(src):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            if os.path.isdir(s) and os.path.isdir(d) and not os.path.islink(d):
                self.merge(s, d)
                continue
            if os.path.isdir(d) and not os.path.islink(d):
                shutil.rmtree(d)
            elif os.path.lexists(d):
                os.unlink(d)
            shutil.move(s, d)   # 跨文件系统时 os.replace 会失败，shutil.move 会退回复制

    def clear(self, d):
        self.guard_tree(d)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p)
            else:
                os.unlink(p)

    # ---- 打包下载 ----

    def zip_into(self, root, sink):
        class Sink:
            def write(self, b):
                sink.put(bytes(b))
                return len(b)

            def flush(self):
                pass

        try:
            with zipfile.ZipFile(Sink(), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for d, dirs, names in os.walk(root):
                    dirs.sort()
                    rel = os.path.relpath(d, root)
                    if rel != "." and not names and not dirs:
                        z.writestr(rel.replace(os.sep, "/") + "/", b"")
                    for n in sorted(names):
                        p = os.path.join(d, n)
                        if os.path.islink(p) and not os.path.exists(p) or not os.path.isfile(p):
                            continue
                        try:
                            z.write(p, os.path.relpath(p, root).replace(os.sep, "/"))
                        except OSError:
                            continue
        finally:
            sink.put(None)

    # ---- 预览通行证 ----

    def grant(self, d):
        now = time.time()
        for k, (dd, exp) in list(self.previews.items()):
            if exp < now:
                del self.previews[k]
        for k, (dd, exp) in self.previews.items():
            if dd == d:
                self.previews[k] = (dd, now + PREVIEW_TTL)
                return k
        if len(self.previews) >= 200:
            raise self.Error("预览链接过多，请稍后再试", status=503)
        k = secrets.token_urlsafe(16)
        self.previews[k] = (d, now + PREVIEW_TTL)
        return k

    # ---- @ 文件搜索 ----

    async def mentions(self, root, q, limit):
        files = await self.hub.workspaces.files(root)
        dirs = sorted({f.rsplit("/", 1)[0] for f in files if "/" in f})
        items = [(f, False) for f in files] + [(d, True) for d in dirs]
        q = (q or "").lower().strip()
        if not q:
            return [{"path": p, **({"dir": True} if is_dir else {})} for p, is_dir in items[:limit]], len(files) >= 50000
        scored = []
        for p, is_dir in items:
            pl = p.lower()
            base = pl.rsplit("/", 1)[-1]
            if base.startswith(q):
                score = 0
            elif q in base:
                score = 1
            elif q in pl:
                score = 2
            else:
                it = iter(pl)
                if not all(c in it for c in q):
                    continue
                score = 3
            scored.append((score, is_dir, len(p), p))
        scored.sort()
        return [{"path": p, **({"dir": True} if d else {})} for _, d, _, p in scored[:limit]], len(files) >= 50000


def register(app, api):
    hub = api.hub
    fs: Files = hub.files
    Error = api.Error

    def js(data, status=200):
        return web.json_response(data, status=status)

    def ctx(request, scope=None):
        w = hub.workspaces.get(request.match_info["id"])
        return w, fs.root(w, request.query.get("scope") if scope is None else scope)

    async def json_body(request):
        try:
            b = await request.json()
        except ValueError:
            raise Error("invalid request body")
        return b if isinstance(b, dict) else {}

    async def list_files(request):
        _, root = ctx(request)
        return js(await asyncio.to_thread(fs.listdir, root, request.query.get("path")))

    async def get_file(request):
        w, root = ctx(request)
        p = fs.resolve(root, request.query.get("path"), allow_root=False)
        if not os.path.isfile(p):
            raise fs.not_found()
        name = os.path.basename(p)
        if request.query.get("dl") == "1":
            return web.FileResponse(p, headers={"Content-Disposition": disposition(name),
                                                "Content-Type": "application/octet-stream"})
        ct = content_type(p)
        headers = {"Content-Type": ct, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
        if ct.split(";")[0].strip() in SCRIPTABLE:   # HTML/SVG 当文档打开时不许跑脚本（会读到登录令牌）
            headers["Content-Security-Policy"] = "sandbox"
        return web.FileResponse(p, headers=headers)

    async def put_file(request):
        _, root = ctx(request)
        p = fs.resolve(root, request.query.get("path"), allow_root=False)
        data = bytearray()
        async for chunk in request.content.iter_chunked(1 << 16):
            data += chunk
            if len(data) > MAX_PUT:
                raise Error("file too large (max 16MB)", status=413)
        st = await asyncio.to_thread(fs.write, p, bytes(data))
        return js({"size": len(data), "mtime": iso_local(st.st_mtime)})

    async def delete_file(request):
        _, root = ctx(request)
        await asyncio.to_thread(fs.remove, root, request.query.get("path"))
        return js({"deleted": True})

    async def move_file(request):
        w = hub.workspaces.get(request.match_info["id"])
        b = await json_body(request)
        src_root, dst_root = fs.root(w, b.get("source_scope")), fs.root(w, b.get("destination_scope"))
        path = await asyncio.to_thread(fs.move, src_root, b.get("source_path"), dst_root, b.get("destination_dir"))
        return js({"scope": b.get("destination_scope") or "", "path": path})

    async def mkdir(request):
        w = hub.workspaces.get(request.match_info["id"])
        b = await json_body(request)
        root = fs.root(w, b.get("scope"))
        parent = fs.resolve(root, b.get("dir"))
        name = fs.leaf(b.get("name"))
        target = os.path.join(parent, name)
        if os.path.lexists(target):
            raise Error("目标目录中已存在同名文件或目录", status=409)
        if not os.path.isdir(parent):
            raise fs.not_found()
        os.mkdir(target, 0o755)
        return js({"path": fs.rel_of(root, target)})

    async def rename(request):
        w = hub.workspaces.get(request.match_info["id"])
        b = await json_body(request)
        root = fs.root(w, b.get("scope"))
        src = fs.resolve(root, b.get("path"), allow_root=False)
        name = fs.leaf(b.get("name"))
        if not os.path.lexists(src):
            raise fs.not_found()
        target = os.path.join(os.path.dirname(src), name)
        if target != src:
            if os.path.lexists(target):
                raise Error("目标目录中已存在同名文件或目录", status=409)
            os.rename(src, target)
        return js({"path": fs.rel_of(root, target)})

    async def upload(request):
        _, root = ctx(request)
        target = fs.resolve(root, request.query.get("path"))
        if not os.path.isdir(target):
            raise fs.not_found()
        stage = tempfile.mkdtemp(dir=fs.state_dir, prefix="upload-")
        try:
            tmp, orig, fields = await fs.save_upload(request, stage)
            content = os.path.join(stage, "content")
            os.mkdir(content)
            if is_archive(orig):
                mode = "archive"
                try:
                    count = await asyncio.to_thread(fs.extract, tmp, orig, content)
                except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
                    raise Error(f"压缩包解不开：{e}")
            else:
                mode, count = "file", 1
                os.replace(tmp, os.path.join(content, fs.leaf(orig)))
            if (request.query.get("clear") or fields.get("clear")) == "1":
                await asyncio.to_thread(fs.clear, target)
            await asyncio.to_thread(fs.merge, content, target)
            return js({"files": count, "mode": mode})
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    async def archive(request):
        w, root = ctx(request)
        name = "shared.zip" if request.query.get("scope") == "shared" else f"{w['name']}-workspace.zip"
        resp = web.StreamResponse(headers={"Content-Type": "application/zip", "Content-Disposition": disposition(name)})
        await resp.prepare(request)
        sink = queue.Queue(maxsize=32)
        threading.Thread(target=fs.zip_into, args=(root, sink), daemon=True).start()
        try:
            while True:
                chunk = await asyncio.to_thread(sink.get)
                if chunk is None:
                    break
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError):
            # 浏览器取消了下载：把队列抽干，让打包线程走完退出
            threading.Thread(target=lambda: [None for _ in iter(sink.get, None)], daemon=True).start()
            raise
        await resp.write_eof()
        return resp

    async def preview_grant(request):
        _, root = ctx(request)
        p = fs.resolve(root, request.query.get("path"), allow_root=False)
        if not os.path.isfile(p):
            raise fs.not_found()
        k = fs.grant(os.path.dirname(p))
        return js({"url": f"/preview/{k}/{urllib.parse.quote(os.path.basename(p))}", "expires_in": PREVIEW_TTL})

    async def preview_serve(request):
        """按通行证直出文件，不走登录（子资源是浏览器按相对路径发的，带不上令牌）。只读、只限那一个目录。"""
        g = fs.previews.get(request.match_info["grant"])
        if not g or g[1] < time.time():
            return web.Response(status=404, text="预览链接已失效，请重新打开预览")
        base = g[0]
        rel = request.match_info["tail"] or "index.html"
        try:
            p = fs.resolve(base, rel, allow_root=False)
        except Error:
            return web.Response(status=404, text="文件不存在")
        if not os.path.isfile(p):
            return web.Response(status=404, text="文件不存在")
        headers = {"Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals "
                                              "allow-downloads allow-pointer-lock",
                   "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}
        if p.lower().endswith((".html", ".htm")) and os.path.getsize(p) <= PREVIEW_MAX_INJECT:
            with open(p, "rb") as f:
                body = inject_shim(f.read())
            return web.Response(body=body, headers={**headers, "Content-Type": "text/html; charset=utf-8"})
        ct = mimetypes.guess_type(p)[0] or content_type(p)
        return web.FileResponse(p, headers={**headers, "Content-Type": ct})

    async def images(request):
        w = hub.workspaces.get(request.match_info["id"])
        os.makedirs(fs.shared, exist_ok=True)
        tmp, orig, _ = await fs.save_upload(request, fs.shared)
        try:
            ext = os.path.splitext(orig)[1].lower()
            if not ATTACH_EXT.match(ext):
                ext = ".bin"
            sub = ".images" if ext in IMAGE_EXTS else ".file"
            os.makedirs(os.path.join(fs.shared, sub), exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3) + ext
            os.replace(tmp, os.path.join(fs.shared, sub, name))
            os.chmod(os.path.join(fs.shared, sub, name), 0o644)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        return js({"name": name, "orig": orig, "path": f"/shared/{sub}/{name}"})

    async def validate(request):
        hub.workspaces.get(request.match_info["id"])
        b = await json_body(request)
        paths = b.get("paths")
        if not isinstance(paths, list) or len(paths) > 64 or not all(isinstance(p, str) and ATTACH_PATH.match(p)
                                                                       for p in paths):
            return js({"code": "invalid_request", "error": "附件路径不对"}, status=400)
        return web.json_response({"valid": [os.path.isfile(os.path.join(fs.shared, p[len("/shared/"):]))
                                            for p in paths]}, headers={"Cache-Control": "no-store"})

    async def search(request):
        w = hub.workspaces.get(request.match_info["id"])
        q = request.query.get("q") or ""
        if len(q) > 512:
            raise Error("query too long")
        try:
            limit = int(request.query.get("limit") or 40)
        except ValueError:
            raise Error("invalid limit")
        if not 1 <= limit <= 100:
            raise Error("invalid limit")
        files, truncated = await fs.mentions(w["path"], q, limit)
        return web.json_response({"files": files, **({"truncated": True} if truncated else {})},
                                 headers={"Cache-Control": "no-store"})

    r = app.router
    r.add_get("/api/sessions/{id}/files", list_files)
    r.add_delete("/api/sessions/{id}/files", delete_file)
    r.add_post("/api/sessions/{id}/files/move", move_file)
    r.add_post("/api/sessions/{id}/files/mkdir", mkdir)
    r.add_post("/api/sessions/{id}/files/rename", rename)
    r.add_get("/api/sessions/{id}/files/search", search)
    r.add_get("/api/sessions/{id}/file", get_file)
    r.add_put("/api/sessions/{id}/file", put_file)
    r.add_post("/api/sessions/{id}/upload", upload)
    r.add_get("/api/sessions/{id}/archive", archive)
    r.add_get("/api/sessions/{id}/preview", preview_grant)
    r.add_post("/api/sessions/{id}/images", images)
    r.add_post("/api/sessions/{id}/attachments/validate", validate)
    r.add_get("/preview/{grant}/{tail:.*}", preview_serve)
