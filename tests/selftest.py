"""littleskin_skin 离线自测。

不依赖 AstrBot 运行时：用假的 astrbot 模块桩把 main.py 拉起来，
再用本地 mock 皮肤站（复刻实测到的 littleskin 行为）跑通全部命令。

用法：
    py tests/selftest.py            # 只跑离线用例
    py tests/selftest.py --live     # 额外对真实站点做只读冒烟测试
"""

from __future__ import annotations

import asyncio
import http.server
import inspect
import io
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import types

# 两种布局都能跑：
#   1) 仓库布局（发布版）：<repo>/main.py + <repo>/tests/selftest.py
#   2) AstrBot 部署沙盒：<proj>/data/plugins/littleskin_skin/ + <proj>/tests/selftest.py
_THIS = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS)
if os.path.exists(os.path.join(_PARENT, "main.py")):
    PLUGIN = _PARENT
    PLUGINS_DIR = os.path.dirname(PLUGIN)
    ROOT = os.path.dirname(PLUGINS_DIR)
else:
    ROOT = _PARENT
    PLUGINS_DIR = os.path.join(ROOT, "data", "plugins")
    PLUGIN = os.path.join(PLUGINS_DIR, "littleskin_skin")
for _p in (ROOT, PLUGIN, PLUGINS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 仓库布局下目录名是 astrbot_plugin_littleskin_skin，而代码里用 `from .render import ...`，
# 必须以 `littleskin_skin` 这个包名导入。用一个只有 __path__ 的空包把两者接上，
# 比 symlink / 改目录名都干净（AstrBot 实际加载时按目录名建包，不受影响）。
if os.path.basename(PLUGIN) != "littleskin_skin" and "littleskin_skin" not in sys.modules:
    _pkg = types.ModuleType("littleskin_skin")
    _pkg.__path__ = [PLUGIN]  # type: ignore[attr-defined]
    sys.modules["littleskin_skin"] = _pkg

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSED if ok else FAILED).append(name)
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


# --------------------------------------------------------------- astrbot 桩


def install_astrbot_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    core = types.ModuleType("astrbot.core")
    star_mod = types.ModuleType("astrbot.api.star")
    event_mod = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    cmd_mod = types.ModuleType("astrbot.core.star.filter.command")

    class Context:
        # 复刻 Context.get_config()：皮肤文件地址前缀会读全局 callback_api_base
        def get_config(self, umo=None):  # noqa: ARG002
            return {"callback_api_base": "http://127.0.0.1:46185"}

    class Result:
        def __init__(self) -> None:
            self.chain: list = []
            self.markdown: bool | None = None

        def message(self, text):
            self.chain.append(Plain(text))
            return self

        def file_image(self, path):
            self.chain.append(Image(path))
            return self

        def use_markdown(self, use=True):
            self.markdown = use
            return self

    class Image:  # astrbot.api.message_components.Image
        def __init__(self, path: str = "") -> None:
            self.path = path

        @classmethod
        def fromFileSystem(cls, path: str) -> Image:  # noqa: N802  复刻真实大小写
            return cls(path)

    class Plain:  # astrbot.api.message_components.Plain
        def __init__(self, text: str = "") -> None:
            self.text = text

    class File:  # astrbot.api.message_components.File
        def __init__(self, name: str = "", file: str = "", url: str = "") -> None:
            self.name = name
            self.file_ = file
            self.url = url

    class MessageChain:  # astrbot.api.event.MessageChain
        def __init__(self, chain=None, type=None) -> None:  # noqa: A002, ARG002
            self.chain = list(chain or [])

    class Event:
        def __init__(self, text: str) -> None:
            self.text = text
            self.unified_msg_origin = "test:local"
            self.sent: list = []

        def get_message_str(self):
            return self.text

        def get_session_id(self):
            return "test:local"

        def make_result(self):
            return Result()

        def plain_result(self, text):
            return Result().message(text)

        def image_result(self, path):
            return Result().file_image(path)

        def get_sender_id(self):
            return "10001"

        def get_group_id(self):
            return ""

        async def send(self, chain):
            """llm 工具和皮肤文件就是用这个发消息的，测试里只记录下来。"""
            self.sent.append(chain)

        def chain_result(self, chain):
            return MessageChain(chain=chain)

    class BaseCustomFilter:
        # 复刻真实 API：CustomFilter.__init__(raise_error=True, **kwargs)
        def __init__(self, raise_error: bool = True, **kwargs) -> None:  # noqa: ARG002
            self.raise_error = raise_error

        def filter(self, event, cfg):  # noqa: ARG002
            return True

    class Star:
        def __init__(self, context=None, config=None):  # noqa: ARG002
            self.context = context
            self.logger = logging.getLogger("littleskin_skin")

    def command_group_decorator(name):
        """模拟 @filter.command_group + @group.command(sub, alias=...) 的注册效果。"""

        def deco(fn):
            fn.group_name = name
            subs: list[tuple[str, frozenset[str]]] = []

            def command(sub, alias=None):
                def inner(f):
                    subs.append((sub, frozenset(alias or ())))
                    return f

                return inner

            fn.command = command
            fn.sub_commands = subs
            return fn

        return deco

    def custom_filter_decorator(cls, raise_error=True):
        # 复刻真实 register_custom_filter：收的是**类**，内部再 cls(raise_error) 实例化。
        # 传实例会在这里 TypeError —— 与线上一致，别放宽。
        instance = cls(raise_error)

        def deco(fn):
            fn.custom_filter = instance
            return fn

        return deco

    _DOC_ARG = re.compile(r"^\s*(\w+)\s*\(\s*([\w\[\]]+)\s*\)\s*:", re.MULTILINE)

    def llm_tool_decorator(name=None):
        """复刻 register_llm_tool：从 docstring 解析参数类型，缺类型当场 ValueError。

        真实实现用 docstring_parser，这里用等价的最小子集：
        每个非 self/event 参数都必须在 docstring 的 `name(string):` 里声明过，否则插件加载失败。
        """

        def deco(fn):
            doc = fn.__doc__ or ""
            declared = {m.group(1): m.group(2) for m in _DOC_ARG.finditer(doc)}
            for arg in inspect.signature(fn).parameters:
                if arg in ("self", "event"):
                    continue
                if not declared.get(arg):
                    raise ValueError(
                        f"LLM 函数工具 {name or fn.__name__} 的参数 {arg} 缺少类型注释。"
                    )
                if declared[arg] not in ("string", "number", "object", "array", "boolean"):
                    raise ValueError(
                        f"LLM 函数工具 {name or fn.__name__} 不支持的参数类型：{declared[arg]}"
                    )
            fn.tool_name = name or fn.__name__
            fn.tool_args = declared
            return fn

        return deco

    class _FilterNS:
        pass

    _FilterNS.CustomFilter = BaseCustomFilter
    _FilterNS.command_group = staticmethod(command_group_decorator)
    _FilterNS.custom_filter = staticmethod(custom_filter_decorator)
    _FilterNS.llm_tool = staticmethod(llm_tool_decorator)

    cmd_mod.GreedyStr = str
    star_mod.Star = Star
    star_mod.Context = Context
    api.star = star_mod
    event_mod.AstrMessageEvent = Event
    event_mod.MessageChain = MessageChain
    event_mod.filter = _FilterNS
    filter_mod.CustomFilter = BaseCustomFilter
    astrbot.api = api
    astrbot.core = core
    comp_mod = types.ModuleType("astrbot.api.message_components")
    comp_mod.Image = Image
    comp_mod.Plain = Plain
    comp_mod.File = File
    api.message_components = comp_mod

    # AstrBot 的文件服务：真实现会把路径登记成一个一次性 token，NapCat 凭它下载字节。
    FILE_TOKENS: list[str] = []

    class _FileTokenService:
        async def register_file(self, path, timeout=None, single_use=True):  # noqa: ARG002
            if not os.path.exists(path):
                raise FileNotFoundError(path)
            FILE_TOKENS.append(path)
            return f"tok{len(FILE_TOKENS)}"

    file_token_mod = types.ModuleType("astrbot.core.file_token_service")
    file_token_mod.register_file = _FileTokenService().register_file
    file_token_mod.FILE_TOKENS = FILE_TOKENS  # 测试断言用
    core.file_token_service = file_token_mod
    core_utils = types.ModuleType("astrbot.core.utils")
    core_utils.__path__ = []
    astrbot_path = types.ModuleType("astrbot.core.utils.astrbot_path")
    astrbot_path.get_astrbot_plugin_data_path = lambda: os.path.join(ROOT, "data", "plugin_data")
    star_filter = types.ModuleType("astrbot.core.star.filter")
    star_filter.__path__ = []
    core_star = types.ModuleType("astrbot.core.star")
    core_star.__path__ = []
    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": star_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.event.filter": filter_mod,
        "astrbot.api.message_components": comp_mod,
        "astrbot.core": core,
        "astrbot.core.file_token_service": file_token_mod,
        "astrbot.core.star": core_star,
        "astrbot.core.star.filter": star_filter,
        "astrbot.core.star.filter.command": cmd_mod,
        "astrbot.core.utils": core_utils,
        "astrbot.core.utils.astrbot_path": astrbot_path,
    }.items():
        sys.modules[name] = mod


