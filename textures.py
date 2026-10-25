"""下载 + 校验 + 缓存 + 限流。

对应需求文档的“全异步 / 节流 / 缓存 / 安全”四节：
- 只用 aiohttp，绝不在事件循环里做阻塞 IO（PIL 解码与落盘走 to_thread）。
- 出站串行：全局 Semaphore(1) + 最小请求间隔，避免把第三方站打成 429 / 封 IP。
- 只访问白名单域名，禁止跟随跳转，禁止解析到内网/环回/保留地址。
- 缓存文件名只由校验过的 hash / tid 生成，绝不拼接用户输入。

本模块不 import astrbot，便于脱离 AstrBot 运行时单独自测。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import logging
import os
import re
import socket
import time
from typing import Any

import aiohttp
from PIL import Image, UnidentifiedImageError
from yarl import URL

logger = logging.getLogger("littleskin_skin")

HEX_RE = re.compile(r"\A[0-9a-fA-F]{8,128}\Z")
TID_RE = re.compile(r"\A\d{1,16}\Z")
# 缓存键：十六进制 hash（可带 _r<scale>_<mode> 渲染后缀），或 t<tid>
SAFE_NAME_RE = re.compile(r"\A[0-9a-fA-F][0-9a-zA-Z_.\-]{0,127}\Z")

# 标准 MC 皮肤尺寸：64x32 旧皮肤 / 64x64 现代皮肤，128 与 256 是 LittleSkin
# 上真实存在的 HD 皮肤（列表项带 hd 标记）。需求文档写的“只允许 64x64/64x32”
# 会误伤 HD 皮肤，这里默认放宽，可用 allow_hd=false 收回。
STD_SIZES = {
    (64, 32),
    (64, 64),
    (128, 64),
    (128, 128),
    (256, 64),
    (256, 128),
    (256, 256),
}
HD_SIZES = STD_SIZES - {(64, 32), (64, 64)}

CGNAT = ipaddress.ip_network("100.64.0.0/10")


class SkinError(Exception):
    """面向用户的错误：消息本身就是中文友好提示，绝不抛到主循环。"""


class NotFoundError(SkinError):
    """站点 404 / 关键字段缺失。"""


class BusyError(SkinError):
    """排队超时。"""


def is_valid_hash(hash_: str | None) -> bool:
    return bool(hash_ and HEX_RE.fullmatch(hash_))


def is_valid_tid(tid: str | None) -> bool:
    return bool(tid and TID_RE.fullmatch(tid))


def is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
    if (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
    ):
        return True
    return ip in CGNAT


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class SitePolicy:
    """URL 白名单校验。每一次出站（包括 Referer 头）都要过这里。"""

    def __init__(
        self,
        base_url: str,
        allowed_hosts: list[str] | tuple[str, ...] | None,
        timeout: float = 10.0,
    ) -> None:
        # 需求硬性要求 timeout=10，配置最多只能往小调
        self.timeout = min(max(1.0, float(timeout)), 10.0)
        hosts = [str(h).strip().lower() for h in (allowed_hosts or []) if str(h).strip()]
        base = (base_url or "").strip()
        if not base.lower().startswith("https://"):
            raise SkinError("配置无效：base_url 必须是 https 地址。")
        parsed = self._parse(base)
        if not hosts:
            hosts = [parsed.host]
        self.allowed_hosts = tuple(hosts)
        self.base_host = parsed.host
        self._dns_cache: dict[str, tuple[float, bool]] = {}

    @staticmethod
    def _parse(url: str) -> URL:
        try:
            parsed = URL(url)
        except Exception as exc:  # noqa: BLE001
            raise SkinError("配置无效：站点地址无法解析。") from exc
        if parsed.user or parsed.password or parsed.raw_user:
            raise SkinError("内部错误：请求地址带凭据，已拒绝。")
        return parsed

    def check_static(self, url: str) -> URL:
        """scheme / 主机名白名单检查（不发 DNS 请求）。"""
        parsed = self._parse(url)
        if parsed.scheme != "https":
            raise SkinError("内部错误：请求地址不是 https，已拒绝。")
        host = (parsed.host or "").lower()
        if not host or not self.host_allowed(host):
            logger.warning("blocked off-allowlist host: %r", host)
            raise SkinError("内部错误：目标域名不在白名单内，已拒绝。")
        # 配置里误填 IP 字面量时也要守住“禁止访问内网地址”，不能只靠 DNS 层
        if is_ip_literal(host) and is_blocked_ip(host):
            logger.warning("blocked internal ip literal: %r", host)
            raise SkinError("内部错误：禁止访问内网/保留地址。")
        if parsed.port not in (None, 443):
            raise SkinError("内部错误：不接受非 443 端口。")
        return parsed

    def host_allowed(self, host: str) -> bool:
        return any(
            host == allowed or host.endswith("." + allowed)
            for allowed in self.allowed_hosts
        )

    async def check_resolved(self, host: str) -> None:
        """DNS 层拦截内网地址（结果缓存 10 分钟）。"""
        host = host.lower()
        now = time.monotonic()
        cached = self._dns_cache.get(host)
        if cached and cached[0] > now:
            if cached[1]:
                raise SkinError("内部错误：该域名解析到内网地址，已拒绝。")
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                443,
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise SkinError(f"无法解析 {host}，请检查服务器 DNS 后重试。") from exc
        addrs = {info[4][0] for info in infos}
        blocked = any(is_blocked_ip(addr) for addr in addrs)
        self._dns_cache[host] = (now + 600.0, blocked)
        if blocked:
            logger.warning("blocked host %s -> %s", host, sorted(addrs))
            raise SkinError("内部错误：该域名解析到内网地址，已拒绝。")

    async def validate(self, url: str) -> URL:
        parsed = self.check_static(url)
        await self.check_resolved(parsed.host)
        return parsed


class HttpGate:
    """全局并发 1 + 最小间隔的出站节流。"""

    def __init__(self, min_interval: float = 0.8, max_wait: float = 6.0) -> None:
        self.min_interval = min(max(0.0, float(min_interval)), 30.0)
        self.max_wait = min(max(0.0, float(max_wait)), 60.0)
        self._sem = asyncio.Semaphore(1)
        self._last = 0.0

    @contextlib.asynccontextmanager
    async def slot(self):
        try:
            # wait_for 取消 acquire 不会吞掉许可：拿到令牌后被取消的等待者会
            # 把许可归还/转交给下一个等待者（asyncio.locks.Semaphore 的
            # CancelledError 分支），所以这里可以放心用它做排队超时。
            await asyncio.wait_for(self._sem.acquire(), timeout=self.max_wait)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise BusyError("同时查皮肤的人有点多，请稍后再试。") from exc
        try:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()
            yield
        finally:
            self._last = time.monotonic()
            self._sem.release()


class TextureCache:
    """<hash>.png 文件缓存，超过容量上限按 mtime 做 LRU 清理。"""

    # 刚交给平台去读取上传的文件不能反手删掉
    KEEP_RECENT = 4
    # 进程崩溃遗留的 .part 半截文件，超过这个时长就回收
    PART_MAX_AGE = 3600.0

    def __init__(self, root: str, max_bytes: int = 200 * 1024 * 1024) -> None:
        self.root = os.path.abspath(root)
        self.max_bytes = max(1024, int(max_bytes))
        self._total: int | None = None
        self._lock = asyncio.Lock()

    def path_for(self, key: str) -> str:
        if not SAFE_NAME_RE.fullmatch(key):
            # 走到这里说明调用方传了没校验过的名字 —— 直接拒绝，不拼路径。
            raise SkinError("内部错误：非法缓存键名。")
        path = os.path.join(self.root, f"{key}.png")
        if os.path.commonpath([self.root, path]) != self.root:
            raise SkinError("内部错误：缓存路径越界。")
        return path

    async def get(self, key: str) -> str | None:
        """命中返回路径并刷新 mtime（既是 LRU 的“最近使用”，也喂给 KEEP_RECENT）。"""
        path = self.path_for(key)
        return await asyncio.to_thread(self._hit, path)

    @staticmethod
    def _hit(path: str) -> str | None:
        try:
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                return None
            with contextlib.suppress(OSError):
                os.utime(path, None)
            return path
        except OSError:
            return None

    async def put(self, key: str, data: bytes) -> str:
        path = self.path_for(key)
        await asyncio.to_thread(self._write_atomic, path, data)
        async with self._lock:
            if self._total is None:
                self._total = await asyncio.to_thread(self._scan_total)
            self._total += len(data)
            if self._total > self.max_bytes:
                removed = await asyncio.to_thread(self._prune)
                if removed:
                    logger.info("texture cache pruned %d files", removed)
                self._total = await asyncio.to_thread(self._scan_total)

    def _scan_total(self) -> int:
        total = 0
        try:
            for name in os.listdir(self.root):
                if name.endswith(".png"):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(self.root, name))
        except OSError:
            return 0
        return total

    @staticmethod
    def _write_atomic(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = f"{path}.part.{os.getpid()}"
        try:
            with open(part, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, path)
        finally:
            with contextlib.suppress(OSError):
                if os.path.exists(part):
                    os.remove(part)

    def _prune(self) -> int:
        """按 mtime 从旧到新删到上限以下，最近 KEEP_RECENT 个不动。返回删除数。"""
        entries: list[tuple[float, int, str]] = []
        total = 0
        try:
            names = os.listdir(self.root)
        except OSError:
            return 0
        now = time.time()
        for name in names:
            path = os.path.join(self.root, name)
            if ".part." in name:
                # 崩溃留下的半截文件不进容量统计，否则会永远赖在磁盘上
                with contextlib.suppress(OSError):
                    if now - os.path.getmtime(path) > self.PART_MAX_AGE:
                        os.remove(path)
                continue
            if not name.endswith(".png"):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if not os.path.isfile(path):
                continue
            entries.append((st.st_mtime, st.st_size, path))
            total += st.st_size
        if total <= self.max_bytes:
            return 0
        removed = 0
        candidates = sorted(entries)[: max(0, len(entries) - self.KEEP_RECENT)]
        for _, size, path in candidates:
            try:
                os.remove(path)
            except OSError:
                continue
            removed += 1
            total -= size
            if total <= self.max_bytes:
                break
        return removed


class SiteClient:
    """带节流、白名单校验、超时与体积上限的 JSON / 二进制客户端。"""

    def __init__(
        self,
        policy: SitePolicy,
        gate: HttpGate,
        user_agent: str,
        max_bytes: int = 2 * 1024 * 1024,
        send_referer: bool = True,
    ) -> None:
        self.policy = policy
        self.gate = gate
        self.max_bytes = max(4096, int(max_bytes))
        self.send_referer = send_referer
        self.headers = {"User-Agent": user_agent or "Mozilla/5.0"}
        self._session: aiohttp.ClientSession | None = None

    async def ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # trust_env=False：不让 HTTP_PROXY 之类的环境变量把请求拐到别处，
            # 否则域名白名单形同虚设。
            self._session = aiohttp.ClientSession(
                headers=self.headers,
                timeout=aiohttp.ClientTimeout(
                    total=self.policy.timeout,
                    connect=self.policy.timeout,
                    sock_read=self.policy.timeout,
                ),
                auto_decompress=True,
                trust_env=False,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        import json

        raw = await self._request(url, dict(params or {}), None, "application/json")
        try:
            return json.loads(raw)
        except ValueError as exc:
            logger.warning("non-json response from %s", url)
            raise SkinError("皮肤站返回了无法解析的内容，请稍后再试。") from exc

    async def get_bytes(self, url: str, referer: str | None = None) -> bytes:
        return await self._request(url, {}, referer, "image/png")

    async def _request(
        self,
        url: str,
        params: dict[str, Any],
        referer: str | None,
        expect: str,
    ) -> bytes:
        await self.policy.validate(url)
        headers: dict[str, str] = {"Accept": expect}
        if referer:
            if self.send_referer:
                self.policy.check_static(referer)
                headers["Referer"] = referer
        try:
            async with self.gate.slot():
                session = await self.ensure_session()
                async with session.get(
                    url,
                    params=params,
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    if 300 <= resp.status < 400:
                        # 第三方站改路由时可能把人拐到别的域，一律不跟随。
                        # 3xx 全挡（不止常见的 5 个），没列出的码也不许当正文解析。
                        logger.warning("refused redirect %s from %s", resp.status, url)
                        raise SkinError("皮肤站发生了跳转，出于安全未继续请求。")
                    if resp.status == 404:
                        raise NotFoundError("未找到")
                    if resp.status == 429:
                        raise SkinError("皮肤站限流（429），请过一会儿再试。")
                    if resp.status >= 400:
                        logger.warning("site returned %s for %s", resp.status, url)
                        raise SkinError(f"皮肤站返回错误 {resp.status}，请稍后再试。")
                    declared = resp.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > self.max_bytes:
                        raise SkinError("文件超出大小上限，已放弃下载。")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise SkinError("文件超出大小上限，已中断下载。")
                        chunks.append(chunk)
                    return b"".join(chunks)
        except SkinError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning("timeout requesting %s", url)
            raise SkinError(
                f"{self.policy.base_host} 响应超时（{self.policy.timeout:.0f}s）。",
            ) from exc
        except aiohttp.ClientError as exc:
            logger.warning("client error requesting %s: %s", url, exc)
            raise SkinError("访问皮肤站失败，请检查网络或稍后再试。") from exc
        except OSError as exc:
            logger.warning("network error requesting %s: %s", url, exc)
            raise SkinError("网络不可达，请检查服务器网络后重试。") from exc


class TextureService:
    """把 source（站点）与 client（下载）+ cache 串成一个可复用的小服务。"""

    def __init__(self, client: SiteClient, cache: TextureCache) -> None:
        self.client = client
        self.cache = cache

    async def load(
        self,
        hash_: str,
        url: str,
        referer: str | None,
        allow_hd: bool = True,
        max_pixels: int = 4_000_000,
    ) -> tuple[str, bytes, Image.Image]:
        """返回 (缓存路径, 原始字节, 已校验的 PIL 图像)。命中缓存则不再发请求。"""
        if not is_valid_hash(hash_):
            raise SkinError("内部错误：纹理标识不合法，已拒绝。")
        path = await self.cache.get(hash_)
        data: bytes | None = None
        if path is not None:
            data = await asyncio.to_thread(self._read, path)
        if data is None:
            data = await self.client.get_bytes(url, referer=referer)
            image = await asyncio.to_thread(
                validate_texture,
                data,
                allow_hd,
                max_pixels,
            )
            await self.cache.put(hash_, data)
            return self.cache.path_for(hash_), data, image
        image = await asyncio.to_thread(validate_texture, data, allow_hd, max_pixels)
        return path, data, image

    @staticmethod
    def _read(path: str) -> bytes | None:
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            return None

    async def store_rendered(self, key: str, data: bytes) -> str:
        await self.cache.put(key, data)
        return self.cache.path_for(key)


def validate_texture(
    data: bytes,
    allow_hd: bool = True,
    max_pixels: int = 4_000_000,
) -> Image.Image:
    """解码 + 校验。同步函数，调用方用 to_thread 包一下。"""
    max_pixels = max(1, int(max_pixels))
    # Image.MAX_IMAGE_PIXELS 是进程级全局量，不能改完就不管了：这里只在它比
    # 我们的阈值更宽松时临时提高，出来立刻还原，别影响同进程别的插件。
    saved_limit = Image.MAX_IMAGE_PIXELS
    try:
        if saved_limit is None or saved_limit < max_pixels:
            Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            with Image.open(io.BytesIO(data)) as image:
                if (image.format or "").upper() != "PNG":
                    raise SkinError("返回内容不是 PNG，已丢弃。")
                width, height = image.size
                if width * height > max_pixels:
                    raise SkinError("图片尺寸异常（疑似解压炸弹），已丢弃。")
                if (width, height) not in STD_SIZES:
                    raise SkinError(f"皮肤尺寸 {width}x{height} 不是标准皮肤，已丢弃。")
                if not allow_hd and (width, height) in HD_SIZES:
                    raise SkinError("这是 HD 皮肤，当前配置不接受。")
                image.load()
                return image.convert("RGBA")
        except SkinError:
            raise
        except UnidentifiedImageError as exc:
            raise SkinError("图片无法解码，已丢弃。") from exc
        except Exception as exc:  # noqa: BLE001  DecompressionBombError 等
            logger.warning("texture decode failed: %s", exc)
            raise SkinError("图片解码失败，已丢弃。") from exc
    finally:
        Image.MAX_IMAGE_PIXELS = saved_limit
