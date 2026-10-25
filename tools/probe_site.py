#!/usr/bin/env python3
"""probe_site.py —— 接新皮肤站之前，先用它把目标站读一遍。

用插件自己的网络层（`sources` / `textures` / `render`），不经过 AstrBot、不写任何配置，
只做只读请求，依次验证：搜索 → 详情 → 纹理下载 → 3D 渲染 → 随机。

用法（在插件目录里）：

    python tools/probe_site.py https://skin.example.com skin.example.com 关键词

输出示例：

    [search] '自由' -> 1 条
       tid=105 name=42奶奶的自由失效 type=alex likes=1 uploader=AYAOYA hd=None
    [detail] tid=105 name=... hash=d4d31401710e… size=3
    [texture] https://skin.pueh.cn/textures/d4d314…
    [download] d4d314….png (64, 64) 2216B
    [render3d] (1000, 1112) -> /tmp/skin-probe-xxx/preview.png (17272B)
    [rand] tid=140

    通过 5/5 步

哪一步挂了，就说明该站需要不同的配置（`search_path` / `detail_path` / `texture_path` /
`field_*` / `list_key` 等，见 README「字段名不一样怎么办」）；五步全过，说明 `sites` 里
填一行就能用。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import types

_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))  # 让 `import littleskin_skin` 成立

# 仓库目录名（astrbot_plugin_littleskin_skin）与包名（littleskin_skin）不一致时，
# 用一个只有 __path__ 的空包把导入接上（AstrBot 实际加载按目录名，不影响运行）。
if os.path.basename(_PLUGIN_DIR) != "littleskin_skin" and "littleskin_skin" not in sys.modules:
    _pkg = types.ModuleType("littleskin_skin")
    _pkg.__path__ = [_PLUGIN_DIR]  # type: ignore[attr-defined]
    sys.modules["littleskin_skin"] = _pkg

from littleskin_skin.render import render_preview  # noqa: E402
from littleskin_skin.sources import build_source, parse_sites  # noqa: E402
from littleskin_skin.textures import (  # noqa: E402
    HttpGate,
    SiteClient,
    SitePolicy,
    TextureCache,
    TextureService,
)

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://skin.pueh.cn"
HOST = sys.argv[2] if len(sys.argv) > 2 else "skin.pueh.cn"
KEYWORD = sys.argv[3] if len(sys.argv) > 3 else "自由"


async def main() -> int:
    specs, problems = parse_sites({"sites": [f"t|blessing_skin|{BASE}|{HOST}"]})
    for problem in problems:
        print("config problem:", problem)
    spec = specs[0]
    source = build_source(spec)

    cache_dir = tempfile.mkdtemp(prefix="skin-probe-")
    cache = TextureCache(cache_dir, 50 * 1024 * 1024)
    client = SiteClient(
        policy=SitePolicy(spec.config.base_url, list(spec.allowed_hosts), 10),
        gate=HttpGate(min_interval=0.8, max_wait=10),
        user_agent="Mozilla/5.0 skin-probe",
        max_bytes=2 * 1024 * 1024,
        send_referer=True,
    )
    service = TextureService(client, cache)
    ok = 0
    try:
        items = await source.search(client, KEYWORD, page=1)
        print(f"[search] {KEYWORD!r} -> {len(items)} 条")
        for item in items[:3]:
            print(
                f"   tid={item.tid} name={item.name} type={item.kind} "
                f"likes={item.likes} uploader={item.uploader} hd={item.hd}"
            )
        if not items:
            print("（搜索没结果，换个关键词再试；也可能是 search_path/参数名不对）")
            return 1
        ok += 1

        tid = str(items[0].tid)
        detail = await source.detail(client, tid)
        print(
            f"[detail] tid={detail.tid} name={detail.name} type={detail.kind} "
            f"hash={detail.hash[:12]}… size={detail.size}"
        )
        if not detail.tid:
            print("（详情解析失败：检查 detail_path 与 field_*）")
            return 2
        ok += 1

        url = source.texture_url(detail)
        print(f"[texture] {url or '（拼不出地址：检查 texture_path / field_hash / field_texture_url）'}")
        if not url:
            return 3
        # 没有 hash 的站（按 tid 取图）用 URL 摘要当缓存键，与插件主流程一致
        key_id = detail.hash or hashlib.sha256(url.encode()).hexdigest()
        path, _, image = await service.load(
            hash_=key_id,
            url=url,
            referer=source.page_url(tid),
            allow_hd=True,
            max_pixels=4_000_000,
        )
        print(f"[download] {os.path.basename(path)} {image.size} {os.path.getsize(path)}B")
        ok += 1

        slim = "alex" in (detail.kind or "").lower()
        out = render_preview(
            image, zoom=8, flatten=False, three_d=True, slim=slim, views=(0, 30, 180)
        )
        out_path = os.path.join(cache_dir, "preview.png")
        out.save(out_path)
        print(f"[render3d] {out.size} -> {out_path} ({os.path.getsize(out_path)}B)")
        ok += 1

        rand = await source.random_item(client)
        print(f"[rand] {'tid=' + str(rand.tid) if rand else '空（站点可能不支持 order=random）'}")
        ok += 1 if rand else 0
    finally:
        await client.close()
    print(f"\n通过 {ok}/5 步")
    return 0 if ok == 5 else 4


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
