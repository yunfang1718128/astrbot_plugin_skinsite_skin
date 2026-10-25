# 皮肤站皮肤查询 — 在群里查 Minecraft 皮肤

一个 AstrBot 插件。在群里发一句，机器人去你配置的皮肤站（默认 `littleskin.cn`）搜，然后把结果发回来：

1. **3D 预览图**（正面 / 斜 30° / 背面）
2. **皮肤文件**（原始 64×64 PNG，可以直接下载导入游戏或皮肤站）

也可以直接使用自然语言命令，模型会自己搜索、自动发送。

## 它能做什么

- 搜索皮肤、看 3D 预览、下载原始皮肤文件、随机给出一张皮肤
- 同时接入**多个皮肤站**，命令后面加 `-s 站名` 就换站
- 自然语言可用（对 AI 说人话就行，不用记指令）
- 不登录、不带 cookie、不批量抓；只允许访问你配置的站点域名，自带限流

## 安装

1. 把这个目录放进 AstrBot 的 `data/plugins/`，或者直接在插件市场搜索安装
2. 依赖：`aiohttp`、`pillow`、`yarl`（仓库里有 `requirements.txt`，市场安装会自动装）
3. 重启或重载插件 —— **默认配置开箱即用**，默认连 `littleskin.cn`

## 怎么用

在群里发（`/` 是唤醒前缀，按你的机器人设置来）：

| 发什么                | 得到什么                             |
| ------------------ | -------------------------------- |
| `/skin 自由高达`       | 搜索结果列表（名称 / tid / 模型 / 点赞 / 上传者） |
| `/skin get 26051`  | 3D 预览图 + 皮肤文件，两条消息               |
| `/skin view 26051` | 只要 3D 预览图                        |
| `/skin raw 26051`  | 只要皮肤文件                           |
| `/skin rand`       | 随机来一张                            |
| `/skin sites`      | 看当前配了哪些站                         |
| `/skin`            | 看全部指令                            |

搜到 tid 之后才能 `get`，所以一般是「搜一次 → 挑一个 → 取图」。

或者直接使用自然语言命令。

## 换站 / 接自己的皮肤站

**兼容性全在配置里，不用改代码。** 两个地方：

1. **加站**：插件配置的 `sites`，一行一个站：
   
   ```
   别名|适配器|站点根|域名白名单
   ```
   
   例如同时挂三个站：
   
   ```
   ls|littleskin|https://littleskin.cn|littleskin.cn
   pueh|blessing_skin|https://skin.pueh.cn|skin.pueh.cn
   my|blessing_skin|https://skin.example.com|skin.example.com
   ```
   
   存好之后：`/skin sites` 能看到，`/skin 自由高达 -s pueh` 就查 pueh 那个站；
   不带 `-s` 时用默认站（`default_site`，留空 = 第一行）。

2. **站点的字段名不一样**：大多数 Blessing Skin 站什么都不用改；如果那个站返回的字段叫
   `id` / `title` / `model` 之类，再加几个配置项就行（`field_tid`、`field_name`、`field_hash`、
   `list_key`…）。完整清单和新站体检方法见 **[docs/stations.md](docs/stations.md)**。

配置在哪改：WebUI 面板 → 插件设置 → `astrbot_plugin_skinsite_skin`；或者直接编辑
`data/config/astrbot_plugin_skinsite_skin_config.json`。

> 接新站之前，先跑一遍体检脚本（只读，不会动任何数据）：
> 
> ```bash
> python tools/probe_site.py https://skin.example.com skin.example.com 关键词
> ```
> 
> 它会依次验证 搜索 → 详情 → 纹理下载 → 3D 渲染 → 随机，哪一步挂了就说明该补哪个配置。

## 常改的配置

| 配置                          | 默认                  | 说明                                                            |
| --------------------------- | ------------------- | ------------------------------------------------------------- |
| `sites` / `default_site`    | 空                   | 多站点清单 / 默认站                                                   |
| `preview_3d` / `views_3d`   | true / `0, 30, 180` | 要不要 3D、看哪几个角度                                                 |
| `skin_file_mode`            | `file`              | 皮肤文件怎么发：`file`（文件附件）/ `image` / `both` / `none`               |
| `file_url_base`             | 空                   | 皮肤文件下载地址前缀；**NapCat 和 AstrBot 不在同一台机器时必须填**（填 NapCat 能访问到的地址） |
| `include_sheet` / `flatten` | false / false       | 想在渲染图里额外加「放大纹理表」「正面展开小条」就打开                                   |
| `max_results`               | 5                   | 搜索结果最多显示几条                                                    |
| `user_cooldown`             | 3                   | 同一个人两次指令的最小间隔（秒）                                              |
| `allow_hd`                  | true                | 是否接受 128×128 等 HD 皮肤                                          |

其余配置（超时、限流、缓存、像素预算等）一般不用动，schema 里都有说明。

## 出问题怎么办

| 现象          | 先看这里                                                                             |
| ----------- | -------------------------------------------------------------------------------- |
| 搜不到东西       | 站点搜索区分大小写，换个写法；或 `/skin sites` 确认查的是哪个站                                          |
| 只收到图、没收到文件  | 文件服务没配好：看 `file_url_base` / AstrBot 全局 `callback_api_base`（异机部署要填 NapCat 能访问的地址） |
| 想确认是不是插件的问题 | `python tests/selftest.py`（离线自测，67 项）                                            |
| 想接新站        | `python tools/probe_site.py …` 体检                                                |
| 看日志         | `journalctl -u astrbot                                                           |

## 已知限制

- 3D 预览是静态正交投影（6 个方块拼的人形），不是游戏内预览，也不能转。
- 只取搜索结果第 1 页，没有分页。
- 不支持上传、改名、删除、登录态皮肤；不碰需要登录/cookie 的站。
- 披风、盔甲这类非玩家皮肤资源会被当成皮肤渲染（尺寸对得上时）。
- **皮肤文件是文件附件**：QQ（aiocqhttp）下是群/私聊文件；不支持文件段的平台（如 WebChat）会自动退化成发图片，不会报错。

实现细节（3D 怎么画的、安全边界、文件发送降级策略）见 **[docs/design.md](docs/design.md)**。

## 许可

MIT。本项目与 LittleSkin / Blessing Skin 项目及任何皮肤站均无隶属关系。

请遵守所用皮肤站的条款，**不要调高默认限流**——被封 IP 是你自己的事，另外本插件仅仅是帮助你查询下载公开皮肤站内容，请尊重创作者的版权和皮肤站管理员的付出。
