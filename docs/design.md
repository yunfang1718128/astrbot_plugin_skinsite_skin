# 实现与安全设计

> 给改代码的人看。日常使用看 README，接新站看 [stations.md](stations.md)。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | AstrBot 侧的一切：命令组、裸关键词 `CustomFilter`、3 个 LLM 工具、站点选择、消息组装 |
| `sources.py` | 站点适配器与多站点解析（`SiteSpec` / `FieldMap` / `parse_sites`），不 import astrbot |
| `textures.py` | HTTP 客户端（SSRF 白名单、限流闸口）、纹理校验、磁盘缓存（LRU），不 import astrbot |
| `preview3d.py` | 纯 PIL 的玩家模型 3D 渲染器 |
| `render.py` | 放大、正面展开、拼图 |
| `tools/probe_site.py` | 接新站体检（五步） |
| `tests/selftest.py` | 67 项离线 + 4 项 live 冒烟，内置 astrbot 模块桩 |

`sources` / `textures` / `render` / `preview3d` 刻意不依赖 astrbot，所以能脱离运行时自测。

## 两种出口，一套业务层

| 出口 | 触发 | 说明 |
| --- | --- | --- |
| `/skin …` 指令组 | 用户手打 | 零 LLM 开销 |
| 裸 `/skin <关键词>` | `CustomFilter`（`SkinKeywordFilter`） | 命令组不接受任意位置参数，这是 AstrBot 的限制 |
| LLM 工具 | 模型自己调 | `skinsite_search` / `skinsite_sites` / `skinsite_skin_image` |

指令和工具都走 `_render_skin()`，改下载/渲染只有一处需要改。

> `@filter.custom_filter(...)` 收的是**类**，不是实例（4.29.1 会内部再 `cls(raise_error)` 实例化一次）。
> 传实例 → `TypeError: 'X' object is not callable`，插件整个加载失败。

## 消息怎么发

`/skin get <tid>` 发两条：

1. **渲染图**：`event.send(_image(...))`，默认只有 3D（`include_sheet` / `flatten` 可在渲染图里加回纹理表与正面展开）；
2. **皮肤文件**：`_send_skin_file()`，三级降级：

```
upload_group_file / upload_private_file（aiocqhttp/NapCat，file 传 token URL）
  → AstrBot File 消息段（File(name=..., url=...)，兼容其它平台）
    → 图片（Image.fromFileSystem）
```

三道全挂也会在回复里说明「已改发原图」，不会静默丢消息。

文件地址 = `{file_url_base or 全局 callback_api_base}/api/file/{token}`，token 由
`astrbot.core.file_token_service.register_file()` 签发（一次性，10 分钟）。

- **同机部署**：`callback_api_base` 留空即可（AstrBot 自己就是那个地址）。
- **NapCat 与 AstrBot 异机**：必须填 NapCat 能访问到的地址（内网 IP / 反向隧道），否则 NapCat 下载不到 → 退化成图片。

## 3D 渲染器

`preview3d.py` 按原版 `ModelPlayer` 的 6 个长方体（头 / 身 / 左右臂 / 左右腿）+ 6 个外扩 0.5 格的覆盖层
（帽子 / 外套 / 袖子 / 裤腿）建模，逐面做最近邻仿射纹理映射，按面心深度做画家算法叠加，
亮度沿用游戏里的轴向着色（顶 1.0 / 正面 0.8 / 侧面 0.6 / 底 0.5）。

- 展开格由 `face_rects()` 一处算出：2D 展开与 3D 预览共用同一份模型定义。
- `skin_model=auto` 按站点 `type` 字段判定 steve / alex；alex 手臂宽 3 像素，判错会错位半格。
- 64×32 旧表没有左半边，左臂/左腿按游戏规则水平镜像复用右侧展开，且不叠覆盖层。
- 成本：64×64 三视角约 13ms（`asyncio.to_thread` 里跑），输出约 22KB PNG。
- 静态正交投影：无关节、无动画、无深度缓冲（个别互相穿插的面排序可能不理想）。

## 安全边界

- **SSRF**：只允许 `https`；只允许各站白名单（含子域）；端口必须 443；拒绝 URL 内嵌凭据；
  IP 字面量按内网 / 保留 / CGNAT(100.64/10) / 组播 / 链路本地拦截（即使写进白名单也一样）；
  解析后 `getaddrinfo` 复查（缓存 10 分钟）；`allow_redirects=False`，**任何 3xx 都拒绝**；
  `trust_env=False` 不吃环境代理。
- **限流**：每站一个 `HttpGate`（`Semaphore(1)` + `min_interval`，默认 0.8s）+
  `max_queue_wait` 排队上限 + 每个会话 `user_cooldown`（LLM 工具侧压到 1 秒，命名空间分开算）。
- **纹理校验**：解码后必须是 64×32 / 64×64（`allow_hd` 再放行 128/256 系列），否则丢弃；
  `Image.MAX_IMAGE_PIXELS` 只在解码期间临时提高并立刻复原（不影响同进程其它插件）。
- **缓存**：`.part.<pid>` → `fsync` → `os.replace` 原子落盘；崩溃遗留的旧 `.part` 由 LRU 回收；
  命中刷新 mtime；LRU 永不删最近 4 个文件（避免刚发出还没读完就被 unlink）；
  键名只允许 `[0-9a-fA-F][0-9a-zA-Z_.-]{0,127}`，落盘前再做 `commonpath` 越界校验。
  没有 hash 的站用 `sha256(url)` 当缓存键。
- **第三方文本**：皮肤名/上传者剔除控制字符、零宽字符、双向覆盖符，压缩空白，截断 40 字符；
  回复关闭 markdown；LLM 工具只把清洗+截断后的名字交给模型，站点原始 JSON 不进上下文。
- **输出图**：受 `max_image_pixels` 像素预算约束，HD 表 × 大倍数会自动降倍数，1.7 GiB 内存的机器也撑得住。

## 测试

```bash
python tests/selftest.py          # 67 项，离线，本地 mock 站（三种字段风格）
python tests/selftest.py --live   # 追加 4 项对 littleskin.cn 的只读冒烟
```

离线用例不依赖 AstrBot：`install_astrbot_stub()` 会造一套桩（含 `filter.command_group` /
`CustomFilter` / `llm_tool` 的 docstring 解析 / `File` 段 / `file_token_service`），
**桩的语义与真实实现一致**——例如 llm 工具参数缺 `name(string):` 类型注释就 `ValueError`，
桩也照样报错，否则等于没测。

覆盖点（挑要紧的）：多站点路由与逐站白名单、字段名映射、完整纹理 URL、`/skin get` 两条消息、
`skin_file_mode` 三种模式、OneBot 文件上传失败回退、SSRF 拦截（含冷门 3xx）、
超时/断网/排队、纹理与输出尺寸、缓存 LRU 与 `.part` 回收、3D 六个面逐一比对、工具冷却。
