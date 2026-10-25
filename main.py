"""命令注册与编排：只做参数解析、调用、组装回复，不含业务细节。

业务在 sources.py（站点，含多站点清单）、textures.py（下载/校验/缓存/限流）、render.py（可视化）。

出口有两条，共用同一套业务层：

- 指令：`/skin search|get|raw|view|rand|sites`，以及裸关键词的 CustomFilter；
- LLM 函数工具：`littleskin_search`（返回已清洗的皮肤名清单）、
  `littleskin_skin_image`（渲染后直接 `event.send` 发图）。

多站点：`sites` 里一行一个站，命令末尾加 `-s 别名` 指定；不带就是默认站。
皮肤文件（64×64 原图）默认以**文件附件**单独发送，与渲染图分开（见 `_send_skin_file`）。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Image, Plain
from astrbot.core.star.filter.command import GreedyStr

from .render import DEFAULT_VIEWS, render_preview
from .sources import (
    NOT_FOUND,
    SiteSpec,
    SkinDetail,
    SkinItem,
    SkinSource,
    build_source,
    clean_text,
    parse_sites,
)
from .textures import (
    NotFoundError,
    SiteClient,
    SitePolicy,
    HttpGate,
    SkinError,
    TextureCache,
    TextureService,
    is_valid_hash,
    is_valid_tid,
)

COMMAND_GROUP = "skin"
RESERVED_SUBCOMMANDS = {
    "get",
    "raw",
    "rand",
    "search",
    "help",
    "view",
    "3d",
    "preview",
    "sites",
}
MAX_VIEWS = 4
SITE_FLAG_RE = re.compile(r"(?:^|\s)(?:-s|--site)\s+(\S+)", re.IGNORECASE)
_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass
class SiteRuntime:
    """一个已就绪的站点：适配器 + 带白名单的 HTTP 客户端 + 纹理服务。"""

    alias: str
    source: SkinSource
    client: SiteClient
    service: TextureService

    @property
    def base_url(self) -> str:
        return self.source.base_url


def _split_site_flag(text: str) -> tuple[str, str]:
    """把 `-s 别名` 从文本里取出来，返回 (剩下的文本, 别名)。"""
    match = SITE_FLAG_RE.search(text or "")
    if not match:
        return (text or "").strip(), ""
    cleaned = ((text or "")[: match.start()] + " " + (text or "")[match.end() :]).strip()
    return re.sub(r"\s+", " ", cleaned), match.group(1).strip().lower()


def _safe_filename(name: str, tid: str) -> str:
    """皮肤文件名：清掉路径分隔符与控件符，保留中文（手机端看得懂）。"""
    base = f"{clean_text(name, 40)}_{tid}" if name and name != NOT_FOUND else f"skin_{tid}"
    base = _FILENAME_BAD.sub("_", base).strip(" ._") or f"skin_{tid}"
    return base[:80] + ".png"


def _default_cache_root() -> str:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        return os.path.join(get_astrbot_plugin_data_path(), "littleskin_skin", "cache")
    except Exception:  # noqa: BLE001  脱离 AstrBot 运行时（自测）时的兜底
        return os.path.join(os.getcwd(), "data", "plugin_data", "littleskin_skin", "cache")


def _int(conf: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(float(conf.get(key, default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _float(conf: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(conf.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


class SkinKeywordFilter(filter.CustomFilter):
    """让 `/skin <关键词>` 走搜索，同时不抢 `/skin get|raw|rand|view|search` 的活。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:  # noqa: ARG002
        text = re.sub(r"\s+", " ", (event.get_message_str() or "").strip())
        if not text.lower().startswith(COMMAND_GROUP + " "):
            return False
        rest = text[len(COMMAND_GROUP) :].strip()
        if not rest:
            return False
        return rest.split(" ", 1)[0].lower() not in RESERVED_SUBCOMMANDS