# ------------------------------------------------------------ mock 皮肤站


def make_png(w: int, h: int, seed: int = 1) -> bytes:
    from PIL import Image

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = img.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((x * 7 + seed) % 256, (y * 11 + seed) % 256, (x * y) % 256, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


TEXTURES = {
    "aa" * 32: make_png(64, 64, 1),  # 普通皮肤
    "bb" * 32: make_png(64, 32, 2),  # 旧皮肤
    "cc" * 32: make_png(128, 128, 3),  # HD 皮肤
    "dd" * 32: b"not a png at all",  # 坏数据
    "ee" * 32: make_png(1024, 1024, 4),  # 非标准尺寸
}
LIST_ROWS = [
    {"tid": 1, "name": "自由高达", "type": "steve", "uploader": 9, "public": True, "hd": False, "likes": 3, "nickname": "alice"},
    {"tid": 2, "name": "GUNDAM RX-78-2", "type": "alex", "uploader": 8, "public": True, "hd": False, "likes": 13, "nickname": "bob"},
    {"tid": 3, "name": "HD 皮肤", "type": "steve", "uploader": 7, "public": True, "hd": True, "likes": 1, "nickname": "carol"},
    {"tid": 4, "name": "旧皮肤", "type": "steve", "uploader": 6, "public": True, "hd": False, "likes": 0, "nickname": "dave"},
    {"tid": 5, "name": "坏图", "type": "steve", "uploader": 5, "public": True, "hd": False, "likes": 0, "nickname": "eve"},
    {"tid": 6, "name": "超大图", "type": "steve", "uploader": 4, "public": True, "hd": False, "likes": 0, "nickname": "mallory\x1b[31m[0m " + "x" * 80},
]
DETAILS = {
    1: {"tid": 1, "name": "自由高达", "type": "steve", "hash": "aa" * 32, "size": 2, "uploader": 9, "public": True, "upload_at": "2025-12-16 22:17:15", "likes": 3, "nickname": "alice"},
    2: {"tid": 2, "name": "GUNDAM RX-78-2", "type": "alex", "hash": "aa" * 32, "size": 2, "uploader": 8, "public": True, "upload_at": "2025-12-16 22:17:15", "likes": 13, "nickname": "bob"},
    3: {"tid": 3, "name": "HD 皮肤", "type": "steve", "hash": "cc" * 32, "size": 8, "uploader": 7, "public": True, "upload_at": "2026-01-01", "likes": 1, "nickname": "carol"},
    4: {"tid": 4, "name": "旧皮肤", "type": "steve", "hash": "bb" * 32, "size": 1, "uploader": 6, "public": True, "upload_at": "2026-01-01", "likes": 0, "nickname": "dave"},
    5: {"tid": 5, "name": "坏图", "type": "steve", "hash": "dd" * 32, "size": 2, "uploader": 5, "public": True, "upload_at": "2026-01-01", "likes": 0, "nickname": "eve"},
    6: {"tid": 6, "name": "超大图", "type": "steve", "hash": "ee" * 32, "size": 2, "uploader": 4, "public": True, "upload_at": "2026-01-01", "likes": 0, "nickname": "mallory"},
    7: {"tid": 7, "name": "缺字段", "type": None},
}
REQUEST_LOG: list[str] = []

# 第二个站点的数据（多站点用例用，挂在 /b 前缀下）
SECOND_ROWS = [
    {"tid": 101, "name": "B站皮肤", "type": "alex", "uploader": 9, "public": True, "hd": False, "likes": 7, "nickname": "zed"},
]
SECOND_DETAILS = {
    101: {"tid": 101, "name": "B站皮肤", "type": "alex", "hash": "aa" * 32, "size": 2, "uploader": 9, "public": True, "upload_at": "2026-02-02", "likes": 7, "nickname": "zed"},
}

# 第三个站：字段名/列表包装/路由全不一样（验证“改配置就能接”）
THIRD_ROWS = [
    {"id": 201, "title": "C站皮肤", "model": "alex", "likes": 9, "author": "carl"},
]


class MockHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        REQUEST_LOG.append(path)
        # /b/ = 第二个站点（字段与 littleskin 同构）；/c/ = 第三个站点（字段名/路由全不一样）
        second = path == "/b" or path.startswith("/b/")
        third = (not second) and (path == "/c" or path.startswith("/c/"))
        if second:
            path = path[2:] or "/"
        elif third:
            path = path[2:] or "/"
        if path.startswith("/slow"):
            time.sleep(5)
            return self._send(200, json.dumps({"data": []}).encode(), "application/json")
        if path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/admin")
            self.end_headers()
            return
        if path.startswith("/moved"):  # 冷门 3xx（300）也必须当错误，不能当正文
            self.send_response(300)
            self.send_header("Location", "/elsewhere")
            self.end_headers()
            return
        m = re.match(r"^/api/search$", path.split("?", 1)[0])
        if m and third:
            # 第三个站：列表包在 items 里，字段名全不一样
            query = path.split("?", 1)[1] if "?" in path else ""
            keyword = _qs(query, "q")
            rows = THIRD_ROWS
            if keyword:
                rows = [r for r in rows if keyword in r["title"]]
            return self._send(200, json.dumps({"items": rows}).encode(), "application/json")
        m = re.match(r"^/api/skin/(\d+)$", path)
        if m and third:
            tid = int(m.group(1))
            row = next((r for r in THIRD_ROWS if r["id"] == tid), None)
            if row:
                host = self.headers.get("Host") or "127.0.0.1"
                payload = dict(row)
                payload["direct"] = f"http://{host}/c/tex/{tid}"
                return self._send(200, json.dumps(payload).encode(), "application/json")
            return self._send(404, b'{"error":"no"}', "application/json")
        m = re.match(r"^/tex/(\d+)$", path)
        if m and third:
            return self._send(200, TEXTURES["aa" * 32], "image/png")
        if path.startswith("/skinlib/list"):
            query = path.split("?", 1)[1] if "?" in path else ""
            if "filter=" in query:  # 实测：传未知参数会返回空数组
                return self._send(200, json.dumps({"data": [], "current_page": 1}).encode(), "application/json")
            keyword = _qs(query, "keyword")
            rows = SECOND_ROWS if second else LIST_ROWS
            if keyword:
                rows = [r for r in rows if keyword in r["name"]]
            if _qs(query, "order") == "random":
                rows = list(reversed(rows))
            page = int(_qs(query, "page") or 1)
            return self._send(200, json.dumps({"data": rows, "current_page": page}).encode(), "application/json")
        m = re.match(r"^/texture/(\d+)$", path)
        if m:
            tid = int(m.group(1))
            table = SECOND_DETAILS if second else DETAILS
            if tid in table:
                return self._send(200, json.dumps(table[tid]).encode(), "application/json")
            return self._send(404, b'{"error":"no"}', "application/json")
        m = re.match(r"^/textures/([0-9a-f]+)$", path)
        if m:  # 实测：不带扩展名才是对的
            data = TEXTURES.get(m.group(1))
            if data is None:
                return self._send(404, b"nope", "text/html")
            return self._send(200, data, "image/png")
        if re.match(r"^/textures/[0-9a-f]+\.png$", path):
            return self._send(404, b"<html>404</html>", "text/html")
        return self._send(404, b"<html>404</html>", "text/html")


def _qs(query: str, key: str) -> str:
    from urllib.parse import parse_qs

    return parse_qs(query).get(key, [""])[0]


def start_mock() -> tuple[str, threading.Thread, http.server.ThreadingHTTPServer]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}", t, srv


