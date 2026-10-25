# littleskin_skin — LittleSkin / Blessing Skin 皮肤查询（AstrBot 插件）

在 QQ 群里用 `/skin` 搜索 Minecraft 皮肤、看 **3D 预览**、拿**可直接下载的皮肤文件（PNG）**，
也可以直接说人话（自然语言）让机器人自己搜。

只读查询：**不登录、不带 cookie、不批量遍历、不做镜像站、不代理站点内容**；出站只允许配置里写明的
皮肤站域名，并带全局串行限流。

```
/skin 自由高达
  → 搜索「自由高达」得到 1 条（最多显示 5 条）：
    1. 自由高达 · tid 26051 · steve · 赞 88 · <上传者>

/skin get 26051
  → 第一条：3D 预览图（正面 / 斜 30° / 背面）
  → 第二条：自由高达_26051.png（64×64，文件附件，可直接导入）

「给我看看自由高达的皮肤」          # 自然语言，走 LLM 函数工具
  → 模型自己调 littleskin_search → littleskin_skin_image → 发图 + 发文件
```

## 特性

- **搜索 / 详情 / 下载 / 随机**一条龙，网站在配置里换，代码不用动。
- **3D 预览**：纯 PIL 写的正交投影软件渲染器，不引入 numpy / OpenGL / three.js / 无头浏览器，
  也不产生任何额外出站请求（64×64 三视角约 13ms，输出约 22KB）。
- **渲染图与皮肤文件分开发**：图是图，文件是文件（`.png` 文件附件，字节完整，能直接导入）。
- **多站点并存**：`sites` 一行一个站，每站独立白名单 + 独立限流，命令末尾 `-s 别名` 切换。
- **自然语言可用**：注册 3 个 LLM 函数工具（搜索 / 列站 / 发图），模型自己会调。
- **陡峭的边界**：只允许 https + 域名白名单 + 端口 443，拒绝跳转、拒绝内网/保留地址，
  纹理解码前做尺寸白名单，输出图有像素预算（1.7 GiB 内存的小机器也能跑）。
- **可测**：64 项离线用例（本地 mock 站，不需要 AstrBot 运行时、不联网）。

## 安装

1. 把整个目录放进 AstrBot 的 `data/plugins/`（目录名随意，插件名以 `metadata.yaml` 的 `name` 为准）：

   ```
   data/plugins/littleskin_skin/
     metadata.yaml
     _conf_schema.json
     main.py          # 命令/工具注册与编排
     sources.py       # 站点适配器（多站点解析、字段映射在这里）
     textures.py      # 下载 / 校验 / 缓存 / 限流 / SSRF 防护
     preview3d.py     # 纯 PIL 的玩家模型 3D 渲染器
     render.py        # 放大、正面展开、拼图
     tools/probe_site.py     # 接新站前先跑它：搜索→详情→纹理→3D→随机
     tests/selftest.py
   ```

2. 依赖：`aiohttp`、`pillow`、`yarl`（AstrBot 通常自带；缺 `yarl` 就 `pip install yarl`）。
   不使用 requests / urllib / 浏览器，也没有新增第三方渲染库。

3. 重启或重载插件。配置项由 `_conf_schema.json` 自动生成，可在 WebUI 插件设置里改，
   落盘在 `data/config/littleskin_skin_config.json`；默认值开箱即用，**模板里不含任何密钥**。

缓存目录默认在 `data/plugin_data/littleskin_skin/cache/`（刻意放在插件目录之外——插件升级会覆盖插件目录），
可用配置 `cache_dir` 改到别处。