class Main(star.Star):
    """LittleSkin / Blessing Skin 皮肤查询。"""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        try:
            super().__init__(context, config)
        except TypeError:  # 兼容只接受 context 的旧版 Star
            super().__init__(context)
        self.conf: dict = dict(config or {})
        self.logger = getattr(self, "logger", None) or logging.getLogger("littleskin_skin")
        self.sites: list[SiteRuntime] = []
        self.default: SiteRuntime | None = None
        self.cache: TextureCache | None = None
        self.init_error: str = ""
        self._cooldown: dict[str, float] = {}

    # 单站点时代的属性，保留给外部（自测/排查）用；内部一律走 self.sites。
    @property
    def service(self) -> TextureService | None:
        return self.default.service if self.default else None

    @property
    def source(self) -> SkinSource | None:
        return self.default.source if self.default else None

    @property
    def client(self) -> SiteClient | None:
        return self.default.client if self.default else None

    # ------------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        conf = self.conf
        try:
            specs, problems = parse_sites(conf, self.logger)
            for problem in problems:
                self.logger.warning("站点配置：%s", problem)
            if not specs:
                raise ValueError("没有任何可用的皮肤站配置")

            cache_dir = str(conf.get("cache_dir") or "").strip() or _default_cache_root()
            self.cache = TextureCache(
                root=cache_dir,
                max_bytes=_int(conf, "cache_max_mb", 200, 1, 8192) * 1024 * 1024,
            )
            await asyncio.to_thread(os.makedirs, cache_dir, exist_ok=True)

            timeout = _float(conf, "timeout", 10, 1, 10)
            min_interval = _float(conf, "min_interval", 0.8, 0, 30)
            max_wait = _float(conf, "max_queue_wait", 6.0, 0, 60)
            user_agent = str(conf.get("user_agent") or "Mozilla/5.0")
            max_bytes = _int(conf, "max_texture_bytes", 2 * 1024 * 1024, 4096, 64 * 1024 * 1024)
            send_referer = bool(conf.get("send_referer", True))

            sites: list[SiteRuntime] = []
            for spec in specs:
                try:
                    source = build_source(spec, self.logger)
                except Exception as exc:  # noqa: BLE001  单个站坏了不影响其它站
                    self.logger.warning("站点 %s 不可用：%s", spec.alias, exc)
                    continue
                client = SiteClient(
                    policy=SitePolicy(
                        base_url=spec.config.base_url,
                        allowed_hosts=list(spec.allowed_hosts),
                        timeout=timeout,
                    ),
                    # 每个站一套闸口：站点不同、限流不该互相排队
                    gate=HttpGate(min_interval=min_interval, max_wait=max_wait),
                    user_agent=user_agent,
                    max_bytes=max_bytes,
                    send_referer=send_referer,
                )
                sites.append(
                    SiteRuntime(
                        alias=spec.alias,
                        source=source,
                        client=client,
                        service=TextureService(client, self.cache),
                    )
                )
            if not sites:
                raise ValueError("所有站点都初始化失败")
            self.sites = sites
            self.default = self._pick_default(sites, conf)
            self.init_error = ""
            self.logger.info(
                "littleskin_skin ready: sites=%s default=%s cache=%s",
                ",".join(f"{rt.alias}({rt.base_url})" for rt in sites),
                self.default.alias,
                cache_dir,
            )
        except Exception as exc:  # noqa: BLE001
            self.init_error = str(exc) or exc.__class__.__name__
            self.logger.warning("插件初始化失败：%s", self.init_error)

    @staticmethod
    def _pick_default(sites: list[SiteRuntime], conf: dict) -> SiteRuntime:
        wanted = str(conf.get("default_site") or "").strip().lower()
        if wanted:
            for rt in sites:
                if rt.alias == wanted:
                    return rt
        return sites[0]

    async def terminate(self) -> None:
        for rt in self.sites:
            try:
                await rt.client.close()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("关闭站点 %s 的连接失败：%s", rt.alias, exc)
        self.sites = []
        self.default = None

    # ------------------------------------------------------------------ 站点选择

    def _site(self, selector: str = "") -> SiteRuntime:
        if not self.sites or self.default is None:
            raise SkinError(f"皮肤站未就绪：{self.init_error or '尚未初始化'}")
        alias = (selector or "").strip().lower().lstrip("-")
        if not alias:
            return self.default
        for rt in self.sites:
            if rt.alias == alias:
                return rt
        names = "、".join(rt.alias for rt in self.sites)
        raise SkinError(f"没有叫「{clean_text(alias, 16)}」的站点。可用：{names}")

    @staticmethod
    def _selector_of(event: AstrMessageEvent) -> str:
        return _split_site_flag(event.get_message_str() or "")[1]

    def _prefix(self, rt: SiteRuntime) -> str:
        """多站点时在文案里标一下来源，单站点不啰嗦。"""
        return f"[{rt.alias}] " if len(self.sites) > 1 else ""

    # ------------------------------------------------------------------ 命令

    @filter.command_group(COMMAND_GROUP)
    def skin_group(self) -> None:
        """皮肤查询：/skin <关键词> | /skin get <tid> | /skin view <tid> | /skin raw <tid> | /skin rand | /skin sites"""

    @skin_group.command("search")
    async def cmd_search(self, event: AstrMessageEvent, keyword: GreedyStr) -> None:
        """搜索皮肤：/skin search <关键词> [-s 站点]"""
        keyword, _ = _split_site_flag(str(keyword or ""))
        return await self._search(event, keyword, self._selector_of(event))

    @skin_group.command("get")
    async def cmd_get(self, event: AstrMessageEvent, tid: str = "") -> None:
        """渲染图（3D 预览）+ 皮肤文件：/skin get <tid> [-s 站点]"""
        tid, spilled = _split_site_flag(tid)
        selector = spilled or self._selector_of(event)
        return await self._get(event, tid.split(" ")[0] if tid else "", raw=False, selector=selector)

    @skin_group.command("raw")
    async def cmd_raw(self, event: AstrMessageEvent, tid: str = "") -> None:
        """只发皮肤文件：/skin raw <tid> [-s 站点]"""
        tid, spilled = _split_site_flag(tid)
        selector = spilled or self._selector_of(event)
        return await self._get(
            event, tid.split(" ")[0] if tid else "", raw=True, selector=selector
        )

    @skin_group.command("view", alias={"3d", "preview"})
    async def cmd_view(self, event: AstrMessageEvent, tid: str = "") -> None:
        """只发 3D 预览图：/skin view <tid> [-s 站点]"""
        tid, spilled = _split_site_flag(tid)
        selector = spilled or self._selector_of(event)
        return await self._get(
            event, tid.split(" ")[0] if tid else "", raw=False, view=True, selector=selector
        )

    @skin_group.command("rand")
    async def cmd_rand(self, event: AstrMessageEvent) -> None:
        """随机一张公开皮肤：/skin rand [-s 站点]"""
        return await self._rand(event, self._selector_of(event))

    @skin_group.command("sites")
    async def cmd_sites(self, event: AstrMessageEvent) -> None:
        """列出已配置的皮肤站：/skin sites"""
        if not self.sites:
            return _text(event, f"皮肤站未就绪：{self.init_error or '尚未初始化'}")
        lines = [f"已配置 {len(self.sites)} 个皮肤站："]
        for rt in self.sites:
            mark = "（默认）" if rt is self.default else ""
            lines.append(f"· {rt.alias}：{rt.base_url}{mark}")
        if len(self.sites) > 1:
            lines.append("换站：在命令后面加 -s 别名，例如 /skin 自由高达 -s " + self.sites[-1].alias)
        return _text(event, "\n".join(lines))

    @filter.custom_filter(SkinKeywordFilter, False)
    async def cmd_keyword_search(self, event: AstrMessageEvent) -> None:
        """/skin <关键词> —— 直接搜索"""
        text, _ = _split_site_flag(event.get_message_str() or "")
        text = re.sub(r"\s+", " ", text.strip())
        return await self._search(
            event, text[len(COMMAND_GROUP) :].strip(), self._selector_of(event)
        )

    # ------------------------------------------------------------------ 业务流程

    async def _guard(self, event: AstrMessageEvent, ns: str = "cmd", cap: float | None = None) -> str:
        """返回错误文案；空串表示可以继续。

        `ns` 把指令和 llm 工具的冷却分开计：用户先 /skin 再随口让模型发一张，
        不应该被自己在两秒前发的指令挡住。
        `cap` 给工具用：指令是用户手打，3 秒合理；工具由模型调用（每次至少 1.5 秒），
        同一句话里要两张图是完全正常的，所以把工具侧的冷却压到 1 秒。
        """
        if not self.service or self.init_error:
            return f"皮肤站插件未正确配置：{self.init_error or '尚未初始化'}"
        limit = _float(self.conf, "user_cooldown", 3, 0, 600)
        if cap is not None:
            limit = min(limit, cap)
        if limit > 0:
            key = f"{ns}:{event.unified_msg_origin or event.get_session_id()}"
            now = time.monotonic()
            last = self._cooldown.get(key, 0.0)
            if now - last < limit:
                return f"手速太快了，请 {limit - (now - last):.0f} 秒后再试。"
            self._cooldown[key] = now
            if len(self._cooldown) > 2048:
                cutoff = now - 600
                self._cooldown = {k: v for k, v in self._cooldown.items() if v > cutoff}
        return ""

    async def _search(self, event: AstrMessageEvent, keyword: str, selector: str = ""):
        if err := await self._guard(event):
            return _text(event, err)
        try:
            rt = self._site(selector)
        except SkinError as exc:
            return _text(event, str(exc))
        keyword = (keyword or "").strip()
        if not keyword:
            return _text(event, "请输入关键词，例如 /skin search 自由高达")
        keyword = keyword[:64]
        shown_kw = clean_text(keyword)  # 回显给用户的关键词也走同一套清洗
        try:
            items = await self._search_with_fallback(rt, keyword)
        except SkinError as exc:
            return _text(event, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("search failed: %s", exc)
            return _text(event, "搜索失败，请稍后再试。")
        prefix = self._prefix(rt)
        limit = _int(self.conf, "max_results", 5, 1, 10)
        shown = items[:limit]
        if not shown:
            return _text(
                event,
                f"{prefix}没有找到与「{shown_kw}」匹配的皮肤。\n"
                "提示：该站搜索区分大小写，换一种大小写写法可能就有了。",
            )
        lines = [f"{prefix}搜索「{shown_kw}」得到 {len(items)} 条（最多显示 {limit} 条）："]
        for idx, item in enumerate(shown, 1):
            lines.append(f"{idx}. {item_line(item)}")
        lines.append(
            "用 /skin get <tid> 取渲染图 + 皮肤文件，/skin view <tid> 只看 3D，"
            "/skin raw <tid> 只取皮肤文件。"
        )
        if len(self.sites) > 1:
            lines.append(f"这条结果来自 {rt.alias}；换站就再加一句 -s 别名。")
        return _text(event, "\n".join(lines))

    async def _search_with_fallback(self, rt: SiteRuntime, keyword: str) -> list[SkinItem]:
        """实测该站搜索区分大小写（GUNDAM 6 条 / gundam 1 条 / Gundam 7 条），
        所以纯 ASCII 小写关键词首查为空时，再试两种大小写变体。"""
        items = await rt.source.search(rt.client, keyword, page=1)
        if items or not self.conf.get("case_fallback", True):
            return items
        if keyword.isascii() and keyword.islower():
            for variant in (keyword.upper(), keyword.capitalize()):
                items = await rt.source.search(rt.client, variant, page=1)
                if items:
                    return items
        return items

    async def _render_skin(
        self, rt: SiteRuntime, tid: str, *, view: bool = False, raw: bool = False
    ) -> tuple[str, str, str, str]:
        """下载 + 渲染，返回 (渲染图路径, 原始纹理路径, 图注, 皮肤名)。

        raw=True 时渲染图路径就是原图路径。失败抛 NotFoundError / SkinError。
        指令（`_get`）和 llm 工具（`littleskin_skin_image`）共用这一条路径，
        免得两边各写一套、以后改了一边忘另一边。
        """
        max_pixels = _int(self.conf, "max_image_pixels", 4_000_000, 1024, 4_000_000)
        detail = await rt.source.detail(rt.client, tid)
        if detail.tid is None:
            raise NotFoundError("未找到")
        url = rt.source.texture_url(detail)
        if not url:
            raise SkinError("该皮肤没有可用的纹理地址（未找到）。")
        # 有的站按 tid 给纹理（没 hash）：那就用 URL 摘要当缓存键（仍是合法 hex）
        key_id = detail.hash if is_valid_hash(detail.hash) else hashlib.sha256(url.encode()).hexdigest()
        raw_path, _, image = await rt.service.load(
            hash_=key_id,
            url=url,
            referer=rt.source.page_url(tid),
            allow_hd=bool(self.conf.get("allow_hd", True)),
            max_pixels=max_pixels,
        )
        caption = (
            f"{detail.name} · tid {detail.tid_str} · {detail.kind} · "
            f"{image.width}x{image.height} · {detail.uploader}"
        )
        if raw:
            return raw_path, raw_path, f"{caption} · 皮肤文件", detail.name
        plan = _plan(self.conf, detail, view, max_pixels)
        if view:
            caption += " · 3D 预览"
        key = _render_key(key_id, plan)
        rendered = await self.cache.get(key)
        if rendered is None:
            data = await asyncio.to_thread(render_to_bytes, image, plan)
            rendered = await rt.service.store_rendered(key, data)
        return rendered, raw_path, caption, detail.name

    async def _get(
        self,
        event: AstrMessageEvent,
        tid: str,
        raw: bool,
        guarded: bool = True,
        view: bool = False,
        selector: str = "",
    ):
        if guarded and (err := await self._guard(event)):
            return _text(event, err)
        try:
            rt = self._site(selector)
        except SkinError as exc:
            return _text(event, str(exc))
        tid = (tid or "").strip()
        if not is_valid_tid(tid):
            return _text(event, "tid 只能是数字，例如 /skin get 687942")
        try:
            render_path, raw_path, caption, name = await self._render_skin(
                rt, tid, view=view, raw=raw
            )
        except NotFoundError:
            return _text(event, f"没找到 tid {tid} 对应的皮肤（{rt.alias}）。")
        except SkinError as exc:
            return _text(event, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("get failed for tid %s: %s", tid, exc)
            return _text(event, "下载失败，请稍后再试。")
        caption = self._prefix(rt) + caption

        if raw:
            # /skin raw：只要文件，不要渲染图
            note = await self._send_skin_file(event, raw_path, name, tid)
            return _text(event, f"{caption}\n{note}") if note else _text(event, caption)

        # 先发渲染图，再单独发皮肤文件：用户要的是「能用的文件」，不该被揉进拼图
        await event.send(_image(event, render_path, caption))
        if view:
            return None  # /skin view 只要 3D，不堆文件
        note = await self._send_skin_file(event, raw_path, name, tid)
        return _text(event, note) if note else None

    async def _send_skin_file(
        self, event: AstrMessageEvent, raw_path: str, name: str, tid: str
    ) -> str:
        """把 64×64 原图当**文件**发出去（可直接下载导入），返回附加说明（失败时）。

        文件走 AstrBot 的文件服务：`file_token_service` 死签一个一次性 token，
        拼成 `{base}/api/file/{token}` 交给 File 消息段，NapCat 再去下载真正的字节。
        （NapCat 与 AstrBot 不在同一台机器时，`callback_api_base` 必须填 NapCat 能访问到的地址，
        例如反向 SSH 隧道 / 内网地址；同机部署直接用 AstrBot 本机地址即可。）
        """
        mode = str(self.conf.get("skin_file_mode") or "file").strip().lower()
        if mode == "none":
            return ""
        filename = _safe_filename(name, tid)
        if mode in ("file", "both"):
            base = self._file_base()
            if base:
                sent = await self._send_file_attachment(event, raw_path, filename, base)
                if sent and mode == "both":
                    await event.send(event.chain_result([Image.fromFileSystem(raw_path)]))
                if sent:
                    return ""
            else:
                self.logger.warning(
                    "没有 file_url_base / callback_api_base，皮肤文件改用图片发送"
                )
        await event.send(event.chain_result([Image.fromFileSystem(raw_path)]))
        return "（文件没发出去，已改发原图；需要文件可以说 /skin raw <tid>）"

    async def _send_file_attachment(
        self, event: AstrMessageEvent, raw_path: str, filename: str, base: str
    ) -> bool:
        """把文件真正送到聊天里。两道都试，任一成功就返回 True。

        第一道：aiocqhttp（NapCat）专用的 `upload_group_file` / `upload_private_file`。
        这是 OneBot 标准的文件上传动作，比把 file 段塞进普通消息更可靠；
        `file` 传 token URL，NapCat 自己去下载（详见 `_send_skin_file` 的说明）。
        第二道：AstrBot 的 File 消息段（兼容非 aiocqhttp 平台 / 第一道失败时）。
        """
        try:
            from astrbot.core import file_token_service

            token = await file_token_service.register_file(raw_path, timeout=600)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("登记皮肤文件失败：%s", exc)
            return False
        url = f"{base}/api/file/{token}"

        bot = getattr(event, "bot", None)
        group_id = event.get_group_id()
        if bot is not None and hasattr(bot, "call_action"):
            try:
                if group_id:
                    await bot.call_action(
                        "upload_group_file",
                        group_id=int(group_id),
                        file=url,
                        name=filename,
                    )
                else:
                    await bot.call_action(
                        "upload_private_file",
                        user_id=int(event.get_sender_id()),
                        file=url,
                        name=filename,
                    )
                self.logger.info("皮肤文件已发送（upload_*_file）：%s", filename)
                return True
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("upload_*_file 失败：%s", exc)

        try:
            await event.send(event.chain_result([File(name=filename, url=url)]))
            self.logger.info("皮肤文件已发送（File 段）：%s", filename)
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("皮肤文件发送失败（File 段）：%s", exc)
            return False

    def _file_base(self) -> str:
        """皮肤文件的下载地址前缀：插件配置优先，否则用 AstrBot 全局 callback_api_base。"""
        explicit = str(self.conf.get("file_url_base") or "").strip()
        if explicit:
            return explicit.rstrip("/")
        try:
            cfg = self.context.get_config() if self.context else None
            return str((cfg or {}).get("callback_api_base") or "").strip().rstrip("/")
        except Exception:  # noqa: BLE001
            return ""

    async def _rand(self, event: AstrMessageEvent, selector: str = ""):
        if err := await self._guard(event):
            return _text(event, err)
        try:
            rt = self._site(selector)
        except SkinError as exc:
            return _text(event, str(exc))
        try:
            item = await rt.source.random_item(rt.client)
        except SkinError as exc:
            return _text(event, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("rand failed: %s", exc)
            return _text(event, "随机获取失败，请稍后再试。")
        if item is None or item.tid is None:
            return _text(event, "皮肤站没有返回可用的公开皮肤。")
        return await self._get(event, str(item.tid), raw=False, guarded=False, selector=rt.alias)

    # ------------------------------------------------------------------ LLM 函数工具
    #
    # 只做两件事：查名字、发图。所有参数都是结构化字符串，不给模型「传 JSON 让插件干别的」的口子；
    # 返回给模型的永远是 clean_text 清洗 + 截断后的文本，站点原始 JSON 不进上下文。

    @filter.llm_tool(name="littleskin_search")
    async def tool_search(self, event: AstrMessageEvent, keyword: str, site: str = "") -> str:
        """在皮肤站搜索 Minecraft 皮肤，返回皮肤名清单。用户想找/想换某个 MC 皮肤时先用这个。

        一次调用就够：搜不到就直接告诉用户站里没有，别换近义词反复试（最多再试一次）。

        Args:
            keyword(string): 搜索关键词，角色名、作品名或皮肤名，中英文都行
            site(string): 站点别名，留空用默认站；多个站时才需要填（用 littleskin_sites 可以查）
        """
        if not self.sites or self.init_error:
            return f"皮肤站插件不可用：{self.init_error or '尚未初始化'}"
        try:
            rt = self._site(site)
        except SkinError as exc:
            return str(exc)
        keyword = (keyword or "").strip()[:64]
        if not keyword:
            return "关键词是空的，没法搜。"
        try:
            items = await self._search_with_fallback(rt, keyword)
        except SkinError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("tool search failed: %s", exc)
            return "搜索失败，稍后再试。"
        prefix = f"[{rt.alias}] " if len(self.sites) > 1 else ""
        if not items:
            return (
                f"{prefix}没搜到「{clean_text(keyword)}」"
                "（该站搜索区分大小写，换个大小写写法可能就有）。"
            )
        shown = items[: min(_int(self.conf, "max_results", 5, 1, 10), 8)]
        lines = [f"{prefix}搜到 {len(items)} 条，前 {len(shown)} 个："]
        lines += [f"{idx}. {clean_text(item.name)}" for idx, item in enumerate(shown, 1)]
        hint = "要发其中某一张就调用 littleskin_skin_image，name 原样填上面的皮肤名"
        lines.append(hint + (f"，site 填 {rt.alias}。" if len(self.sites) > 1 else "。"))
        return "\n".join(lines)

    @filter.llm_tool(name="littleskin_sites")
    async def tool_sites(self, event: AstrMessageEvent) -> str:
        """列出已配置的皮肤站别名。用户提到某个皮肤站、或需要指定站点时先用它。"""
        if not self.sites:
            return f"皮肤站插件不可用：{self.init_error or '尚未初始化'}"
        lines = [f"已配置 {len(self.sites)} 个皮肤站："]
        for rt in self.sites:
            mark = "（默认）" if rt is self.default else ""
            lines.append(f"· {rt.alias}：{rt.base_url}{mark}")
        return "\n".join(lines)

    @filter.llm_tool(name="littleskin_skin_image")
    async def tool_skin_image(self, event: AstrMessageEvent, name: str, site: str = "") -> str:
        """把一张 MC 皮肤渲染成 3D 预览图、并把原始皮肤文件一起发给用户。用户说「发一下 / 给我看看 / 来一张」某个皮肤时用这个。

        用户一次要好几张就连续调用多次，间隔够 1 秒即可。

        Args:
            name(string): 皮肤名（用 littleskin_search 搜到的名字，原样填），或纯数字的 tid
            site(string): 站点别名，留空用默认站；多个站时才需要填
        """
        if err := await self._guard(event, ns="tool", cap=1.0):
            return err
        try:
            rt = self._site(site)
        except SkinError as exc:
            return str(exc)
        spec = (name or "").strip()[:64]
        if not spec:
            return "要发哪一张？给我皮肤名或 tid。"
        note = ""
        if is_valid_tid(spec):
            tid = spec
        else:
            try:
                items = await self._search_with_fallback(rt, spec)
            except SkinError as exc:
                return str(exc)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("tool image search failed: %s", exc)
                return "搜索失败，稍后再试。"
            if not items:
                return f"没搜到「{clean_text(spec)}」，换个名字再试，或者直接给我 tid。"
            picked = _pick_by_name(items, spec)
            if picked is None or picked.tid is None:
                return f"「{clean_text(spec)}」搜到了结果，但没有可用的纹理。"
            tid = str(picked.tid)
            if len(items) > 1:
                note = (
                    f"（「{clean_text(spec)}」共 {len(items)} 条，取了「{clean_text(picked.name)}」；"
                    "用户要的不是这张就让他说清楚点）"
                )
        try:
            render_path, raw_path, caption, detail_name = await self._render_skin(rt, tid)
        except NotFoundError:
            return f"没找到 tid {tid} 对应的皮肤。"
        except SkinError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("tool image failed for tid %s: %s", tid, exc)
            return "下载失败，稍后再试。"
        # 图直接发出去（tool 返回值会进模型上下文，图片不走那条路）
        await event.send(MessageChain(chain=[Plain(caption), Image.fromFileSystem(render_path)]))
        await self._send_skin_file(event, raw_path, detail_name, tid)
        return f"3D 预览图和皮肤文件都已发给用户。{note}"


def _pick_by_name(items: list[SkinItem], wanted: str) -> SkinItem | None:
    """从搜索结果里挑最可能是用户要的那张：名字全等 → 互相包含 → 第一条。"""

    def norm(text: str) -> str:
        return re.sub(r"\s+", "", text or "").lower()

    want = norm(wanted)
    for item in items:
        if norm(item.name) == want:
            return item
    if want:
        for item in items:
            name = norm(item.name)
            if want in name or name in want:
                return item
    return items[0] if items else None


def _text(event: AstrMessageEvent, text: str):
    # 关掉 markdown：站点文本里的 [x](y) 之类不应被渲染成链接
    return event.make_result().message(text).use_markdown(False)


def _image(event: AstrMessageEvent, path: str, caption: str):
    return event.make_result().message(caption).file_image(path).use_markdown(False)


def item_line(item: SkinItem) -> str:
    bits = [item.name, f"tid {item.tid_str}", item.kind]
    if item.hd:
        bits.append("HD")
    bits.append(f"赞 {item.likes if item.likes is not None else NOT_FOUND}")
    bits.append(item.uploader)
    return " · ".join(bits)


def _views_3d(conf: dict) -> tuple[float, ...]:
    """配置里写 "0, 30, 180"（或列表）；最多 MAX_VIEWS 个，防止拼出超宽图。"""
    raw = conf.get("views_3d", "0, 30, 180")
    parts = raw if isinstance(raw, (list, tuple)) else str(raw).replace("，", ",").split(",")
    angles: list[float] = []
    for item in parts:
        try:
            angles.append(float(str(item).strip()) % 360.0)
        except (TypeError, ValueError):
            continue
    return tuple(angles[:MAX_VIEWS]) or DEFAULT_VIEWS


def _is_slim(conf: dict, detail: SkinDetail) -> bool:
    """alex（slim）模型的手臂只有 3 像素宽，判错会让手臂贴图错位半格。

    LittleSkin 的 type 字段实测就是模型名（steve / alex），所以默认跟着站点走，
    skin_model 配置留作人工纠正的口子。
    """
    forced = str(conf.get("skin_model") or "auto").strip().lower()
    if forced in ("alex", "slim"):
        return True
    if forced in ("steve", "classic", "wide"):
        return False
    kind = detail.kind.lower()
    return "alex" in kind or "slim" in kind


def _plan(conf: dict, detail: SkinDetail, view: bool, max_pixels: int) -> dict:
    """把配置折成一次渲染需要的参数，同时作为缓存键的输入。

    默认只出 3D 预览：纹理表由皮肤文件单独发送，正面展开默认关
    （以前那段“中间的小条”就是它，想看配色再手动开 flatten）。
    """
    return {
        "zoom": _int(conf, "zoom", 10, 1, 32),
        "flatten": bool(conf.get("flatten", False)) and not view,
        "three_d": bool(conf.get("preview_3d", True)) or view,
        "sheet": bool(conf.get("include_sheet", False)) and not view,
        "slim": _is_slim(conf, detail),
        "views": _views_3d(conf),
        "max_pixels": max_pixels,
    }


def _render_key(hash_: str, plan: dict) -> str:
    tag = "".join(
        (
            "v" if not plan["sheet"] else "g",
            str(plan["zoom"]),
            "f" if plan["flatten"] else "",
            "3" if plan["three_d"] else "",
            "s" if plan["slim"] else "",
            "v" + "-".join(f"{v:g}" for v in plan["views"]),
        )
    )
    key = f"{hash_}_{tag}"
    if len(key) > 128:  # 缓存文件名有长度上限，超长时整体退化为摘要
        key = hashlib.sha256(key.encode()).hexdigest()
    return key


def render_to_bytes(image, plan: dict) -> bytes:
    try:
        out = render_preview(image, **plan)
    except (ValueError, OSError) as exc:
        # 3D 只是附加的一栏，渲染不出来时宁可退回 2D，也不能整条消息失败。
        if not plan.get("three_d"):
            raise
        logging.getLogger("littleskin_skin").warning("3d preview failed: %s", exc)
        out = render_preview(image, **{**plan, "three_d": False})
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
