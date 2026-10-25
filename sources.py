"""皮肤站适配器。

只负责“把某个站的 HTTP 接口翻译成本地数据结构”，不做下载、不做渲染。
新增一个站 = 继承 SkinSource（或复用 BlessingSkinSource）并注册到 SOURCES。

本模块及其同级模块（textures.py / render.py）刻意不 import astrbot，
以便脱离 AstrBot 运行时单独自测。
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

NOT_FOUND = "未找到"

_CONTROL_OR_FORMAT = re.compile(r"[\x00-\x20\x7f\u200b-\u200f\u202a-\u202e\ufeff]")
_TEXT_LIMIT_DEFAULT = 40


def clean_text(value: Any, limit: int = _TEXT_LIMIT_DEFAULT) -> str:
    """把站点返回的第三方文本变成可安全发到聊天里的字符串。

    皮肤名 / 上传者昵称都是别人可控的内容，可能夹带控制字符、零宽字符、
    方向覆盖符（提示注入与终端转义常用的载体），因此一律清洗后再输出。
    llm 工具（littleskin_search）只把**清洗 + 截断后**的皮肤名交给模型，
    站点原始 JSON 永远不进 LLM 上下文。
    """
    if value is None:
        return NOT_FOUND
    text = value if isinstance(value, str) else str(value)
    text = "".join(
        ch
        for ch in text
        if unicodedata.category(ch)[0] != "C" or ch in ("\n", "\t")
    )
    text = _CONTROL_OR_FORMAT.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return NOT_FOUND
    if len(text) > limit:
        # 省略号也算进长度：需求是“截断到 40 字符”，不是 40+1
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def as_int(value: Any, default: int | None = None) -> int | None:
    try:
        if isinstance(value, bool) or value is None:
            return default
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return None


@dataclass(frozen=True)
class SiteConfig:
    """站点端点/参数名。第三方站没有 API 契约，全部放配置文件。"""

    base_url: str
    search_path: str = "/skinlib/list"
    search_keyword_param: str = "keyword"
    search_page_param: str = "page"
    search_random_param: str = "order"
    search_random_value: str = "random"
    detail_path: str = "/texture/{tid}"
    texture_path: str = "/textures/{hash}"
    show_path: str = "/skinlib/show/{tid}"
    fields: FieldMap = field(default_factory=lambda: FieldMap())

    @classmethod
    def from_conf(cls, conf: dict[str, Any]) -> SiteConfig:
        def text(key: str, fallback: str) -> str:
            raw = conf.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            return fallback

        base = text("base_url", "https://littleskin.cn").rstrip("/")
        return cls(
            base_url=base,
            search_path=text("search_path", cls.search_path),
            search_keyword_param=text("search_keyword_param", cls.search_keyword_param),
            search_page_param=text("search_page_param", cls.search_page_param),
            search_random_param=str(conf.get("search_random_param") or "").strip(),
            search_random_value=text(
                "search_random_value",
                cls.search_random_value,
            ),
            detail_path=text("detail_path", cls.detail_path),
            texture_path=text("texture_path", cls.texture_path),
            show_path=text("show_path", cls.show_path),
            fields=FieldMap.from_conf(conf),
        )

    @property
    def root(self) -> str:
        return self.base_url

    def url(self, template: str, **kw: str) -> str:
        """拼 URL。模板可以是相对路径，也可以是完整 URL（带 http:// / https://）。"""
        target = template
        for key, value in kw.items():
            target = target.replace("{" + key + "}", str(value))
        if target.startswith(("http://", "https://")):
            return target
        if not target.startswith("/"):
            target = "/" + target
        return self.base_url + target


@dataclass(frozen=True)
class FieldMap:
    """响应字段名映射。

    Blessing Skin 系的字段大体一致（`tid` / `name` / `type` / `hash`），但各家魔改过的不算少，
    所以字段名全部可配：改配置就能接，不用写子类。列表外层的包装键也在这里（`list_key`，
    支持 `data.items` 这种点号路径）。
    """

    tid: str = "tid"
    name: str = "name"
    kind: str = "type"
    hash: str = "hash"
    likes: str = "likes"
    uploader: str = "nickname"
    uploader_fallback: str = "uploader"
    hd: str = "hd"
    public: str = "public"
    size: str = "size"
    upload_at: str = "upload_at"
    texture_url: str = ""  # 站点直接给了完整纹理 URL 时填这里（留空 = 用 texture_path 拼）
    list_key: str = "data"

    @classmethod
    def from_conf(cls, conf: dict[str, Any]) -> FieldMap:
        def text(key: str, fallback: str) -> str:
            raw = conf.get(key)
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
            return fallback

        return cls(
            tid=text("field_tid", cls.tid),
            name=text("field_name", cls.name),
            kind=text("field_type", cls.kind),
            hash=text("field_hash", cls.hash),
            likes=text("field_likes", cls.likes),
            uploader=text("field_uploader", cls.uploader),
            uploader_fallback=text("field_uploader_fallback", cls.uploader_fallback),
            hd=text("field_hd", cls.hd),
            public=text("field_public", cls.public),
            size=text("field_size", cls.size),
            upload_at=text("field_upload_at", cls.upload_at),
            texture_url=str(conf.get("field_texture_url") or "").strip(),
            list_key=text("list_key", cls.list_key),
        )

    @staticmethod
    def pick(obj: dict, key: str) -> Any:
        """按 `a.b` 这种路径取值（字段名本身含点也行：先整键，再按路径）。"""
        if not key:
            return None
        if key in obj:
            return obj[key]
        node: Any = obj
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


