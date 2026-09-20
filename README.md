# 网易云点歌 · astrbot_plugin_ncm_player

为 AstrBot 设计的网易云点歌插件，之前用的https://github.com/Zhalslar/astrbot_plugin_music 每次点歌都超时，语音都发不出来，于是按照这个插件的思路让kimi K3写了这个插件：

- **双通道点歌**：LLM 工具自然语言点歌 + 关键词监听兜底
- **发送方式自定义**：语音优先自动降级、只发语音、只发文件、只发卡片、或语音+文件+卡片一起发
- **音质档位化**：标准 128k / 较高 192k / 极高 320k / 无损 FLAC / 高清臻音 Hi-Res / 超清母带，自动逐级回退
- **扫码登录**：`/网易云登录` 生成二维码，网易云音乐 App 扫码后解锁会员音质（无损 / Hi-Res / 母带）
- **热评卡片**：点歌后嗅探一条最热评论，渲染成卡片随歌发送
- **歌词合并转发**：嗅探整首歌词，以聊天记录（合并转发）形式发送，纯音乐自动跳过
- **载入方式可选**：本地文件路径 / URL 直链 / base64 编码，适配不同部署拓扑

## 安装

在 AstrBot WebUI → 插件 → 右下角 + → 上传本插件 zip 压缩包（或克隆本仓库到 `data/plugins/` 目录），重启后生效。

依赖：`aiohttp`、`Pillow`（AstrBot 会自动安装 requirements.txt）。

## 使用

### 自然语言 / 关键词监听（推荐）

```
我：我要听晴天          ← LLM 工具调用；LLM 不灵时关键词监听自动接管
AI：（发送 CD 风选歌图）
我：2                  ← 纯数字序号直接播放（5 分钟内有效）
AI：（发送播放卡片 + 语音 + 热评卡片 + 歌词合并转发）
```

关键词监听支持「我想听 / 我要听 / 点歌 / 来一首 / 放一首 / 播放」等触发词，
带防误触发机制（叠词「想听听…」、疑问句「想听什么」不会触发），可用 `enable_keyword_listen` 关闭。

### 指令

| 指令 | 说明 |
| --- | --- |
| `/点歌 歌名` | 搜索并发送选歌图，120 秒内回复序号播放，发送「取消」退出 |
| `/直接点歌 歌名` | 跳过选歌，直接播放第一首结果 |
| `/网易云登录` | 生成登录二维码（需先配置 `ncm_api_base`），扫码解锁会员音质 |

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ncm_api_base` | 空 | NeteaseCloudMusicApi 服务地址，如 `http://127.0.0.1:3000`。配置后优先使用，支持扫码登录与母带级音质。部署见 [api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced) |
| `meting_api` | `https://api.qijieya.cn/meting/` | Meting 镜像，官方接口失效时的备用音源（音质不可控），留空禁用 |
| `quality` | `exhigh` | 音质档位：`standard` 标准128k / `higher` 较高192k / `exhigh` 极高320k / `lossless` 无损FLAC / `hires` 高清臻音 / `jymaster` 超清母带。取不到自动回退；无损及以上需登录会员 |
| `send_mode` | `auto` | `auto` 语音优先自动降级；`voice`/`file`/`card` 只发对应方式；`voice_card` 等下划线组合为同时多发 |
| `load_mode` | `file` | `file`=本地路径（**推荐**，要求 AstrBot 与 NapCat 同机）；`url`=直链由协议端下载（跨机部署用，此模式不本地下载）；`base64`=编码内嵌（大文件会撑爆 WebSocket，慎用） |
| `send_play_card` | `true` | 发送 CD 风播放卡片图 |
| `send_comment_card` | `true` | 发送热评卡片 |
| `send_lyrics_forward` | `true` | 发送歌词合并转发 |
| `send_timeout` | `20` | 每种发送方式的超时秒数 |
| `download_timeout` | `20` | 音频下载超时 |
| `download_max_mb` | `40` | 超过此大小不下载，直接降级为卡片。无损/母带通常 30-100MB，用高音质请调大 |
| `search_limit` | `5` | 选歌列表数量 |
| `http_proxy` | 空 | HTTP 代理 |

## 为什么默认用 file 载入而不是 base64？

AstrBot 的 aiocqhttp 适配器会把 Record 组件统一转成 base64（且会转码为 wav）再塞进 WebSocket，
一首歌轻松突破 ws 库的帧大小上限，协议端直接断开连接，表现为「点歌 60 秒超时」。
本插件在 aiocqhttp 平台直接调用原生 API 发送，`file` 模式下 WebSocket 上只传一个路径字符串。

## 音源说明

播放地址按以下优先级获取：

1. `ncm_api_base` 配置的 NeteaseCloudMusicApi 服务（音质可控，登录后可达母带级）
2. 网易云官方网页接口（无登录态时多数返回 -110，仅作尝试）
3. `meting_api` 镜像（实测可用，音质不可控）
4. 网易云官方外链兜底

VIP / 无版权歌曲可能所有源都拿不到音频，此时会自动降级为音乐卡片或链接。

## 目录结构

```
astrbot_plugin_ncm_player/
├── main.py              # 插件入口：LLM 工具 + 指令 + 扫码登录
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # WebUI 配置项
├── requirements.txt
└── core/
    ├── ncm_api.py       # 网易云 API：搜索 / 封面 / 播放地址 / 热评 / 歌词 / 二维码登录 / 下载
    ├── renderer.py      # Pillow 渲染：CD 风选歌图、播放卡片、热评卡片
    └── sender.py        # 语音 / 文件 / 卡片 / 链接 发送与降级
```