def make_noisy_png(size: int = 256) -> bytes:
    """随机噪声 PNG（压不动，单张几十 KB），用于把缓存撑到上限。"""
    from PIL import Image

    img = Image.frombytes("RGB", (size, size), os.urandom(size * size * 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ 3D 素材
#
# 与其“看一张图觉得像”，不如给每个面的正面涂一种独占颜色，再断言它出现在
# 屏幕上的哪个位置、多大一块。展开格写错、投影写反、镜像写歪都会立刻失配。

FACE_COLORS = {
    "head": (255, 10, 10),
    "body": (10, 255, 10),
    "right_arm": (10, 10, 255),
    "left_arm": (255, 255, 10),
    "right_leg": (255, 10, 255),
    "left_leg": (10, 255, 255),
}


def front_labeled_sheet(slim: bool = False):
    """只给 6 个部件的“正面”上色，其余全透明（覆盖层因此渲染不出任何东西）。"""
    from PIL import Image, ImageDraw

    from littleskin_skin.preview3d import face_rects, model_parts, part_size

    sheet = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dr = ImageDraw.Draw(sheet)
    for part in model_parts(slim):
        w, h, d = part_size(part, 1.0)
        rect = face_rects(part.tex[0], part.tex[1], w, h, d)["front"]
        dr.rectangle([rect[0], rect[1], rect[0] + rect[2] - 1, rect[1] + rect[3] - 1],
                     fill=FACE_COLORS[part.name] + (255,))
    return sheet


def shaded(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """复算 preview3d 的轴向着色结果（正视图里只有正面可见，factor 恒为 0.8）。"""
    return tuple(min(255, int(v * factor + 0.5)) for v in rgb)


def bbox_of(img, rgb: tuple[int, int, int]):
    """返回某种颜色的外接框 (x0, y0, x1, y1)，找不到返回 None。"""
    xs: list[int] = []
    ys: list[int] = []
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y][:3]
            if abs(r - rgb[0]) <= 2 and abs(g - rgb[1]) <= 2 and abs(b - rgb[2]) <= 2:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def box_size(box):
    return (box[2] - box[0] + 1, box[3] - box[1] + 1)


# ------------------------------------------------------------------- 用例


def base_conf(base_url: str) -> dict:
    import json as _json

    with open(os.path.join(PLUGIN, "_conf_schema.json"), encoding="utf-8") as f:
        schema = _json.load(f)
    conf = {k: v["default"] for k, v in schema.items()}
    conf.update(
        {
            "site": "blessing_skin",
            "base_url": base_url,
            "allowed_hosts": ["127.0.0.1", "localhost", "littleskin.cn"],
            "min_interval": 0.05,
            "max_queue_wait": 5,
            "user_cooldown": 0,
            "cache_dir": os.path.join(ROOT, ".test_cache"),
            "cache_max_mb": 1,
            "timeout": 5,
        },
    )
    return conf


def patch_policy_for_local_mock() -> None:
    """测试接缝：mock 站是 http://127.0.0.1:随机端口。

    生产代码坚持 https + 白名单 + 非内网，这里只在测试里把三处换掉：
    scheme（http→https）、非 443 端口、回环 IP 字面量，且仅对
    ``127.0.0.1:端口`` 这种形式放行。主机名白名单、 userinfo/凭据、
    跳转拒绝、体积与解码校验等逻辑全部不变。
    """
    from littleskin_skin import textures as tex_mod

    orig_init = tex_mod.SitePolicy.__init__
    orig_parse = tex_mod.SitePolicy._parse
    orig_static = tex_mod.SitePolicy.check_static
    orig_resolved = tex_mod.SitePolicy.check_resolved
    local = re.compile(r"^http://127\.0\.0\.1(:\d+)?")

    def init(self, base_url, allowed_hosts, timeout=10.0):
        orig_init(self, local.sub("https://127.0.0.1", base_url), allowed_hosts, timeout)

    def check_static(self, url):
        if not local.match(url):
            return orig_static(self, url)
        # 只有指向 mock 自身的 http://127.0.0.1[:端口] 才放松 scheme/端口/回环，
        # 白名单仍然照旧生效
        parsed = orig_parse(local.sub("https://127.0.0.1", url))
        host = (parsed.host or "").lower()
        if not host or not self.host_allowed(host):
            raise tex_mod.SkinError("内部错误：目标域名不在白名单内，已拒绝。")
        return parsed

    async def check_resolved(self, host):
        if host == "127.0.0.1":
            return None
        return await orig_resolved(self, host)

    tex_mod.SitePolicy.__init__ = init
    tex_mod.SitePolicy.check_static = check_static
    tex_mod.SitePolicy.check_resolved = check_resolved


def make_plugin(Event, conf):
    from astrbot.api.star import Context

    from littleskin_skin import main as main_mod

    async def _new():
        plugin = main_mod.Main(Context(), conf)
        await plugin.initialize()
        if plugin.init_error:
            raise AssertionError(f"初始化失败: {plugin.init_error}")
        return plugin, main_mod

    return _new()


# 整个离线用例共用一个事件循环：aiohttp 的 ClientSession 和
# asyncio.Semaphore 都绑定在创建它们的循环上，每条用例新建循环会让
# wait_for 报 "Timeout context manager should be used inside a task"。
LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)


def run(coro):
    return LOOP.run_until_complete(coro)


def chain_text(result) -> str:
    if result is None:
        return ""
    from astrbot.api.message_components import Plain as CompPlain

    return " | ".join(part.text for part in result.chain if isinstance(part, CompPlain))


def chain_image(result) -> str | None:
    if result is None:
        return None
    from astrbot.api.message_components import Image as CompImage

    for part in result.chain:
        if isinstance(part, CompImage):
            return part.path
    return None


def sent_parts(ev, cls) -> list:
    """从 event.sent 里挖出某种消息段（皮肤文件和渲染图现在都走 event.send）。"""
    out = []
    for chain in getattr(ev, "sent", []):
        for part in getattr(chain, "chain", []):
            if isinstance(part, cls):
                out.append(part)
    return out


def text_or_none(result) -> str:
    return chain_text(result) if result is not None else ""


def offline_tests() -> None:
    install_astrbot_stub()
    patch_policy_for_local_mock()
    from astrbot.api.event import AstrMessageEvent as Event
    from astrbot.api.message_components import Image as CompImage  # 别和函数里后导入的 PIL.Image 撞名

    base, _t, srv = start_mock()
    try:
        plugin, main_mod = run(make_plugin(Event, base_conf(base)))
        check("初始化成功", plugin.service is not None and plugin.init_error == "", plugin.init_error)

        # 1. 中文关键词
        r = run(plugin._search(Event("skin search 自由高达"), "自由高达"))
        check("中文关键词搜索", "自由高达" in chain_text(r), chain_text(r)[:60])

        # 2. 英文关键词 + 大小写回退
        r = run(plugin._search(Event("skin search gundam"), "gundam"))
        check("英文关键词搜索", "GUNDAM" in chain_text(r), chain_text(r)[:60])

        # 3. 无结果 -> 提示而非异常
        r = run(plugin._search(Event("skin search 不存在的东西zzz"), "不存在的东西zzz"))
        check("无结果返回提示", "没有找到" in chain_text(r), chain_text(r)[:40])

        # 4. /skin <关键词> 自定义过滤器命中/避让
        f = main_mod.SkinKeywordFilter()
        cfg = {}
        check(
            "裸关键词走搜索",
            f.filter(Event("skin 自由高达"), cfg) is True
            and f.filter(Event("skin get 1"), cfg) is False
            and f.filter(Event("skin view 1"), cfg) is False
            and f.filter(Event("skin rand"), cfg) is False
            and f.filter(Event("skin"), cfg) is False
            and f.filter(Event("other 1"), cfg) is False,
        )

        # 4b. 子命令注册表（含 3D 预览的别名）
        subs = dict(main_mod.Main.skin_group.sub_commands)
        check(
            "子命令注册齐全",
            {"get", "raw", "rand", "search", "view"} <= set(subs)
            and subs["view"] == frozenset({"3d", "preview"}),
            str(sorted(subs)),
        )

        # 5. 下载 + 渲染：渲染图（3D）与皮肤文件分开发
        from astrbot.api.message_components import File as CompFile

        ev = Event("skin get 2")
        r = run(plugin._get(ev, "2", raw=False))
        imgs = sent_parts(ev, CompImage)
        files = sent_parts(ev, CompFile)
        from PIL import Image

        if not imgs:
            print("DEBUG get: result=", r, "| sent=", [getattr(c, "chain", c) for c in ev.sent])
        with Image.open(imgs[0].path) as im:
            w, h = im.size
        check(
            "get 发渲染图 + 皮肤文件（各一条）",
            len(imgs) == 1 and len(files) == 1 and r is None,
            f"imgs={len(imgs)} files={len(files)} result={r}",
        )
        check(
            "渲染图只有 3D（不再揉进纹理表/展开）",
            w % 10 == 0 and h % 10 == 0 and h < w,
            f"{w}x{h}",
        )
        check(
            "皮肤文件走 AstrBot 文件服务",
            files[0].name.endswith(".png")
            and "/api/file/" in (files[0].url or "")
            and files[0].url.startswith("http://127.0.0.1:46185"),
            f"{files[0].name} -> {files[0].url}",
        )
        get_box = (w, h)

        # 5a. 打开 include_sheet / flatten 后，渲染图会明显变高（旧三段行为仍可用）
        conf_full = base_conf(base)
        conf_full["include_sheet"] = True
        conf_full["flatten"] = True
        conf_full["cache_dir"] = os.path.join(ROOT, ".test_cache_full")
        pfull, _ = run(make_plugin(Event, conf_full))
        ev_full = Event("skin get 2")
        run(pfull._get(ev_full, "2", raw=False))
        with Image.open(sent_parts(ev_full, CompImage)[0].path) as im:
            full_box = im.size
        check(
            "include_sheet+flatten 可回到三段拼图",
            full_box[1] > get_box[1] * 2,
            f"{full_box} vs {get_box}",
        )
        run(pfull.terminate())

        # 5b. /skin view 只发 3D 预览图，不发皮肤文件
        ev = Event("skin view 2")
        r = run(plugin._get(ev, "2", raw=False, view=True))
        vimgs = sent_parts(ev, CompImage)
        check(
            "view 只发 3D 预览（无文件）",
            len(vimgs) == 1 and not sent_parts(ev, CompFile),
            f"imgs={len(vimgs)}",
        )

        # 5c. 关掉 preview_3d 后同一 tid 应换一份缓存（而不是复用带 3D 的图）
        conf_flat = base_conf(base)
        conf_flat["preview_3d"] = False
        conf_flat["cache_dir"] = os.path.join(ROOT, ".test_cache_flat")
        pflat, _ = run(make_plugin(Event, conf_flat))
        ev_flat = Event("skin get 2")
        run(pflat._get(ev_flat, "2", raw=False))
        with Image.open(sent_parts(ev_flat, CompImage)[0].path) as im:
            flat_box = im.size
        check("preview_3d=false 时不含 3D 条", flat_box != get_box, f"{flat_box} vs {get_box}")
        run(pflat.terminate())

        # 5d. 同一张纹理，alex 与 steve 必须出不同的图（手臂宽 3 vs 4）
        from littleskin_skin.sources import SkinDetail

        ev1 = Event("skin get 1")
        ev2 = Event("skin get 2")
        run(plugin._get(ev1, "1", raw=False))
        run(plugin._get(ev2, "2", raw=False))
        with Image.open(sent_parts(ev1, CompImage)[0].path) as im:
            steve_box = im.size
        with Image.open(sent_parts(ev2, CompImage)[0].path) as im:
            alex_box = im.size
        check(
            "steve/alex 模型分别出图",
            steve_box != alex_box
            and main_mod._is_slim({}, SkinDetail(kind="alex")) is True
            and main_mod._is_slim({"skin_model": "steve"}, SkinDetail(kind="alex")) is False,
            f"{steve_box} vs {alex_box}",
        )

        # 6. raw 原图：只发文件，发的是真正的 64×32 PNG 字节
        from astrbot.core import file_token_service

        ev = Event("skin raw 4")
        r = run(plugin._get(ev, "4", raw=True))
        raw_files = sent_parts(ev, CompFile)
        raw_path = file_token_service.FILE_TOKENS[-1] if file_token_service.FILE_TOKENS else ""
        with Image.open(raw_path) as im:
            size = im.size
        check(
            "raw 只发 64x32 原文件",
            len(raw_files) == 1 and not sent_parts(ev, CompImage) and size == (64, 32),
            f"files={len(raw_files)} size={size}",
        )

        # 6b. skin_file_mode=image / none
        for mode, want_img, want_file in (("image", 2, 0), ("none", 1, 0)):
            conf_mode = base_conf(base)
            conf_mode["skin_file_mode"] = mode
            pmode, _ = run(make_plugin(Event, conf_mode))
            ev_mode = Event("skin get 4")
            run(pmode._get(ev_mode, "4", raw=False))
            got_img = len(sent_parts(ev_mode, CompImage))
            got_file = len(sent_parts(ev_mode, CompFile))
            check(
                f"skin_file_mode={mode}",
                got_img == want_img and got_file == want_file,
                f"img={got_img} file={got_file}",
            )
            run(pmode.terminate())

        # 6c. aiocqhttp（NapCat）优先用 upload_group_file / upload_private_file
        class FakeBot:
            def __init__(self) -> None:
                self.calls: list[tuple] = []

            async def call_action(self, action, **kwargs):
                self.calls.append((action, kwargs))
                return {"ok": True}

        for gid, action in (("123456", "upload_group_file"), ("", "upload_private_file")):
            ev_q = Event("skin get 4")
            ev_q.bot = FakeBot()
            ev_q.get_group_id = lambda g=gid: g
            run(plugin._get(ev_q, "4", raw=False))
            calls = ev_q.bot.calls
            check(
                f"{action}：先试 OneBot 文件上传",
                len(calls) == 1
                and calls[0][0] == action
                and "/api/file/" in calls[0][1].get("file", "")
                and calls[0][1].get("name", "").endswith(".png"),
                str(calls)[:120],
            )

        # 6d. OneBot 上传失败时退回 File 段（不让消息直接丢）
        class BrokenBot:
            async def call_action(self, action, **kwargs):  # noqa: ARG002
                raise RuntimeError("napcat down")

        ev_fb = Event("skin get 4")
        ev_fb.bot = BrokenBot()
        ev_fb.get_group_id = lambda: "123456"
        run(plugin._get(ev_fb, "4", raw=False))
        check(
            "OneBot 上传失败时退回 File 段",
            len(sent_parts(ev_fb, CompFile)) == 1,
            f"files={len(sent_parts(ev_fb, CompFile))}",
        )

        # 7. HD 皮肤
        ev = Event("skin get 3")
        run(plugin._get(ev, "3", raw=False))
        check("HD 皮肤可下载", len(sent_parts(ev, CompImage)) == 1, "")
        conf_no_hd = base_conf(base)
        conf_no_hd["allow_hd"] = False
        plugin_no_hd, _ = run(make_plugin(Event, conf_no_hd))
        r = run(plugin_no_hd._get(Event("skin get 3"), "3", raw=False))
        check("allow_hd=false 时拒绝 HD", "HD" in chain_text(r), chain_text(r)[:40])
        run(plugin_no_hd.terminate())

        # 8. 非 PNG / 非标准尺寸 -> 丢弃
        r = run(plugin._get(Event("skin get 5"), "5", raw=False))
        check("非 PNG 丢弃", "PNG" in chain_text(r) or "解码" in chain_text(r), chain_text(r)[:40])
        r = run(plugin._get(Event("skin get 6"), "6", raw=False))
        check("非标准尺寸丢弃", "标准" in chain_text(r) or "尺寸" in chain_text(r), chain_text(r)[:40])

        # 9. 缺字段降级
        r = run(plugin._get(Event("skin get 7"), "7", raw=False))
        check("缺字段降级为未找到", "未找到" in chain_text(r), chain_text(r)[:40])

        # 10. 路径穿越 / 非法 tid
        bad_inputs = ["../etc/passwd", "..%2f", "/etc/passwd", "1;rm -rf", "9" * 30, "0x1f", "", " "]
        ok = True
        for bad in bad_inputs:
            ev = Event("skin get x")
            run(plugin._get(ev, bad, raw=False))
            if sent_parts(ev, CompImage) or sent_parts(ev, CompFile):
                ok = False
                print("   leaked:", bad)
        check("非法 tid 不产生文件/越权", ok)

        from littleskin_skin.textures import TextureCache

        cache = TextureCache(os.path.join(ROOT, ".test_cache"), 1024)
        raised = 0
        for bad in ["../../evil", "a/../../b", "..", "x" * 200, "aa bb", "aa/bb"]:
            try:
                cache.path_for(bad)
            except Exception:  # noqa: BLE001
                raised += 1
        check("缓存键名白名单生效", raised == 6, f"{raised}/6")

        # 11. 404 -> 友好提示
        r = run(plugin._get(Event("skin get 999"), "999", raw=False))
        check("不存在的 tid 有提示", "没找到" in chain_text(r), chain_text(r)[:40])

        # 12. 缓存命中不再发请求
        before = len(REQUEST_LOG)
        run(plugin._get(Event("skin get 2"), "2", raw=False))
        run(plugin._get(Event("skin get 2"), "2", raw=False))
        after = len(REQUEST_LOG)
        check("同 hash 命中缓存不再请求", after - before <= 2, f"{after - before} req")

        # 13. 限流：两次请求间隔 >= min_interval
        conf_gate = base_conf(base)
        conf_gate["min_interval"] = 0.3
        p2, _ = run(make_plugin(Event, conf_gate))
        t0 = time.monotonic()
        run(p2._search(Event("skin search 自由高达"), "自由高达"))
        run(p2._search(Event("skin search 自由高达"), "自由高达"))
        dt = time.monotonic() - t0
        check("节流间隔生效", dt >= 0.3, f"{dt:.2f}s")
        run(p2.terminate())

        # 14. 排队超时 -> 友好提示（不抛异常）
        conf_busy = base_conf(base)
        conf_busy["max_queue_wait"] = 0.1
        p4, _ = run(make_plugin(Event, conf_busy))

        async def busy():
            async with p4.client.gate.slot():
                return await p4._search(Event("skin search 自由高达"), "自由高达")

        r = run(busy())
        check("闸口被占时返回提示", "稍后再试" in chain_text(r) or "手速" in chain_text(r), chain_text(r)[:40])
        run(p4.terminate())

        # 15. 站点超时（慢响应）-> 在 timeout 内返回错误，不卡住事件循环
        conf_timeout = base_conf(base)
        conf_timeout["timeout"] = 1
        conf_timeout["search_path"] = "/slow"
        p5, _ = run(make_plugin(Event, conf_timeout))
        t0 = time.monotonic()
        r = run(p5._search(Event("skin search x"), "x"))
        dt = time.monotonic() - t0
        check(
            "站点超时在 10s 内返回错误",
            dt < 10 and "超时" in chain_text(r),
            f"{dt:.1f}s {chain_text(r)[:40]}",
        )
        run(p5.terminate())

        # 16. 断网 / DNS 解析失败
        conf_dead = base_conf("https://nonexistent-zone-xyz.invalid")
        conf_dead["allowed_hosts"] = ["nonexistent-zone-xyz.invalid"]
        p6, _ = run(make_plugin(Event, conf_dead))
        t0 = time.monotonic()
        r = run(p6._search(Event("skin search x"), "x"))
        dt = time.monotonic() - t0
        check(
            "断网返回友好提示且不抛异常",
            chain_image(r) is None and len(chain_text(r)) > 0 and dt < 10,
            f"{dt:.1f}s {chain_text(r)[:40]}",
        )
        run(p6.terminate())

        # 17. SSRF：白名单外域名 / 跳转 / 内网 IP
        p7, _ = run(make_plugin(Event, base_conf(base)))
        r = run(p7._search(Event("skin search x"), "http://169.254.169.254/"))
        check("关键词不会变成请求地址", chain_image(r) is None)

        async def ssrf():
            from littleskin_skin.textures import SitePolicy, is_blocked_ip

            errs = []
            # 默认配置：只允许 littleskin.cn
            strict = SitePolicy("https://littleskin.cn", None)
            for url in (
                "https://evil.example.com/x",
                "https://evil-littleskin.cn/x",
                "http://littleskin.cn/textures/" + "aa" * 32,
                "https://littleskin.cn:8443/x",
                "https://user:pw@littleskin.cn/x",
                "https://169.254.169.254/latest/meta-data",
                "https://100.64.1.2/x",
                "https://[::1]/x",
            ):
                try:
                    strict.check_static(url)
                    errs.append(url)
                except Exception:  # noqa: BLE001
                    pass
            # 有人把内网 IP 直接填进白名单，也要被 IP 字面量这一层挡住
            loose = SitePolicy(
                "https://littleskin.cn",
                ["littleskin.cn", "127.0.0.1", "10.1.2.3", "172.16.5.4", "192.168.1.1", "169.254.1.1"],
            )
            for url in (
                "https://127.0.0.1/x",
                "https://10.1.2.3/x",
                "https://172.16.5.4/x",
                "https://192.168.1.1/x",
                "https://169.254.1.1/x",
            ):
                try:
                    loose.check_static(url)
                    errs.append(url)
                except Exception:  # noqa: BLE001
                    pass
            for internal in ("127.0.0.1", "10.1.2.3", "172.16.5.4", "192.168.1.1", "169.254.1.1", "100.64.0.1", "::1", "::ffff:127.0.0.1", "not-an-ip"):
                if not is_blocked_ip(internal):
                    errs.append(internal)
            return errs

        ssrf_errs = run(ssrf())
        check("URL/IP 拦截生效", ssrf_errs == [], str(ssrf_errs[:3]))

        async def redirect():
            return await p7.client.get_bytes(f"{base}/redirect")

        try:
            run(redirect())
            check("拒绝跟随跳转", False)
        except Exception as exc:  # noqa: BLE001
            check("拒绝跟随跳转", "跳转" in str(exc) or "302" in str(exc), str(exc)[:40])

        async def moved():
            return await p7.client.get_bytes(f"{base}/moved")

        try:
            run(moved())
            check("冷门 3xx 也按跳转拒绝", False)
        except Exception as exc:  # noqa: BLE001
            check("冷门 3xx 也按跳转拒绝", "跳转" in str(exc), str(exc)[:40])
        run(p7.terminate())

        # 18. 文本清洗
        from littleskin_skin.sources import clean_text

        dirty = "ab\x1b[0m\x00cd​‮" + "x" * 100
        c = clean_text(dirty)
        check(
            "第三方文本清洗",
            "\x1b" not in c and "\x00" not in c and "​" not in c and "‮" not in c and len(c) <= 40,
            repr(c[:20]),
        )

        # 19. 缓存 LRU
        conf_lru = base_conf(base)
        conf_lru["cache_max_mb"] = 1
        p8, _ = run(make_plugin(Event, conf_lru))

        async def fill():
            blobs = [make_noisy_png() for _ in range(40)]
            for i, blob in enumerate(blobs):
                await p8.cache.put(f"{i:064x}", blob)
                if i % 10 == 9:
                    await asyncio.sleep(0.02)  # 让 mtime 有先后，LRU 才有得挑
            return sorted(os.listdir(p8.cache.root))

        names = run(fill())
        total = sum(
            os.path.getsize(os.path.join(p8.cache.root, n)) for n in names if n.endswith(".png")
        )
        check(
            "缓存超上限后自动清理",
            total <= 1024 * 1024 and 0 < len(names) < 40,
            f"{len(names)} files, {total} bytes",
        )
        run(p8.terminate())

        # 19b. 输出像素预算：2G 内存的机器不能被 zoom×HD 撑爆
        from littleskin_skin.render import render_preview

        big = Image.frombytes("RGBA", (256, 256), os.urandom(256 * 256 * 4))
        out = render_preview(big, zoom=32, flatten=True, max_pixels=4_000_000)
        check("输出图受像素预算约束", out.width * out.height <= 4_000_000, f"{out.width}x{out.height}")

        # 19c. 3D 预览：正视图里六个面的位置/尺寸必须严格对得上展开规律
        from littleskin_skin.preview3d import face_rects, model_parts, part_size, render_views

        fig = render_views(front_labeled_sheet(), views=(0.0,), pitch=0.0, scale=2.0)
        want = {
            "head": (16, 16),
            "body": (16, 24),
            "right_arm": (8, 24),
            "left_arm": (8, 24),
            "right_leg": (8, 24),
            "left_leg": (8, 24),
        }
        boxes = {}
        bad = []
        for name, rgb in FACE_COLORS.items():
            box = bbox_of(fig, shaded(rgb, 0.8))
            if box is None:
                bad.append(f"{name}:没画出来")
                continue
            boxes[name] = box
            size = box_size(box)
            if size != want[name]:
                bad.append(f"{name}:{size}!={want[name]}")
        # 头在上、身居中、腿在最下，两只手臂分列身体两侧
        if len(boxes) == 6:
            if not boxes["head"][3] < boxes["body"][1]:
                bad.append("头不在身体上方")
            if not boxes["body"][3] <= boxes["right_leg"][1]:
                bad.append("腿不在身体下方")
            if not boxes["right_arm"][2] < boxes["left_arm"][0]:
                bad.append("左右臂没分列两侧")
            if not boxes["right_leg"][0] < boxes["left_leg"][0]:
                bad.append("左右腿位置颠倒")
        check("3D 正视图六面位置", not bad, ";".join(bad[:3]))

        # 19d. 覆盖层比本体每边外扩半格（游戏里 hat 就是 0.5）
        from PIL import ImageDraw

        from littleskin_skin.preview3d import FACE_LIGHT

        hat = front_labeled_sheet()
        hr = face_rects(*model_parts()[0].overlay_tex, *part_size(model_parts()[0], 1.0))["front"]
        ImageDraw.Draw(hat).rectangle([hr[0], hr[1], hr[0] + hr[2] - 1, hr[1] + hr[3] - 1], fill=(200, 200, 200, 255))
        hat_fig = render_views(hat, views=(0.0,), pitch=0.0, scale=2.0)
        grey = bbox_of(hat_fig, shaded((200, 200, 200), FACE_LIGHT["front"]))
        check(
            "3D 覆盖层外扩半格",
            grey is not None and box_size(grey) == (18, 18),
            str(box_size(grey)) if grey else "没画出来",
        )

        # 19e. 64x32 旧表没有左半边，左臂必须是右臂展开的水平镜像：
        # 只涂右臂正面展开的“左上角 2x6”，镜像正确时两臂的色块都该贴着外缘、
        # 都在上半部；少一次翻转就会有一侧跑到内侧/下方。
        legacy = Image.new("RGBA", (64, 32), (0, 0, 0, 0))
        af = face_rects(40, 16, 4, 12, 4)["front"]
        ImageDraw.Draw(legacy).rectangle([af[0], af[1], af[0] + 1, af[1] + 5], fill=(255, 10, 10, 255))
        lg = render_views(legacy, views=(0.0,), pitch=0.0, scale=2.0)
        red = bbox_of(lg, shaded((255, 10, 10), FACE_LIGHT["front"]))
        mid_x = (lg.width - 1) / 2
        mid_y = (lg.height - 1) / 2
        check(
            "旧表左臂镜像不翻向",
            red is not None
            and red[0] <= 2
            and red[2] >= lg.width - 3
            and red[1] <= 2
            and red[3] < mid_y,
            f"{red} of {lg.size}",
        )

        # 19f. 3D 条也要吃像素预算
        out3 = render_preview(front_labeled_sheet(), zoom=10, flatten=True, three_d=True, max_pixels=60_000)
        check("3D 拼图受像素预算约束", out3.width * out3.height <= 60_000, f"{out3.width}x{out3.height}")

        # 19g. 视角配置：非法项丢弃、超过 4 个截断、全非法退回默认
        v = main_mod._views_3d
        check(
            "3D 视角配置解析",
            v({"views_3d": "0, 30, 180"}) == (0.0, 30.0, 180.0)
            and len(v({"views_3d": "0,10,20,30,40,50"})) == 4
            and v({"views_3d": "abc"}) == main_mod.DEFAULT_VIEWS
            and v({"views_3d": ["0", "45"]}) == (0.0, 45.0),
        )

        # 19h. Image.MAX_IMAGE_PIXELS 是进程级全局量，用完必须复原
        from littleskin_skin.textures import validate_texture

        saved_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = 1000
        validate_texture(make_png(64, 64, 3), True, 4_000_000)
        check("MAX_IMAGE_PIXELS 用完复原", Image.MAX_IMAGE_PIXELS == 1000, str(Image.MAX_IMAGE_PIXELS))
        Image.MAX_IMAGE_PIXELS = saved_limit

        # 19d. 崩溃遗留的半截文件要能被回收；正要发送的最近文件不许被 LRU 删掉
        root = p8.cache.root
        stale = os.path.join(root, f"{'0' * 64}.png.part.{os.getpid()}")
        with open(stale, "wb") as fh:
            fh.write(b"half")
        hour_ago = time.time() - 7200
        os.utime(stale, (hour_ago, hour_ago))
        newest_key = f"{9999:064x}"
        run(p8.cache.put(newest_key, make_noisy_png()))
        check("陈旧 .part 被回收", not os.path.exists(stale))
        check(
            "LRU 保留最近的缓存文件",
            os.path.exists(os.path.join(root, f"{newest_key}.png")),
            str(sorted(os.listdir(root))[:2]),
        )

        # 22. LLM 函数工具：搜索结果只给名字（不带 tid / 赞 / 上传者）
        plugin.conf["user_cooldown"] = 0  # 桩里所有事件共用一个 umo，冷却会挡住连续调用
        text = run(plugin.tool_search(Event("找找自由高达的皮肤"), "自由高达"))
        check(
            "工具：搜索只返回名字",
            "自由高达" in text
            and "tid" not in text
            and "赞" not in text
            and "alice" not in text,
            text.replace("\n", " / ")[:80],
        )

        # 22b. 工具发图：图由 event.send 直发（tool 返回值只进模型上下文）
        ev = Event("发一下自由高达")
        msg = run(plugin.tool_skin_image(ev, "自由高达"))
        sent = [part for chain in ev.sent for part in chain.chain if isinstance(part, CompImage)]
        check(
            "工具：按名字发图",
            len(sent) == 1 and os.path.exists(sent[0].path) and "已发" in msg,
            f"{msg} | {[p.path for p in sent]}",
        )

        ev = Event("用 tid 发一张")
        msg = run(plugin.tool_skin_image(ev, "1"))
        sent = [part for chain in ev.sent for part in chain.chain if isinstance(part, CompImage)]
        check("工具：按 tid 发图", len(sent) == 1 and "已发" in msg, msg)

        ev = Event("搜不到")
        msg = run(plugin.tool_skin_image(ev, "不存在zzz"))
        check("工具：搜不到不发图", not ev.sent and "没搜到" in msg, msg)

        # 22c. 工具冷却与指令冷却分开计：指令刚发过，不该把工具也挡住
        plugin.conf["user_cooldown"] = 3
        run(plugin._search(Event("skin search 自由高达"), "自由高达"))
        ev = Event("紧接着让模型发一张")
        msg = run(plugin.tool_skin_image(ev, "自由高达"))
        check("工具冷却与指令分开计", bool(ev.sent) and "已发" in msg, msg)
        plugin.conf["user_cooldown"] = 0

        # 23. 多站点：sites 列表 + -s 选站 + /skin sites
        conf_multi = base_conf(base)
        conf_multi["sites"] = [
            f"ls|blessing_skin|{base}|127.0.0.1",
            f"b|blessing_skin|{base}/b|127.0.0.1",
        ]
        conf_multi["cache_dir"] = os.path.join(ROOT, ".test_cache_multi")
        pmulti, _ = run(make_plugin(Event, conf_multi))
        check(
            "多站点：解析出两个站",
            [rt.alias for rt in pmulti.sites] == ["ls", "b"],
            str([rt.alias for rt in pmulti.sites]),
        )
        check("多站点：默认站是第一个", pmulti.default.alias == "ls", pmulti.default.alias)

        r = run(pmulti.cmd_search(Event("skin search 自由高达"), "自由高达"))
        check(
            "多站点：默认站搜索带前缀",
            "[ls]" in chain_text(r) and "自由高达" in chain_text(r),
            chain_text(r)[:60],
        )
        r = run(pmulti.cmd_search(Event("skin search 皮肤 -s b"), "皮肤 -s b"))
        check(
            "多站点：-s 选站",
            "[b]" in chain_text(r) and "B站皮肤" in chain_text(r),
            chain_text(r)[:60],
        )
        r = run(pmulti.cmd_search(Event("skin search x -s nope"), "x -s nope"))
        check("多站点：未知别名有提示", "没有叫" in chain_text(r), chain_text(r)[:60])

        r = run(pmulti.cmd_sites(Event("skin sites")))
        check(
            "多站点：/skin sites 列出站点",
            "ls" in chain_text(r) and "b" in chain_text(r) and "默认" in chain_text(r),
            chain_text(r)[:80].replace("\n", " / "),
        )

        ev = Event("skin get 101 -s b")
        run(pmulti.cmd_get(ev, "101"))
        check(
            "多站点：按站取图 + 文件",
            len(sent_parts(ev, CompImage)) == 1 and len(sent_parts(ev, CompFile)) == 1,
            f"img={len(sent_parts(ev, CompImage))} file={len(sent_parts(ev, CompFile))}",
        )

        # 23b. 每个站自己的白名单：b 站白名单写成别的域名 → 只有 b 站被拦
        conf_hosts = base_conf(base)
        conf_hosts["sites"] = [
            f"ls|blessing_skin|{base}|127.0.0.1",
            f"b|blessing_skin|{base}/b|example.com",
        ]
        conf_hosts["cache_dir"] = os.path.join(ROOT, ".test_cache_multi2")
        phosts, _ = run(make_plugin(Event, conf_hosts))
        r_b = run(phosts.cmd_search(Event("skin search 皮肤 -s b"), "皮肤 -s b"))
        r_ls = run(phosts.cmd_search(Event("skin search 自由高达 -s ls"), "自由高达 -s ls"))
        check(
            "多站点：白名单逐站生效",
            ("白名单" in chain_text(r_b) or "拒绝" in chain_text(r_b))
            and "自由高达" in chain_text(r_ls),
            f"b={chain_text(r_b)[:30]} | ls={chain_text(r_ls)[:30]}",
        )
        run(phosts.terminate())

        conf_def = base_conf(base)
        conf_def["sites"] = list(conf_multi["sites"])
        conf_def["default_site"] = "b"
        conf_def["cache_dir"] = os.path.join(ROOT, ".test_cache_multi3")
        pdef, _ = run(make_plugin(Event, conf_def))
        check("多站点：default_site 生效", pdef.default.alias == "b", pdef.default.alias)
        run(pdef.terminate())
        run(pmulti.terminate())

        # 24. 非法 sites 行被跳过，并退回单站点配置（不能让插件整个起不来）
        conf_broken = base_conf(base)
        conf_broken["sites"] = ["坏行", "x|blessing_skin|http://insecure.example.com|insecure.example.com"]
        conf_broken["cache_dir"] = os.path.join(ROOT, ".test_cache_multi4")
        pbroken, _ = run(make_plugin(Event, conf_broken))
        check(
            "非法 sites 退回单站点",
            len(pbroken.sites) == 1 and pbroken.default.base_url.rstrip("/") == base.rstrip("/"),
            str([rt.alias for rt in pbroken.sites]),
        )
        run(pbroken.terminate())

        # 25. 字段映射：字段名/路由/列表包装全不一样的站，只靠配置就能接
        conf_map = base_conf(base)
        conf_map["sites"] = [
            "c|blessing_skin|" + base + "/c|127.0.0.1|"
            + "search_path=/api/search|search_keyword_param=q|"
            + "detail_path=/api/skin/{tid}|texture_path=/tex/{tid}|"
            + "list_key=items|field_tid=id|field_name=title|field_type=model|"
            + "field_likes=likes|field_uploader=author",
        ]
        conf_map["cache_dir"] = os.path.join(ROOT, ".test_cache_map")
        pmap, _ = run(make_plugin(Event, conf_map))
        r = run(pmap.cmd_search(Event("skin search C站"), "C站"))
        text = chain_text(r)
        check(
            "字段映射：搜索解析出名字/模型/作者",
            "C站皮肤" in text and "alex" in text and "carl" in text,
            text[:90].replace("\n", " / "),
        )
        ev_map = Event("skin get 201")
        run(pmap._get(ev_map, "201", raw=False))
        check(
            "字段映射：详情/纹理/{tid} 模板可用",
            len(sent_parts(ev_map, CompImage)) == 1,
            f"img={len(sent_parts(ev_map, CompImage))}",
        )
        run(pmap.terminate())

        # 25b. 站点直接返回完整纹理 URL（CDN）时，field_texture_url 优先
        conf_direct = base_conf(base)
        conf_direct["sites"] = [
            "c|blessing_skin|" + base + "/c|127.0.0.1|"
            + "search_path=/api/search|search_keyword_param=q|"
            + "detail_path=/api/skin/{tid}|"
            + "list_key=items|field_tid=id|field_name=title|field_type=model|"
            + "field_uploader=author|field_texture_url=direct",
        ]
        conf_direct["cache_dir"] = os.path.join(ROOT, ".test_cache_map2")
        pdirect, _ = run(make_plugin(Event, conf_direct))
        ev_dir = Event("skin get 201")
        run(pdirect._get(ev_dir, "201", raw=False))
        check(
            "field_texture_url：直接用站点给的完整 URL",
            len(sent_parts(ev_dir, CompImage)) == 1,
            f"img={len(sent_parts(ev_dir, CompImage))}",
        )
        run(pdirect.terminate())

        # 20. 不写入插件目录
        check("缓存目录不在插件目录内", not os.path.abspath(base_conf(base)["cache_dir"]).startswith(PLUGIN))

        # 21. 半截文件：.part 不残留
        leftovers = [n for n in os.listdir(base_conf(base)["cache_dir"]) if ".part" in n]
        check("无 .part 残留", not leftovers, str(leftovers[:3]))

        run(plugin.terminate())
    finally:
        srv.shutdown()


def live_tests() -> None:
    install_astrbot_stub()
    from astrbot.api.event import AstrMessageEvent as Event
    from astrbot.api.message_components import Image as CompImage
    from astrbot.api.star import Context

    from littleskin_skin import main as main_mod

    with open(os.path.join(PLUGIN, "_conf_schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    conf = {k: v["default"] for k, v in schema.items()}
    conf.update(
        {
            "min_interval": 1.0,
            "user_cooldown": 0,
            "cache_dir": os.path.join(ROOT, ".live_cache"),
            "max_results": 5,
        },
    )

    async def go():
        plugin = main_mod.Main(Context(), conf)
        await plugin.initialize()
        if plugin.init_error:
            check("live 初始化", False, plugin.init_error)
            return
        r = await plugin._search(Event("skin search 自由高达"), "自由高达")
        check("live 中文搜索", "高" in chain_text(r), chain_text(r).replace("\n", " ")[:90])
        r = await plugin._search(Event("skin search GUNDAM"), "GUNDAM")
        check("live 英文搜索", "GUNDAM" in chain_text(r).upper(), chain_text(r).replace("\n", " ")[:90])
        # _rand 现在把图/文件用 event.send 发出去，不靠返回值，所以要查 event.sent
        ev = Event("skin rand")
        r = await plugin._rand(ev)
        text = chain_text(r)
        got = len(sent_parts(ev, CompImage))
        check(
            "live 随机皮肤",
            got == 1 or "没有返回可用的公开皮肤" in text,
            f"img={got} {text[:60]}",
        )
        r = await plugin._search(Event("skin search zzz不存在的关键词zzz"), "zzz不存在的关键词zzz")
        check("live 无结果提示", "没有找到" in chain_text(r), chain_text(r)[:40])
        await plugin.terminate()

    run(go())


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        offline_tests()
        if "--live" in sys.argv:
            print("\n--- 真实站点冒烟测试 ---")
            live_tests()
    finally:
        LOOP.close()
    print(f"\n通过 {len(PASSED)} / 失败 {len(FAILED)}")
    if FAILED:
        print("失败项：")
        for name in FAILED:
            print("  -", name)
        sys.exit(1)