def pick_list(payload: Any, list_key: str) -> list[dict]:
    """从响应里取出列表（支持 `data` / 裸数组 / `data.items` 这种嵌套）。"""
    node: Any = payload
    if list_key:
        if not isinstance(node, dict):
            return [] if not isinstance(node, list) else [x for x in node if isinstance(x, dict)]
        node = FieldMap.pick(node, list_key)
    if isinstance(node, dict):  # 最后一道宽容：只有一层时再摸摸 items / list
        for alt in ("items", "list"):
            if isinstance(node.get(alt), list):
                node = node[alt]
                break
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, dict)]


@dataclass
class SkinItem:
    """列表项。缺字段一律降级为“未找到”，不抛异常。"""

    tid: int | None = None
    name: str = NOT_FOUND
    kind: str = NOT_FOUND
    likes: int | None = None
    uploader: str = NOT_FOUND
    hd: bool | None = None
    public: bool | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def tid_str(self) -> str:
        return str(self.tid) if self.tid is not None else NOT_FOUND

    @classmethod
    def from_json(cls, obj: Any, fields: FieldMap | None = None) -> SkinItem:
        if not isinstance(obj, dict):
            return cls()
        fields = fields or FieldMap()
        pick = fields.pick
        return cls(
            tid=as_int(pick(obj, fields.tid)),
            name=clean_text(pick(obj, fields.name)),
            kind=clean_text(pick(obj, fields.kind), 16),
            likes=as_int(pick(obj, fields.likes), 0),
            uploader=clean_text(
                pick(obj, fields.uploader) or pick(obj, fields.uploader_fallback)
            ),
            hd=as_bool(pick(obj, fields.hd)),
            public=as_bool(pick(obj, fields.public)),
            raw=obj,
        )


@dataclass
class SkinDetail:
    tid: int | None = None
    name: str = NOT_FOUND
    kind: str = NOT_FOUND
    hash: str = ""
    size: int | None = None
    uploader: str = NOT_FOUND
    public: bool | None = None
    upload_at: str = NOT_FOUND
    likes: int | None = None
    hd: bool | None = None
    texture_url: str = ""  # 站点直接给的纹理地址（可选，优先于 hash 拼装）

    @property
    def tid_str(self) -> str:
        return str(self.tid) if self.tid is not None else NOT_FOUND

    @classmethod
    def from_json(cls, obj: Any, fields: FieldMap | None = None) -> SkinDetail:
        if not isinstance(obj, dict):
            return cls()
        fields = fields or FieldMap()
        pick = fields.pick
        raw_hash = pick(obj, fields.hash)
        hash_ = raw_hash.strip() if isinstance(raw_hash, str) else ""
        direct = pick(obj, fields.texture_url) if fields.texture_url else None
        return cls(
            tid=as_int(pick(obj, fields.tid)),
            name=clean_text(pick(obj, fields.name)),
            kind=clean_text(pick(obj, fields.kind), 16),
            hash=hash_ if re.fullmatch(r"[0-9a-fA-F]{8,128}", hash_) else "",
            size=as_int(pick(obj, fields.size)),
            uploader=clean_text(
                pick(obj, fields.uploader) or pick(obj, fields.uploader_fallback)
            ),
            public=as_bool(pick(obj, fields.public)),
            upload_at=clean_text(pick(obj, fields.upload_at), 32),
            likes=as_int(pick(obj, fields.likes), 0),
            hd=as_bool(pick(obj, fields.hd)),
            texture_url=direct.strip() if isinstance(direct, str) else "",
        )


