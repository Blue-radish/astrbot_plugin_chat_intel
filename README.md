<p align="center">
  <img src="docs/logo.png" width="132" alt="技术情报日报">
</p>

<h1 align="center">技术情报日报（astrbot_plugin_tech_digest）</h1>

<p align="center">把群聊压成一份<strong>只留技术向内容</strong>的情报简报，本地排版成一张长图，私聊发给你。</p>

面向的读者是计算机专业的学生 / 从业者：只关心 **计算机与编程、AI、软件与工具、有意思的网站与资源、工作与就业**，
吃饭、游戏、情感、追星、地域争吵、纯玩梗复读一律不进报告。

## 样张

聊得比较开的一天（五个板块全满）：

![样张：完整](docs/sample-full.png)

只有零星技术讨论的一天（板块按内容有无自动省略）：

<img src="docs/sample-light.png" width="620" alt="样张：内容较少">

整群都在灌水的一天（没有技术内容时不硬凑，直接给空状态）：

<img src="docs/sample-quiet.png" width="620" alt="样张：没有技术内容">

## 它做什么

1. **拉消息**：用指定 AstrBot 平台的 OneBot 适配器读取群历史；机器人常驻时同时把每条群消息落盘累积（见「为什么要本地累积」）
2. **抽取**：一次 LLM 调用，输出结构化 JSON——技术话题 / 工具与资源 / 就业与工作 / AI 动向 / 今日圣经
3. **排版**：本地 [Typst](https://typst.app) 编译成一张长图，不依赖任何远程渲染服务（不需要浏览器、不需要 GPU、断网也能出图）
4. **多群**：一份配置盯多个群，每个群各出一份报告；报告里只写群名，不写群号
5. **现场图片**：群里的图片当场下载转存（QQ 图片链接有时效，晚了就下不到），生成时挑有价值的进报告
6. **投递**：图片写入插件数据目录，**永不发回群里**；到点自动私聊推送，或用命令随时生成
5. **静默通道**（可选）：让承载它的机器人账号在任何群、任何私聊里都不开口——用「只读机器人号挂着群」时不会暴露

## 安装

### 1. 插件本体

把本仓库放到 AstrBot 的插件目录：

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/Blue-radish/astrbot_plugin_tech_digest
```

然后在 WebUI 的「插件管理」里重载，或在配置里启用。

### 2. 依赖

```bash
pip install typst
```

Typst 是本地排版引擎，`pip install` 即可，无需额外运行时（不依赖浏览器、不依赖 LaTeX）。

### 3. 中文字体

报告模板优先使用 **Noto Sans SC**。把字体文件放进：

```
<AstrBot>/data/plugin_data/astrbot_plugin_tech_digest/fonts/
```

至少放一个中文字体（`NotoSansSC.ttf` 或任何 `Source Han Sans` / `Microsoft YaHei` 均可），
否则中文会显示成方框。模板的字体回退顺序是：
`Noto Sans SC → Source Han Sans SC → Microsoft YaHei → PingFang SC → Inter`，
所以 Windows / macOS 上即使不装 Noto 也能正常显示。

## 配置

在 WebUI 的插件配置页填写（也可以直接改 `data/config/astrbot_plugin_tech_digest_config.json`）：

| 配置项 | 说明 | 默认 |
|---|---|---|
| `group_ids` | 要总结的群号，多个用逗号分隔（必填） | 空 |
| `group_names` | 群名备注，格式 `群号=群名`，留空则自动获取 | 空 |
| `platform_id` | 用哪个平台实例去拉消息，该账号必须在这些群里 | 空 |
| `days` | 首次生成（还没有上次记录）时回溯多少天 | `1.0` |
| `max_messages` | 单群单次最多处理多少条 | `10000` |
| `use_local_store` | 本地累积群消息（强烈建议开） | `true` |
| `include_images` | 保存图片并挑进报告 | `true` |
| `max_images` | 每份报告最多放几张图 | `6` |
| `max_image_mb` | 单图大小上限，超过丢弃 | `8` |
| `daily_time` | 每天自动生成时间 `HH:MM`，留空则不定时 | `23:00` |
| `push_enabled` | 生成后自动私聊推送 | `false` |
| `push_umo` | 推送目标会话 `<平台id>:FriendMessage:<QQ号>` | 空 |
| `provider_id` | 指定模型供应商，留空用默认 | 空 |
| `keep_quotes` | 是否保留「今日圣经」板块 | `true` |
| `report_title` | 报告标题 | `技术情报日报` |
| `render_ppi` | 出图清晰度 | `180` |
| `ignore_uids` | 不统计的 QQ 号（一般填机器人自己） | 空 |
| `silence_platforms` | 静默平台，逗号分隔；留空则静默 `platform_id` | 空 |
| `font_dir` | 字体目录，留空用插件数据目录下的 `fonts/` | 空 |

`push_umo` 的格式是 `<平台实例id>:FriendMessage:<QQ号>`，比如机器人平台实例叫 `mybot`、你的 QQ 是 `10001`，
就填 `mybot:FriendMessage:10001`。想推到某个群则用 `GroupMessage:<群号>`——但**不建议**，那等于把报告发群里。

## 命令

| 命令 | 作用 |
|---|---|
| `/techdigest`（别名 `/技术日报`） | 只总结**还没看过**的消息（自上次报告以来的新消息） |
| `/techdigest 今天` | 今天 00:00 到现在 |
| `/techdigest 昨天` / `前天` | 那一整天 |
| `/techdigest 2026-11-05` | 指定日期（也支持 `11-05`、`3天前`） |
| `/techdigest 今天 群名关键词` | 只跑匹配的那个群，例如 `/techdigest 今天 考古` |
| `/techdigest last` | 重发最近生成的一份 |
| `/techdigest status` | 每个群的今日条数、带图数、上次覆盖到哪、静默拦截计数 |

不带时间参数就是「增量」：第一次跑回溯 `days` 天，之后每次只补上次之后的新消息，不会重复总结。

## 为什么要「本地累积」

多数 OneBot 实现（NapCat / SnowLuma 等）的 `get_group_msg_history` 一次只返回最近约 200 条，且
`message_seq` 参数不一定支持翻页。实测 SnowLuma：`count` 上限 200，
`message_seq` / `message_id` / `start_message_id` / `anchor` / `before` 五种传法都拿回同一页——
想覆盖一整天，靠历史接口是不够的。

所以本插件在静默通道里顺手把目标群的每条消息写到
`data/plugin_data/astrbot_plugin_tech_digest/messages/YYYY-MM-DD.jsonl`，
生成时把「本地库 + 历史接口兜底」合并去重。机器人 24 小时在线，就能拿到完整一天的聊天。

代价是：**首次安装的当天只能覆盖最近 200 条**，第二天起才是完整一天。

## 图片怎么挑

QQ 的图片链接带临时签名，几个小时后就失效，所以插件在收到消息时就把图下载转存成 JPEG
（限宽 1200px，超过 `max_image_mb` 的丢弃），再在生成时挑选。

挑选是「宁可漏，不要错」：先按尺寸和文字线索硬过滤（小于 500×500、接近正方形的一律当表情包；
本条消息里要有「报错 / 日志 / 代码 / 部署 / 接口」这类词或链接），再把候选连同上下文文字交给模型做减法。
实测在一个梗图密集的群里，一天几十张图最后能留下的通常是 0~2 张——这正是想要的效果：
报告里出现的图，基本都能拿来当证据看。

没有视觉模型也能跑：判断只依赖尺寸、形状和上下文文字。如果你把 `provider_id` 指向支持读图的模型，
效果会更准（当前版本仍以规则为准，不额外发图给模型）。

## 隐私与安全

- 报告**只落盘 + 私聊投递**，代码里没有任何「发回被总结的群」的路径
- 静默通道三层拦截（事件终止 / LLM 请求拦截 / 回复内容清空），避免机器人账号在群里说话
- 消息库只存文本与你配置的群，路径在插件自己的数据目录，删目录即清空

## 已知限制

- 需要机器人账号**在目标群里且长期在线**，离线期间的消息只能靠 200 条历史兜底补
- 依赖 OneBot v11 实现提供的 `get_group_msg_history` / `get_group_info`
- 报告质量取决于所配模型的结构化输出能力，建议用支持长上下文的模型
- 首次装上时本地库是空的，第一天只能覆盖最近约 200 条历史，从第二天起才是完整覆盖
- 图片挑选不读图，只靠尺寸、形状和上下文文字判断，宁可漏掉也不会放进梗图；
  想要更准需要接支持读图的模型

## 开发

```bash
# 渲染 logo 和三张样张（离线，不连 AstrBot，不调模型）
python scripts/render_assets.py --fonts ./fonts

# 只渲染某一张：logo / full / light / quiet
python scripts/render_assets.py --fonts ./fonts --scene light
```

代码结构：

```
main.py                  # 插件主逻辑：拉消息 / 抽取 / 排版 / 投递 / 静默通道
templates/report.typ     # Typst 排版模板，数据由同目录 data.json 提供
assets/logo.typ          # logo 源文件（纯几何，不依赖字体渲染）
scripts/render_assets.py # 用示例数据渲染 logo 与样张
_conf_schema.json        # WebUI 配置项定义
```

## License

MIT