> 实测环境：AstrBot **4.29.1**（`metadata.yaml` 声明 `>=4.29.0`）。更早的版本缺少
> `@filter.llm_tool` 的 docstring 参数解析 / `File` 消息段 / `file_token_service` 等 API。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/skin <关键词>` | 搜索，最多显示 `max_results`（默认 5）条：名称 · tid · 类型 · 点赞 · 上传者 |
| `/skin search <关键词>` | 同上（显式写法） |
| `/skin get <tid>` | 3D 预览图（一张）+ 皮肤文件（一条独立文件消息） |
| `/skin view <tid>` | 只发 3D 预览（别名 `/skin 3d`、`/skin preview`） |
| `/skin raw <tid>` | 只发皮肤文件，不附图 |
| `/skin rand` | 随机一张公开皮肤 |
| `/skin sites` | 列出已配置的皮肤站与默认站 |
| `/skin` | 打印命令树 |

所有命令都可以在末尾加 `-s 别名` 指定站点（只有一个站时不必写）。

## 多站点

在插件配置的 `sites` 里一行一个站（留空则退回下面的单站点老配置 `site` / `base_url` / `allowed_hosts`）：

```
ls|littleskin|https://littleskin.cn|littleskin.cn
bs|blessing_skin|https://skin.pueh.cn|skin.pueh.cn
```

一行是 `别名|适配器|站点根|白名单[,白名单]|可选覆盖项`：

| 字段 | 说明 |
| --- | --- |
| 别名 | 小写字母/数字/`-`/`_`，1~16 字符；命令里用 `-s 别名` 引用 |
| 适配器 | `littleskin` 或 `blessing_skin`（后者是通用 Blessing Skin 实现，路由/字段全可配） |
| 站点根 | 必须 `https`（本机回环地址测试除外） |
| 白名单 | **这一个站**的出站域名白名单；不写就从站点根取 hostname |
| 可选覆盖项 | `search_path=`、`detail_path=`、`texture_path=`、`show_path=`、`list_key=`、`field_*=`、参数名等，不写就继承顶层同名配置 |

- 每站有自己的 `SitePolicy`（白名单）和 `HttpGate`（限流闸口），一个站被限流不影响另一个站；
- `default_site` 指定默认站（留空 = 第一行）；
- 配置写坏了不会拖死插件：非法行跳过并打 `WARNING`，全部非法时自动退回单站点配置。

### 已实测站点（2026-10-25）

| 站点 | 适配器 | 需要改什么 | 实测结果 |
| --- | --- | --- | --- |
| `littleskin.cn` | `littleskin` | 不用改（默认值就是它） | 搜索 / 详情 / 纹理 / 3D / 随机 全通 |
| `skin.pueh.cn` | `blessing_skin` | 只填 `sites` 一行 | 同上，5/5 步全通 |
| `skin.fairingstudio.com` | `blessing_skin` | 只填 `sites` 一行 | 同上，5/5 步全通 |

> 结论：**同构的 Blessing Skin 站确实只需要一行配置**。但也别假定所有站都一样——见下节。
>
> 想接的站不在这张表里？先跑一遍 `python tools/probe_site.py https://站点 域名 关键词`，
> 它用插件自己的网络层走完搜索→详情→纹理→3D→随机五步，哪一步挂就看下面那节改哪个配置。

### 字段名不一样怎么办（`field_*` 映射）

不是每个 Blessing Skin 魔改版都把字段叫 `tid` / `name` / `type` / `hash`。这些全部可配，**不用写代码**：

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `list_key` | `data` | 列表在响应里的位置，支持点号路径（`data.items`）；留空 = 响应本身就是数组 |
| `field_tid` / `field_name` / `field_type` | `tid` / `name` / `type` | 主键、皮肤名、模型 |
| `field_hash` | `hash` | 纹理 hash（模板要用 `{hash}` 时才必需） |
| `field_likes` / `field_uploader` / `field_uploader_fallback` | `likes` / `nickname` / `uploader` | 点赞、上传者（主选为空时用 fallback） |
| `field_size` / `field_upload_at` / `field_hd` / `field_public` | 同名 | 其余元数据 |
| `field_texture_url` | 空 | 站点**直接返回完整纹理地址**时填这个字段名，优先于 `texture_path` 拼装 |
| `texture_path` | `/textures/{hash}` | 模板可用 `{hash}` **或 `{tid}`**，也可以写成完整 URL（`https://cdn.example.com/{hash}`） |

例：一个把列表包在 `items` 里、字段叫 `id`/`title`/`model`/`author`、纹理按 tid 给的站：

```
x|blessing_skin|https://skin.example.com|skin.example.com|search_path=/api/search|search_keyword_param=q|detail_path=/api/skin/{tid}|texture_path=/tex/{tid}|list_key=items|field_tid=id|field_name=title|field_type=model|field_likes=likes|field_uploader=author
```

仍然搞不定的情况（需要子类化）：站点用 POST / 需要 cookie 或 token / 列表和详情结构差异大到不能靠重命名解决。
「不带 cookie」是刻意设计，不为接站而妥协。

## 渲染图与皮肤文件是分开的

| 产物 | 内容 | 控制项 |
| --- | --- | --- |
| 渲染图 | 默认只有 **3D 预览**（视角由 `views_3d` 决定） | `preview_3d` / `views_3d` |
| 皮肤文件 | 原始 64×64（或 HD）PNG，**文件附件** | `skin_file_mode` |

- `skin_file_mode`：`file`（默认）/ `image`（当图片发）/ `both`（文件 + 图片）/ `none`（不发）。
- 想要「放大纹理表 + 正面展开 + 3D」三段拼图：把 `include_sheet` 和 `flatten` 都设为 `true`。
  那段「正面展开」是把头/身/左右臂/左右腿的正面裁成小方块横排，只为看配色，不是可用文件。