class SkinSource(ABC):
    """皮肤站适配器基类。"""

    name: str = "base"

    def __init__(self, site: SiteConfig, logger: logging.Logger | None = None) -> None:
        self.site = site
        self.logger = logger or logging.getLogger("astrbot_plugin_skinsite_skin")

    @property
    def base_url(self) -> str:
        return self.site.base_url

    # ---- 子类需要给出的接口 ------------------------------------------------

    @abstractmethod
    def search_params(self, keyword: str, page: int) -> dict[str, str]: ...

    @abstractmethod
    async def search(
        self,
        http,
        keyword: str,
        page: int = 1,
        random_order: bool = False,
    ) -> list[SkinItem]: ...

    @abstractmethod
    async def detail(self, http, tid: str) -> SkinDetail: ...

    # ---- 与站点无关的公共部分 ----------------------------------------------

    def texture_url(self, detail: SkinDetail) -> str:
        """纹理地址：站点直接给了 URL 就用它，否则用 `texture_path` 模板拼。

        模板里可用 `{hash}`，也可用 `{tid}`；模板本身写成完整 URL 也支持（不拼 base_url）。
        拼完仍带未替换占位符（例如没 hash 而模板只要 `{hash}`）时返回空串，
        由调用方给出“没有可用纹理地址”的提示。
        """
        if detail.texture_url:
            return detail.texture_url
        if "{hash}" in self.site.texture_path and not detail.hash:
            return ""  # 模板要 hash 但站点没给 → 别拼出一个必然 404 的地址
        url = self.site.url(
            self.site.texture_path,
            hash=detail.hash,
            tid=detail.tid_str if detail.tid is not None else "",
        )
        return "" if "{" in url else url

    def page_url(self, tid: str | int) -> str:
        return self.site.url(self.site.show_path, tid=str(tid))

    async def random_item(self, http) -> SkinItem | None:
        """随机一张公开皮肤。

        站点分页信息里没有 total / last_page，所以优先用站点的随机排序参数
        （实测 littleskin 支持 order=random），退化时才从第一页里随机挑一条。
        """
        items: list[SkinItem] = []
        try:
            items = await self.search(http, keyword="", page=1, random_order=True)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("random search failed: %s", exc)
        if not items:
            try:
                items = await self.search(http, keyword="", page=1)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("fallback search failed: %s", exc)
                items = []
        if not items:
            return None
        return random.choice(items)

    def parse_list(self, payload: Any, fields: FieldMap | None = None) -> list[SkinItem]:
        fields = fields or self.site.fields
        return [SkinItem.from_json(obj, fields) for obj in pick_list(payload, fields.list_key)]


class BlessingSkinSource(SkinSource):
    """Blessing Skin 通用适配位。

    LittleSkin 就是 Blessing Skin 的一个分支，路由与参数名一致，
    因此通用实现直接按 SiteConfig 里可配置的端点走。
    """

    name = "blessing_skin"

    def search_params(self, keyword: str, page: int) -> dict[str, str]:
        params: dict[str, str] = {}
        # 只发送站点确认支持的参数：实测传未知参数（如 sort=random）会 500，
        # 传 filter=all 会让 data 变成空数组。
        if keyword:
            params[self.site.search_keyword_param] = keyword
        if page and page > 1:
            params[self.site.search_page_param] = str(page)
        return params

    async def search(
        self,
        http,
        keyword: str,
        page: int = 1,
        random_order: bool = False,
    ) -> list[SkinItem]:
        params = self.search_params(keyword, page)
        if random_order and self.site.search_random_param:
            params[self.site.search_random_param] = self.site.search_random_value
        url = self.site.url(self.site.search_path)
        payload = await http.get_json(url, params)
        return self.parse_list(payload, self.site.fields)

    async def detail(self, http, tid: str) -> SkinDetail:
        url = self.site.url(self.site.detail_path, tid=tid)
        payload = await http.get_json(url, {})
        return SkinDetail.from_json(payload, self.site.fields)


class LittleSkinSource(BlessingSkinSource):
    """littleskin.cn —— 实测端点见 README。"""

    name = "littleskin"


SOURCES: dict[str, type[SkinSource]] = {
    LittleSkinSource.name: LittleSkinSource,
    BlessingSkinSource.name: BlessingSkinSource,
}


@dataclass(frozen=True)
class SiteSpec:
    """一个站点的完整描述：别名 + 适配器 +端点/参数名 + 出站白名单。"""

    alias: str
    adapter: str
    config: SiteConfig
    allowed_hosts: tuple[str, ...]


# 单行站点的格式：别名|适配器|站点根|白名单[,白名单]|可选 key=value...
# 例：ls|littleskin|https://littleskin.cn|littleskin.cn
#     bs|blessing_skin|https://skin.example.com|skin.example.com|search_path=/skinlib/list
_OVERRIDABLE = (
    "search_path",
    "search_keyword_param",
    "search_page_param",
    "search_random_param",
    "search_random_value",
    "detail_path",
    "texture_path",
    "show_path",
    # 字段名映射（各家 Blessing Skin 魔改不一致的地方）
    "list_key",
    "field_tid",
    "field_name",
    "field_type",
    "field_hash",
    "field_likes",
    "field_uploader",
    "field_uploader_fallback",
    "field_hd",
    "field_public",
    "field_size",
    "field_upload_at",
    "field_texture_url",
)
_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,16}$")


