# 站点兼容与接入指南

> 给「想接一个新皮肤站」的人看。日常使用只需要 README。

## 先说结论

- **同一个 Blessing Skin 系、接口形状标准的站**：`sites` 里加一行就能用，**不动代码**。
- **字段名/路由/纹理 URL 形态不一样的魔改站**：加几个配置项（`field_*` / `list_key` / `texture_path` / `field_texture_url`），**仍然不动代码**。
- **完全另一套协议的站**（POST / GraphQL / 要 cookie / 要 token / 详情返回 HTML）：需要写适配器子类。

已实测（2026-10-25，只读）：

| 站点 | 适配器 | 需要改什么 | 结果 |
| --- | --- | --- | --- |
| `littleskin.cn` | `littleskin` | 默认值就是它 | 全通 |
| `skin.pueh.cn` | `blessing_skin` | 只加一行 `sites` | 搜索/详情/纹理/3D/随机 5/5 |
| `skin.fairingstudio.com` | `blessing_skin` | 只加一行 `sites` | 5/5 |

> 注意：这三个站接口形状本来就相近。用 `field_*` 接「非标站」这条路是用 mock 站验证的（三种字段风格），
> 真实世界里还没遇到样本。你接的站如果不符合预期，把探针输出贴出来，改配置基本能解决。

## 怎么配

`sites` 一行一个站，用 `|` 分隔：

```
别名|适配器|站点根|域名白名单[|可选覆盖项...]
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| 别名 | 是 | 小写字母/数字/`-`/`_`，1~16 字符；命令里 `-s 别名` 用它 |
| 适配器 | 是 | `littleskin` 或 `blessing_skin`（同构站都用后者） |
| 站点根 | 是 | 必须 `https`（`http://127.0.0.1` 这类本机测试除外） |
| 域名白名单 | 否 | 该站出站白名单，逗号分隔；不写就取站点根的 hostname |
| 可选覆盖项 | 否 | 形如 `search_path=/api/search`，见下表 |

每个站有**自己的白名单和限流**，一个站被限流不影响另一个站。`default_site` 指定默认站（空 = 第一行）。
配置写错了不会拖死插件：非法行会被跳过并打 `WARNING`，全部非法时自动退回单站点老配置。

## 可选覆盖项（`key=value`）

### 1. 路由与参数名

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `search_path` | `/skinlib/list` | 搜索接口 |
| `search_keyword_param` | `keyword` | 关键词参数名（不是所有站都叫 keyword） |
| `search_page_param` | `page` | 页码参数名 |
| `search_random_param` / `search_random_value` | `order` / `random` | 随机排序；留空则退化为第一页随机取一条 |
| `detail_path` | `/texture/{tid}` | 详情接口（`{tid}` 会被替换） |
| `texture_path` | `/textures/{hash}` | 纹理下载模板。可用 `{hash}` **或 `{tid}`**；也可以直接写完整 URL（`https://cdn.example.com/{hash}`） |
| `show_path` | `/skinlib/show/{tid}` | 详情页，用作 Referer |

### 2. 字段名映射（魔改站用）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `list_key` | `data` | 列表在响应里的位置，支持点号路径（`data.items`）；留空 = 响应本身就是数组 |
| `field_tid` | `tid` | 皮肤 ID |
| `field_name` | `name` | 皮肤名 |
| `field_type` | `type` | 模型（steve/alex） |
| `field_hash` | `hash` | 纹理 hash（模板要用 `{hash}` 时才必需） |
| `field_likes` | `likes` | 点赞数 |
| `field_uploader` | `nickname` | 上传者昵称（主选） |
| `field_uploader_fallback` | `uploader` | 主选为空时用这个 |
| `field_size` | `size` | 文件大小 |
| `field_upload_at` | `upload_at` | 上传时间 |
| `field_hd` / `field_public` | `hd` / `public` | HD 标记 / 是否公开 |
| `field_texture_url` | 空 | 站点**直接返回完整纹理地址**时填这个字段名（优先于 `texture_path`，仍受白名单限制） |

字段名支持嵌套路径（`a.b`），**找不到就按「未找到」降级，不会报错**。

### 3. 例子：一个字段全不一样的站

假设目标站：

- 搜索：`GET /api/search?q=关键词` → `{"items":[{"id":201,"title":"…","model":"alex","author":"…"}]}`
- 详情：`GET /api/skin/201` → 同结构，另有 `direct`（完整纹理 URL）

``` 
x|blessing_skin|https://skin.example.com|skin.example.com|search_path=/api/search|search_keyword_param=q|detail_path=/api/skin/{tid}|list_key=items|field_tid=id|field_name=title|field_type=model|field_uploader=author|field_texture_url=direct
```

## 体检脚本

接站之前先跑，**只读**，不写任何配置：

```bash
python tools/probe_site.py https://skin.example.com skin.example.com 关键词
```

输出长这样（五步依次验证）：

```
[search] '自由' -> 1 条
   tid=105 name=42奶奶的自由失效 type=alex likes=1 uploader=AYAOYA hd=None
[detail] tid=105 name=42奶奶的自由失效 type=alex hash=d4d31401710e… size=3
[texture] https://skin.pueh.cn/textures/d4d3140…
[download] d4d314….png (64, 64) 2216B
[render3d] (1000, 1112) -> /tmp/skin-probe-xxx/preview.png (17272B)
[rand] tid=140

通过 5/5 步
```

哪一步挂了，对应改哪里：

| 挂了 | 通常是 |
| --- | --- |
| `search` 0 条 | `search_path` / `search_keyword_param` / `list_key` / `field_name` |
| `detail` 解析失败 | `detail_path` / `field_tid` |
| `texture` 拼不出地址 | `texture_path` / `field_hash` / `field_texture_url` |
| `download` 404 或不是 PNG | 纹理模板要加扩展名、或者站点要求 Referer / UA |
| `render3d` 报错 | 纹理不是标准皮肤尺寸（见下） |
| `rand` 空 | 站点不支持 `order=random`（功能会退化为第一页随机） |

## 站点差异：配置修不了的

| 差异 | 表现 |
| --- | --- |
| 搜索区分大小写 | 插件会再试大写/首字母大写，但词形不同（自由 vs 自由敢达）就是搜不到 |
| 字段确实没有 | 例如某站详情只给上传者 ID 不给昵称，图注就显示数字 |
| 皮肤尺寸不标准 | 只认 64×32 / 64×64 / 128×64 / 128×128 / 256×64 / 256×128 / 256×256；其它尺寸直接丢弃（安全取舍） |
| 需要登录 / cookie / token | 不支持，也不打算支持（插件定位是匿名只读） |
| 接口是 POST / GraphQL / 返回 HTML | 要写适配器子类 |

## 写适配器（最后手段）

`sources.py`：

- `BlessingSkinSource` 是通用实现（配置驱动）；`LittleSkinSource` 只是注册名 + 默认参数。
- 覆写 `search()` / `detail()` / `parse_list()` 即可适配特殊协议。
- 在 `SOURCES` 字典里注册名字，然后就能在 `sites` 行里用。

请不要为了「适配更多站」而引入 cookie/登录态，也不要调高默认限流。