- 文件发送有三道降级：**`upload_group_file` / `upload_private_file`（aiocqhttp/NapCat）→ AstrBot `File`
  消息段 → 图片**，任何一道成功就停；全失败也会在回复里说明「已改发原图」，不会静默丢消息。
- 文件下载地址前缀：插件配置 `file_url_base` 优先，否则用 AstrBot 全局 `callback_api_base`。
  **NapCat 与 AstrBot 不在同一台机器时**（例如 NapCat 在本机、AstrBot 在云服务器），
  这个前缀必须填 NapCat 能访问到的地址（内网 IP / 反向 SSH 隧道），否则会退回图片发送。

## LLM 函数工具（自然语言）

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `littleskin_search` | `keyword`, `site` | 返回**只有皮肤名**的清单，让模型挑一张 |
| `littleskin_sites` | — | 列出站点别名与默认站 |
| `littleskin_skin_image` | `name`, `site` | 名字或 tid → 渲染 + 下载 → 直接发图并发皮肤文件 |

设计取舍：

- **搜索不给 tid / 赞 / 上传者**：名字够模型用，少给字段就少一份幻觉素材。
- **图片不走工具返回值**：返回值会进模型上下文，图片用 `MessageChain` 直发。
- **工具冷却独立**（`tool:` 命名空间，1 秒上限）：用户刚发过 `/skin`，紧接着让模型发一张不该被自己挡住。
- **失败一律回中文短句**（搜不到 / 无纹理 / 下载失败），让模型复述，不抛异常给主循环。
- 工具描述里写死了使用场景（「用户想找/想换某个 MC 皮肤时」），避免模型拿它回答无关问题。

## 3D 预览是怎么来的

`preview3d.py` 按原版 `ModelPlayer` 的 6 个长方体（头 / 身 / 左右臂 / 左右腿）加 6 个外扩 0.5 格的
覆盖层（帽子 / 外套 / 袖子 / 裤腿）建模，逐面做最近邻仿射纹理映射，按面心深度做画家算法叠加，
亮度沿用游戏里的轴向着色（顶 1.0 / 正面 0.8 / 侧面 0.6 / 底 0.5）。展开格由 `face_rects()` 一处算出，
2D 展开与 3D 预览共用同一份模型定义，不会各说各话。

- `views_3d` 默认 `0, 30, 180`（正面、斜 30°、背面），最多 4 个。
- `skin_model=auto` 按站点返回的 `type` 字段判定（实测就是 `steve` / `alex`）；alex 手臂宽 3 像素，
  判错会错位半格，可用 `skin_model` 手动纠正。
- 64×32 旧表没有左半边，左臂/左腿按游戏规则水平镜像复用右侧展开，且不叠覆盖层。

## 主要配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `sites` | `[]` | 多站点清单（见上）；留空则用单站点配置 |
| `default_site` | 空 | 默认站别名（空 = 清单第一个） |
| `list_key` / `field_*` / `field_texture_url` | 见 schema | 响应字段名映射（接非标站点用） |
| `site` / `base_url` | `littleskin` / `https://littleskin.cn` | 单站点模式的适配器与站点 |
| `allowed_hosts` | `["littleskin.cn"]` | 单站点模式的出站白名单 |
| `skin_file_mode` | `file` | `file` / `image` / `both` / `none` |
| `file_url_base` | 空 | 皮肤文件下载前缀；空 = 用 AstrBot 全局 `callback_api_base` |
| `include_sheet` | false | 渲染图是否带放大纹理表 |
| `flatten` | false | 渲染图是否带正面展开小条 |
| `preview_3d` | true | 渲染图是否带 3D 预览（`/skin view` 不受影响） |
| `views_3d` | `0, 30, 180` | 3D 视角（度，逗号分隔，最多 4 个） |
| `zoom` | 10 | 最近邻放大倍数（1~32，受像素预算自动压低） |
| `skin_model` | `auto` | `auto` / `steve` / `alex` |
| `allow_hd` | true | 是否接受 HD 皮肤（128×128 等） |
| `search_path` / `detail_path` / `texture_path` / `show_path` | 见 schema | 端点路径模板（也是多站点的默认值） |
| `search_keyword_param` / `search_page_param` / `search_random_param` / `search_random_value` | `keyword` / `page` / `order` / `random` | 参数名与随机排序写法 |
| `timeout` | 10 | 单次请求超时（硬性上限 10 秒） |
| `min_interval` | 0.8 | 全局并发 1 + 两次出站最小间隔 |
| `max_queue_wait` | 6.0 | 排队上限，超过就回「稍后再试」 |
| `user_cooldown` | 3.0 | 同会话指令冷却（LLM 工具侧自动压到 1 秒） |
| `max_results` | 5 | 搜索展示条数 |
| `case_fallback` | true | 纯 ASCII 小写关键词查空时再试大写/首字母大写 |
| `cache_max_mb` | 200 | 缓存上限（按 mtime LRU 清理） |
| `max_texture_bytes` | 2097152 | 单张纹理下载体积上限 |
| `max_image_pixels` | 4000000 | 像素预算：纹理解码上限，也是输出图上限 |