def _site_path_defaults(conf: dict[str, Any]) -> dict[str, Any]:
    """顶层那几个路径/参数名配置是所有站点的默认值（单站点老配置也靠它）。"""
    return {key: conf.get(key) for key in _OVERRIDABLE if conf.get(key) not in (None, "")}


def _hosts_from(raw: str, base_url: str) -> tuple[str, ...]:
    hosts = [h.strip().lower() for h in re.split(r"[,;\s]+", raw or "") if h.strip()]
    if hosts:
        return tuple(dict.fromkeys(hosts))
    # 没写白名单就拿站点根的 hostname 充数（少一个必填项，但不放宽）“
    match = re.match(r"https?://([^/:]+)", base_url or "")
    return (match.group(1).lower(),) if match else ()


def parse_sites(
    conf: dict[str, Any],
    logger: logging.Logger | None = None,
) -> tuple[list[SiteSpec], list[str]]:
    """把配置解析成站点列表（按顺序）。返回 (站点, 问题清单)。

    `sites` 为空时退回单站点老配置（site/base_url/allowed_hosts/...），
    因此升级插件不会把已有配置弄丢；多站点就在 `sites` 里一行一个地写。
    """
    logger = logger or logging.getLogger("astrbot_plugin_skinsite_skin")
    problems: list[str] = []
    defaults = _site_path_defaults(conf)
    specs: list[SiteSpec] = []
    seen: set[str] = set()

    raw_sites = conf.get("sites")
    entries = (
        [str(x) for x in raw_sites if str(x).strip()]
        if isinstance(raw_sites, (list, tuple))
        else []
    )
    if not entries and isinstance(raw_sites, str):
        entries = [line.strip() for line in raw_sites.splitlines() if line.strip()]

    for line in entries:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            problems.append(f"站点配置至少需要 别名|适配器|站点根：{line!r}")
            continue
        alias, adapter, base_url = parts[0].lower(), parts[1].lower() or "blessing_skin", parts[2]
        if not _ALIAS_RE.match(alias):
            problems.append(f"别名只能是小写字母/数字/-/_(1~16)：{alias!r}")
            continue
        if alias in seen:
            problems.append(f"别名重复，已忽略：{alias!r}")
            continue
        if not base_url.startswith("https://") and "127.0.0.1" not in base_url and "localhost" not in base_url:
            problems.append(f"站点根必须是 https（{alias}）：{base_url!r}")
            continue
        overrides = dict(defaults)
        overrides["base_url"] = base_url.rstrip("/")
        ok = True
        for extra in parts[4:]:
            if "=" not in extra:
                problems.append(f"覆盖项要写成 key=value，已忽略：{extra!r}")
                continue
            key, _, value = extra.partition("=")
            key = key.strip()
            if key not in _OVERRIDABLE:
                problems.append(f"不支持覆盖 {key!r}，可选：{', '.join(_OVERRIDABLE)}")
                ok = False
                continue
            overrides[key] = value.strip()
        if not ok:
            continue
        specs.append(
            SiteSpec(
                alias=alias,
                adapter=adapter,
                config=SiteConfig.from_conf(overrides),
                allowed_hosts=_hosts_from(parts[3] if len(parts) > 3 else "", base_url),
            )
        )
        seen.add(alias)

    if specs:
        return specs, problems

    # ---- 退回单站点老配置 -------------------------------------------------
    adapter = str(conf.get("site") or "littleskin").strip().lower() or "littleskin"
    site_conf = SiteConfig.from_conf(conf)
    hosts = conf.get("allowed_hosts")
    host_list = (
        tuple(str(h).strip().lower() for h in hosts if str(h).strip())
        if isinstance(hosts, (list, tuple))
        else _hosts_from(str(hosts or ""), site_conf.base_url)
    )
    if entries:  # 写了 sites 但全部不合法
        problems.append("sites 里没有任何可用的站点，已退回单站点配置")
        logger.warning("sites 配置全部非法，退回单站点配置")
    specs.append(
        SiteSpec(
            alias=adapter if _ALIAS_RE.match(adapter) else "main",
            adapter=adapter,
            config=site_conf,
            allowed_hosts=host_list,
        )
    )
    return specs, problems


def build_source(spec: SiteSpec, logger: logging.Logger | None = None) -> SkinSource:
    cls = SOURCES.get(spec.adapter)
    if cls is None:
        raise ValueError(
            f"未知皮肤站适配器 '{spec.adapter}'，可选：{', '.join(sorted(SOURCES))}",
        )
    return cls(spec.config, logger)
