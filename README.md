# 网易云点歌 · astrbot_plugin_ncm_player

为 AstrBot 设计的网易云点歌插件。设计思路参考 [Zhalslar/astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music)，针对语音超时、音源回退和账号登录重新实现。

[![Version](https://img.shields.io/badge/version-v1.5.0-blue.svg)](https://github.com/TZYCeng/astrbot_plugin_ncm_player)
[![Python](https://img.shields.io/badge/python-3.10+-green.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.9+-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

- 自然语言 LLM 工具与关键词监听点歌，选歌按会话和用户隔离。
- 语音、文件、音乐卡片及组合发送；支持本地路径、URL、base64 载入。
- 扫码/短信登录与手动网页 Cookie 两路账号，分别核验 UID、昵称和会员状态。
- 内置 API、外部 API、网页音源、Meting 镜像和官方外链独立开关、多层回退。
- 标准至母带逐级降质，显示实际返回音质，识别试听；下载失败继续尝试其他来源。
- CD 风选歌/播放卡片、热评卡片和歌词合并转发。

## 安装与配置

在 AstrBot WebUI 安装本仓库，或将代码放入 `data/plugins/` 后重载插件。依赖为 `aiohttp`、`Pillow`，由 AstrBot 根据 `requirements.txt` 安装。

### 内置或外部 API

- **内置**：开启 `ncm_api_embedded`，保存后重载插件。首次从 [api-enhanced Release](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/releases/tag/v4.40.1) 下载当前平台的二进制，校验大小及 SHA-256 后启动。仓库和插件包不携带二进制。
- **外部**：开启 `ncm_api_external_enabled`，填写 `ncm_api_base`，例如 `http://127.0.0.1:3000`。
- 两者可以单开、双开或全关；双开时内置优先，失败继续外部。全关仍可使用独立开启的网页、镜像和官方外链音源。
- 内置服务支持 Linux/Windows/macOS x64；macOS arm64 需要 Rosetta，其他平台可使用外部 API。

配置修改后重载插件生效。API 管理命令会立即执行并保存内置开关。

### 网页 Cookie

1. 在自己的浏览器登录 `music.163.com`。
2. 在开发者工具的网络请求中复制请求头 `Cookie` 内容，应包含 `MUSIC_U`。
3. 填入 `ncm_web_cookie`，打开 `ncm_web_cookie_enabled` 和 `ncm_web_enabled`，保存并重载。
4. 管理员发送 `/网易云诊断` 核对网页账号的 UID、昵称及会员状态。

网页 Cookie 不依赖先扫码，与扫码/短信账号分开保存。Cookie 是账号凭据，不要发到群聊或公开日志；过期后更新配置即可。会员状态查询失败显示“未知”，不会直接认定为非会员；歌曲是否可下载最终取决于实际返回的完整音频权限。

## 使用

```text
我：我要听晴天
机器人：（选歌图）
我：2
机器人：（播放卡片、语音、热评和歌词）
```

关键词支持“我想听 / 我要听 / 点歌 / 来一首 / 放一首 / 播放”，可用 `enable_keyword_listen` 关闭。普通选歌结果 5 分钟内可回复数字；`/点歌` 的交互等待时间为 120 秒。

| 指令 | 说明 |
| --- | --- |
| `/点歌 歌名` | 搜索选歌，回复序号或“取消” |
| `/直接点歌 歌名` | 播放搜索到的第一首 |
| `/网易云登录` | 管理员扫码登录，需要已就绪的内置或外部 API |
| `/网易云验证码登录` | 仅管理员私聊，不带参数，按提示输入大陆手机号和验证码；短信间隔至少 60 秒，验证码最多尝试两次 |
| `/网易云诊断` | 管理员核验两路账号 UID、昵称、打码用户名、会员状态、API 版本、启动错误及最近点歌回退原因 |
| `/网易云退出登录` | 管理员清除扫码/验证码登录；手动网页 Cookie 在配置页关闭或清空 |
| `/网易云清理缓存` | 管理员清理点歌临时文件和选歌状态，跳过正在下载/发送及等待协议端读取的音频与当前二维码 |
| `/网易云删除API` | 管理员停止并删除内置 API 程序和隔离运行缓存，**持久化关闭内置开关**；下次开启并重载会重新下载 |
| `/网易云重装API` | 管理员停止、删除、重新下载校验并启动内置 API，**持久化开启内置开关**，随后复核账号 |

删除/重装仅管理插件创建的服务，保留账号 Cookie、点歌缓存和外部 API。进行中的登录会取消，防止重装前的旧响应覆盖账号。重装失败可用诊断命令查看原因，其他启用的音源继续参与回退。

## 音源与音质链路

1. **API 登录账号**：内置 API → 外部 API，各服务从所选档位向下降质。
2. **网页 Cookie 账号**：用独立 Cookie 尝试内置 API → 外部 API → 官方网页接口。
3. **Meting 镜像**。
4. **网易云官方外链**。

仅尝试已启用的来源。未配置网页 Cookie 时，网页阶段仅尝试匿名网页接口。API 返回试听、无地址、请求失败，或实际音频下载失败/超限，都会继续链路；URL 模式先做少量音频响应探测。全部失败时发送歌曲网页链接。镜像、外链和账号均不保证拥有每首歌曲的版权或完整音频。

API 账号与网页账号的会员信息独立，VIP 查询异常不覆盖已确认的账号身份。登录校验拒绝游客及 account/profile UID 不一致的响应。Cookie 经统一规范化后，以 POST Cookie 对象及规范请求头提交；带上游 `X-Apicache-Bypass` 与独立缓存键，避免 Cookie 格式或缓存造成错误身份。每次登录和点歌固定凭据快照，旧复核不能覆盖新登录。

音质显示使用服务实际返回的 `level`、格式或码率，不将请求的母带档位直接标成下载结果。`freeTrialInfo` 的空值和字符串 `"null"` 不视为试听。

## 主要配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `ncm_api_embedded` | `false` | 内置 API 开关 |
| `ncm_api_embedded_port` | `13000` | 只监听 `127.0.0.1`，端口冲突需修改 |
| `ncm_api_external_enabled` | `true` | 外部 API 独立开关，地址为空时不调用 |
| `ncm_api_base` | 空 | 外部 API 根地址 |
| `ncm_web_enabled` | `true` | 网页账号及网页音源阶段开关 |
| `ncm_web_cookie_enabled` | `false` | 手动 Cookie 开关 |
| `ncm_web_cookie` | 空 | 自己的网易云网页 Cookie |
| `meting_enabled` | `true` | 镜像音源开关 |
| `meting_api` | `https://api.qijieya.cn/meting/` | 可改为自己的 Meting 地址，留空不使用 |
| `ncm_outer_enabled` | `true` | 最后一级官方音频外链开关 |
| `ncm_api_github_acceleration` | `true` | 开启时加速站依次尝试，最后回退官方地址；关闭时只用官方 GitHub |
| `ncm_api_github_accelerators` | `ghfast.top`、`gh-proxy.com` | 可编辑的有序 URL 前缀列表，所有下载均验证固定版本摘要 |
| `ncm_api_embedded_mirror` | 空 | 兼容旧配置的优先加速前缀，失败后仍轮换，仅加速开启时生效 |
| `ncm_api_log_output` | `false` | 将内置 API stdout/stderr 经常见敏感字段脱敏后转发到 AstrBot 日志 |
| `quality` | `极高 320k` | 标准 / 较高 / 极高 / 无损 / Hi-Res / 母带 |
| `load_mode` | `file` | `file` 本地路径；`url` 协议端下载；`base64` 内嵌发送 |
| `send_mode` | `auto` | 语音优先降级，或 `voice` / `file` / `card` 及下划线组合 |
| `send_play_card` / `send_comment_card` / `send_lyrics_forward` | `true` | 播放卡片、热评、歌词开关 |
| `enable_keyword_listen` | `true` | 关键词点歌开关 |
| `send_timeout` | `20` | 单种发送方式的超时秒数 |
| `download_timeout` | `20` | 下载停滞超时秒数，失败重试一次 |
| `download_max_mb` | `40` | 单个音源下载上限，超限后降质/换源；高音质通常需调大 |
| `search_limit` | `5` | 搜索候选数量 |
| `http_proxy` | 空 | 公网请求/Release 下载代理，本机 API 不经过代理 |

`file` 适合 AstrBot 与协议端同机，在 aiocqhttp 平台通过原生 API 只传文件路径，避免大音频转 base64 撑大 WebSocket 消息。跨机部署可使用 `url`；`base64` 会增加消息体积。

## 数据目录与服务管理

插件通过 `StarTools.get_data_dir("astrbot_plugin_ncm_player")` 获取 AstrBot 数据目录，通常为：

```text
data/plugin_data/astrbot_plugin_ncm_player/
├── ncm_cookie.txt           # 扫码/短信账号，规范化后原子保存
├── cache/                  # 音频、卡片和二维码
└── ncm_api_server/
    ├── ncm-api-<platform>   # 固定 Release 资产
    └── runtime/            # 子进程专用 TMP/TEMP/TMPDIR
```

手动 Cookie 及开关由 AstrBot 插件配置保存。运行时不向插件源码目录写程序、缓存或临时文件。上游的 `anonymous_token`、`xeapi_public_key` 被限制在子进程私有临时目录，删除/重装可清理；不会删除系统临时目录中的其他服务文件。启动验证 `/inner/version` 和目标版本，不把任意 HTTP 200 当作 API 已就绪。

## 开发验证

```bash
python -m pip install -r requirements.txt ruff
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -t . -v
```

测试用本地 aiohttp 服务覆盖真实 HTTP 请求、登录 Cookie、双账号、缓存隔离、下载回退和生命周期；仅 AstrBot 宿主接口使用替身。可额外设置 `NCM_RELEASE_SMOKE=1` 后运行 `python -m unittest tests.test_release_smoke -v`，验证真实 Release 下载、启动、重装及删除；该测试无需真实账号。

## v1.5.0 历史瘦身

本次不仅移除当前 `bin/`，还通过 `git-filter-repo` 从 Git 历史删除三个二进制（合计约 221 MB 原始文件），保留其余开发记录。历史提交哈希已重写，已有克隆建议备份本地工作后重新克隆。以后只在 AstrBot 数据目录按需下载 Release；备份包放在仓库外，未提交。

## 开源致谢与许可

- 本插件采用 [MIT](LICENSE)，保留 [astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music) 的设计来源说明。
- 内置服务下载自 [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)，固定版本 [v4.40.1](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/releases/tag/v4.40.1)。本次依据其公开接口独立实现客户端和进程管理；仓库不打包其二进制。
- 上游 [v4.40.1/LICENSE](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/blob/v4.40.1/LICENSE) 原文如下：

```text
The MIT License (MIT)

Copyright (c) 2013-2022 Binaryify

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
```