## 安全设计

- **全异步**：`aiohttp` + `asyncio.to_thread`（PIL/磁盘），任何路径都不阻塞事件循环。
- **SSRF**：只允许 `https`；只允许各站白名单（含子域）；端口必须 443；拒绝 URL 内嵌凭据；
  IP 字面量按内网/保留/CGNAT/组播/链路本地拦截（即使被写进白名单也一样）；解析后再 `getaddrinfo` 复查
  （缓存 10 分钟）；`allow_redirects=False`，**任何 3xx 都拒绝**；`trust_env=False` 不吃环境代理。
- **纹理校验**：解码后必须是 64×64 / 64×32（`allow_hd` 时再放行 128/256 系列），否则丢弃；
  `Image.MAX_IMAGE_PIXELS` 只在解码期间临时提高并立刻复原，不影响同进程其它插件。
- **缓存**：写入用 `.part.<pid>` → `fsync` → `os.replace`；崩溃遗留的旧 `.part` 由 LRU 顺手回收；
  命中刷新 mtime；LRU 永不删最近 4 个文件（避免刚发出还没读完就被 unlink）；
  键名只允许 hash/tid 派生的 `[0-9a-fA-F][0-9a-zA-Z_.-]{0,127}`，落盘前再做 `commonpath` 越界校验。
- **第三方文本**（皮肤名、上传者）：剔除控制字符、零宽字符、双向覆盖符，压缩空白，截断 40 字符，
  回复关闭 markdown 渲染；LLM 工具只把清洗 + 截断后的名字交给模型，站点原始 JSON 不进上下文。
- **不猜类型**：站点返回的字段全部 `dict.get()`，缺字段降级成「未找到」，不抛异常。

## 已知限制

- 3D 预览是**静态正交投影**：只有 6 个长方体，没有关节弯曲/动画，也不做真正的深度缓冲
  （按面心深度做画家算法叠加，个别互相穿插的面排序可能不理想）。
- 披风 / 盔甲在 Blessing Skin 系里是另一类资源；若其纹理尺寸恰好落在白名单里，会被当玩家皮肤渲染。
- 搜索**区分大小写**（实测同一站的 `GUNDAM` / `gundam` / `Gundam` 结果条数不同），已加 `case_fallback`
  兜大部分情况，但仍是站点行为，不保证。
- 只取第 1 页，没有分页命令；不支持上传 / 改名 / 删除 / 登录态皮肤。
- `/skin rand` 依赖站点的 `order=random`；站点不支持时退化为第一页随机取一条。
- HD（128×128 等）皮肤按 `width/64` 等比取格，非等比畸形表会错位。

## 自测

```bash
python tests/selftest.py          # 离线：本地 mock 站，67 项（含三个字段风格不同的站点 mock）
python tests/selftest.py --live   # 额外对 littleskin.cn 做只读冒烟（共 71 项）
```

离线用例不依赖 AstrBot 运行时（内置 astrbot 模块桩），全部用例共用一个事件循环；
覆盖内容包括多条命令路径、限流/超时/断网、SSRF 拦截、纹理与输出图尺寸、缓存 LRU、
3D 六个面逐一比对、多站点路由与逐站白名单、字段名映射/完整纹理 URL、LLM 工具注册与冷却。

## 适配新站点

`sources.py` 里 `BlessingSkinSource` 是通用实现，`LittleSkinSource` 只是注册名 + 默认参数。
接一个新站**先试配置**：`sites` 一行 + 必要时 `field_*` / `list_key` / `texture_path` 覆盖。

如果那个站返回结构差异大到配置调不平（或者要 POST / cookie / token），才需要子类化
`BlessingSkinSource` 覆写 `parse_list()` / `detail()` / `search()`，在 `SOURCES` 里注册，
并在 `_conf_schema.json` 的 `site` 说明里补上名字。

## 免责声明

- 本项目只做**只读**查询，不存储、不二次分发站点内容；请遵守所用皮肤站的服务条款与 robots 约定。
- 请保持默认限流（全局串行 + `min_interval`），不要改成高频抓取——被封 IP 是自己站点账号/网络的问题。
- 与 LittleSkin / Blessing Skin 项目及任何皮肤站均无隶属关系。
