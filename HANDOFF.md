# 网易云点歌插件 · 开发交接文档

> 本文是旧仓库的历史交接。v1.5.2 是旧仓库最后一个迁移过渡版；后续维护和寒假重构迁往 [astrbot_plugin_tzyc_music](https://github.com/TZYCeng/astrbot_plugin_tzyc_music)，问题请到[新仓库 Issues](https://github.com/TZYCeng/astrbot_plugin_tzyc_music/issues)。以下版本与行号对应历史快照。
> 交接对象：后续接手维护/二次开发的开发者
> 文档基于代码快照：`5811d6a fix: repair login and add resilient API lifecycle (v1.5.0)`（2026-10-09）
> 文档编写日期：2026-10-25
> 当前版本：**v1.5.1**（`metadata.yaml`），注册版本 `1.5.1`（`main.py:61`）

本文不是 README 的复述。README 讲**用户怎么用**；本文讲**代码为什么这么写、坑在哪、接下来怎么改**。

---

## 本轮交接更新：跨系统路径修复与审查待办（2026-10-25）

本节优先于下方历史快照的行号、测试数量、发送路径描述与问题优先级。本轮路径修复已归入 v1.5.1 补丁版本，版本号、插件描述和更新日志已同步；没有升级上游 API，也没有创建版本标签或 GitHub Release。R01–R30 审查建议仍待逐项确认。

### 已完成的修改

- 修复 Linux Docker 路径被默认转成 Windows 的问题：`guest_path_style=auto` 不再猜测远端操作系统，无映射时保留原路径。
- `PATH_CONVERT` 支持 Linux/Windows 双向绝对目录映射、完整目录边界和最长前缀匹配；Windows 源路径不区分大小写，POSIX 源路径区分大小写。
- `windows` / `linux` 仅校验目标路径类型，不再自动构造 `C:\AstrBot`。旧配置依赖自动猜测时，应补充真实映射。
- 语音及文件发送使用同一映射；URI 按目标路径编码，支持盘符、UNC、中文、空格和特殊字符。
- 更新配置提示与 README，新增10项跨系统路径与发送参数测试。base64原有实现保留，已验证其语音载荷；未新增自动切换或消息上限探测。

具体实现见 [sender](core/sender.py)、[配置](./_conf_schema.json)、[测试](tests/test_sender_paths.py) 与 [部署说明](README.md)。

### 部署选择与环境变量

环境变量设置在 **AstrBot 进程/容器**，左侧是它看到的目录，右侧是 **协议端进程看到的目录**，不一定是物理宿主机路径：

```yaml
environment:
  PATH_CONVERT: '/AstrBot/data=>/shared/astrbot-data'
# Windows协议端可改成：
# PATH_CONVERT: '/AstrBot/data=>D:\astrbot\data'
```

多条映射以分号分隔。映射不会共享或复制文件：两端必须实际访问同一份音频，并有读取权限。修改Docker环境变量通常需重新创建容器，插件重载不会改变已有进程环境。

| AstrBot / 协议端 | file语音与文件发送 | base64语音 | URL语音 |
|---|---|---|---|
| Linux / Linux，同路径 | 保持路径，协议端可读即可 | 不需要共享目录 | 协议端独立访问直链 |
| Linux Docker / Linux宿主或另一容器 | 共享存储，路径不同则映射 | 同上 | 同上 |
| Linux Docker / Windows | 共享存储，映射到实际Windows目录 | 同上 | 同上 |
| Windows / Windows | 同路径或显式映射 | 同上 | 同上 |
| Windows / Linux，或跨机器 | 先共享文件，再映射 | 同上 | 同上 |
| macOS / POSIX协议端 | 按POSIX路径规则处理 | 同上 | 同上 |

无共享目录时可用 `load_mode=base64`、`send_mode=voice` 或 `voice_card`。base64仅用于语音；文件发送和auto文件降级仍需共享路径。40 MiB音频编码约53.3 MiB，另有JSON和内存副本，必须考虑协议端、WebSocket、代理限制。URL模式不下载本地音频，文件发送不可用，也不转交插件Cookie、请求头或代理。

Linux ARM64没有当前内置API资产，需使用外部API；macOS ARM64内置资产依赖Rosetta。这些限制独立于路径修复。

### 本轮验证与边界

- Windows Python 3.13.15：完整单测33项，32通过、1项真实Release下载测试默认跳过。
- 全仓库Ruff lint、格式检查及 `git diff --check` 通过。
- 没有Docker CLI，未连接真实AstrBot/NapCat。跨系统验证使用路径函数和模拟OneBot发送参数，不等于挂载、权限、音频解码或实际投递验收。
- Windows受限环境曾拒绝格式化写入；权限诊断未发现可修复项，没有修改权限。切换完全权限后验证通过。不能把此前所有拒绝访问都断言为已证明的ACL根因。

### 待确认审查建议（全部尚未实施）

已完整审查生产代码、测试、配置及文档。完整的触发条件、代码位置、影响和修复边界见 [审查报告](REVIEW_REPORT.md)。以下编号与报告一致，后续按用户选择执行，不应把本次提交/推送授权理解为批准这些修复。

P1=建议尽快处理，P2=常规可靠性修复，P3=工程改进；没有足够证据认定存在P0事故。条件风险须在实施时验证其部署前提。

| 编号 | 级别 | 问题与拟修复方向 |
|---|---|---|
| R01 | P1 条件风险 | 认证POST跨源307/308可能转发JSON凭据；禁自动重定向或逐跳同源校验。 |
| R02 | P1 | 多行、超长分片及QR key存在日志脱敏缺口；采用结构化安全字段及完整记录过滤。 |
| R03 | P1 | stop被取消可能跳过kill/reap；保护有界清理，完成后再传播取消。 |
| R04 | P1 条件风险 | 卸载不主动失效/取消登录流程，依宿主行为可能保存旧凭据；closing状态、代数、任务回收。 |
| R05 | P1 | 启动/重装持锁下载可拖延停止；可取消启动任务、短锁和整体预算。 |
| R06 | P2 | card模式仍要求音频成功，音源失败绕过卡片；按发送策略获取音频并保留卡片回退。 |
| R07 | P2 | pending过期条目仅同key访问时清理；增加TTL清扫与容量限制。 |
| R08 | P2 | 短信登录首条回复前退出可能占槽180秒；注册时保存可取消会话。 |
| R09 | P2 条件风险 | 发送图片可被清缓存或12张淘汰删除；统一图片占用与宽限期保护。 |
| R10 | P1/P2 | 图片响应、解码、评论画布缺资源预算；字节、像素、文本和画布上限。 |
| R11 | P2 | 同步渲染及整文件base64阻塞事件循环；有界工作线程、大小预算并处理清理并发。 |
| R12 | P2 | 无插件级频控/并发闸门；用户/群限流、下载和渲染队列上限。 |
| R13 | P2 | 非空octet-stream错误页可被当音频；内容识别/媒体探测，保持换源。 |
| R14 | P2 | 高档位异常直接换源可能漏掉低档；区分服务故障与档位失败。 |
| R15 | P2 | 持续慢滴流及串行回退没有整体预算；总时限/最低速率窗口。 |
| R16 | P2 | 子进程退出后仍显示运行中；退出watcher，自动重启作为独立可选项。 |
| R17 | P2 条件风险 | Cookie文件未显式私有权限；POSIX 0600与目录权限、Windows继承权限核验。 |
| R18 | P2 待验证 | 真实waiter并发派发时可能取消后提交/突破次数限制；先验证再串行化状态机。 |
| R19 | P2 | 取消to_thread删除不终止线程，可能与新启动竞态；等实际删除结束再释放锁。 |
| R20 | P2 | 发送超时可能结果未知并重复降级，文件显示名及超时策略不统一；分类处理与边界校验。 |
| R21 | P2 按需 | 全局映射不能为多个异构协议端选择不同目标；按机器人/适配器配置映射。 |
| R22 | P2 | fee=0被or忽略、图片代理不一致、缺字体重复搜索；分项修零值/代理/字体降级。 |
| R23 | P2 | 配置类型/范围/组合验证不足；集中校验，明确url+file等组合限制。 |
| R24 | P3 | CI及真实宿主/取消/降级/渲染测试不足；多系统Python矩阵与行为回归。 |
| R25 | P3 | 依赖无区间，版本测试仅硬编码metadata；兼容范围与实际一致性验证。 |
| R26 | P3 | 门面和API模块职责过多；测试先行渐进拆分，诊断改公开接口。 |
| R27 | P3 | 仅最后一条播放记录，异常/URL脱敏分散；有界历史、阶段耗时与安全日志。 |
| R28 | P3 | 重复下载、固定清理、附加消息串行；可选隔离缓存、去重、孤儿清理和受管理后台任务。 |
| R29 | P3 | ARM64资产缺失、摘要手工升级、仅直接子进程管理；按上游支持与实际后代进程证据扩展。 |
| R30 | P3 | 交接与历史文档过时；更新发布流程；群策略/队列/WebUI/多音源/i18n独立立项。 |

建议先评估R01–R05与R10，再R06–R09、R11–R19；其余按部署需求拆分。不要一次性大重构。用户尚未选择执行编号。

### 对历史交接结论的纠正

- `_waiting` 有finally释放，不能和pending一起称永久驻留。
- `_probe_accounts` 本身不获取 `_service_lock`；管理操作持锁影响terminate的事实仍成立。
- 版本重复、没有CI不是P0；同步渲染耗时未经基准测量。
- sender测试不再为零，旧Path.as_uri描述已被本轮映射/URI函数替代。
- 版本测试仅检查硬编码metadata字符串，没有真正比较注册版本。
- 固定大小和SHA-256有效保护Release执行；Range检查不足不等于摘要绕过。
- auth模块未发现同级独立确证漏洞；会员字段冲突须先核实上游语义。

---

## 0. 历史验证记录（以本轮交接更新为准）

| 项目 | 命令 | 结果（2026-10-25） |
| --- | --- | --- |
| Lint | `python -m ruff check .` | 通过，无告警 |
| 格式 | `python -m ruff format --check .` | 15 files already formatted |
| 单测 | `python -m unittest discover -s tests -t . -v` | 23 tests，**OK（skipped=1）** |
| 版本一致性 | 见 `tests/test_plugin.py:209` | 通过（但硬编码 `v1.5.0`，发版需手动改，见 §7-P1-4） |
| 真实 Release 冒烟 | `NCM_RELEASE_SMOKE=1 python -m unittest tests.test_release_smoke -v` | 默认跳过；需联网下载 ~66MB 二进制 |

规模：**生产代码 3126 行 / 测试 926 行（约 0.30 倍）**，6 个生产文件。

> 环境注意：若在受限沙箱（如 DSH 的 workspace-write）中运行测试，`tempfile.mkdtemp()` 生成的临时目录带受限 ACL，会导致 `PermissionError: cache` 之类的失败。这是环境问题，不是代码缺陷；在普通终端或放宽权限下 23 项测试全绿。

---

## 1. 一分钟了解这个项目

一个 AstrBot（QQ/多平台机器人框架）插件：用户在群里说「我想听晴天」，机器人搜索、发一张 CD 风格选歌图，用户回「2」，机器人把歌以语音/文件/音乐卡片发出来，并附播放卡片、热评卡片、歌词合并转发。

技术要点不是"调个网易云接口"，而是三件事：

1. **多级音源回退**：API 账号 → 网页 Cookie 账号 → Meting 镜像 → 官方外链，逐级降质、逐级探测，任一级真实音频不可用就继续往下走。
2. **账号与登录的脏数据治理**：网易云扫码接口会下发游客 Cookie、会用 `Set-Cookie` 属性污染 Cookie 字符串、上游还有 URL 级 apicache。代码里大量精力花在"如何证明这次登录真的是那个账号"。
3. **自带 API 服务的生命周期管理**：把上游 NeteaseCloudMusicApi 的预编译二进制按需下载到插件数据目录、SHA-256 校验、隔离 TMP、启动健康检查、可删除/重装，且**不阻塞 AstrBot 启动**。

---

## 2. 快速上手

### 2.1 本地运行（接插件开发）

```bash
python -m pip install -r requirements.txt ruff
python -m ruff check . && python -m ruff format --check .
python -m unittest discover -s tests -t . -v
```

AstrBot 宿主是**可选的**：`tests/support.py:80-119` 用 `sys.modules` 注入了 `astrbot.*` 的替身，因此测试不需要安装 AstrBot。真实插件运行才需要 AstrBot 4.9+。

### 2.2 真实环境调试

1. 源码放入 `AstrBot/data/plugins/astrbot_plugin_ncm_player/`，WebUI 重载插件。
2. 打开 `ncm_api_embedded` → 重载 → 首次自动下载上游 API 二进制（或配置 `ncm_api_base` 指向自建服务）。
3. 管理员私聊机器人：`/网易云登录`（扫码）或 `/网易云验证码登录`（短信）。
4. `/网易云诊断` 是排障主入口：服务可达性、版本、两个账号的 UID/昵称/会员态、最近一次点歌的回退原因。

### 2.3 数据目录（不要写进源码目录）

```
data/plugin_data/astrbot_plugin_ncm_player/
├── ncm_cookie.txt              # 扫码/短信账号 Cookie（规范化后原子写入）
├── cache/                      # 音频（<song_id>_<rand>.ext）、卡片图（prefix_<rand>.png）、登录二维码
└── ncm_api_server/
    ├── ncm-api-<platform>[.exe]  # 固定 Release 资产
    └── runtime/                  # 子进程专用 TMP/TEMP/TMPDIR（隔离 anonymous_token 等）
```

数据目录由 `StarTools.get_data_dir()` 获得（`main.py:67`）；运行时**不向插件源码目录写任何文件**，这是 v1.5.0 明确的设计约束。

---

## 3. 代码地图

```
main.py (1073)            插件门面：注册、配置、三条入口、登录/管理命令、选歌状态
core/
├── auth.py      (122)    Cookie 解析/规范化、账号快照 AccountState、会员判定
├── ncm_api.py   (819)    HTTP 客户端 + 数据模型 + 音源回退 + 账号复核 + 下载
├── ncm_server.py (367)   内置 API 二进制下载/校验/进程生命周期
├── renderer.py  (519)    Pillow 渲染：CD 盘面、选歌图、播放卡、热评卡
└── sender.py    (226)    发送链路：语音 → 文件 → 音乐卡片 → 链接
tests/           (926)    support.py 造 AstrBot 替身 + 4 个测试模块
```

依赖方向是单向的：

```
main.py ──> sender.py ──> ncm_api.py ──> auth.py
   │            │             │
   ├──> renderer.py ──────────┘
   └──> ncm_server.py   （只依赖标准库 + aiohttp，不依赖其它 core 模块）
```

`core/__init__.py` 为空，没有对外导出；目前所有跨模块引用都是显式 `from .core.xxx import ...`。

### 3.1 各文件职责与"看哪里"

| 文件 | 职责 | 关键位置 |
| --- | --- | --- |
| `main.py` | 插件类、配置读取、点歌编排、命令注册 | 常量 `:28-31`；触发词 `:34-46`；`__init__` `:64`；`initialize` `:133`；`terminate` `:166` |
| `core/auth.py` | Cookie 边界与会员状态 | `parse_cookie` `:23`；`normalize_cookie` `:48`；`AccountState` `:70`；`membership_status` `:86` |
| `core/ncm_api.py` | 网络与业务数据 | 音质表 `:25`；模型 `:53-143`；客户端 `:148`；搜索 `:280`；回退链 `:371`；登录接口 `:520`；复核 `:612`；下载 `:745` |
| `core/ncm_server.py` | 内置服务进程 | 摘要表 `:27`；下载 `:135`；健康检查 `:293`；删除/重装 `:352` |
| `core/renderer.py` | 图像 | 字体探测 `:29-75`；CD 盘面 `:123`；选歌 `:286`；播放卡 `:368`；热评 `:445` |
| `core/sender.py` | 发送降级 | 平台判定 `:27-47`；语音 `:51`；文件 `:88`；卡片 `:112`；分发 `:124` |

---

## 4. 运行时架构与关键流程

### 4.1 三条点歌入口（同一套下游）

```
                    ┌──────────────────────────────┐
 用户消息 ──┬──────>│ 1) @filter.regex 关键词监听   │ listen_keyword    main.py:451
            │       │    不过 LLM，直接搜歌发选歌图 │
            ├──────>│ 2) LLM 工具 search_songs      │ main.py:392
            │       │    play_song(index/song_name)│ main.py:411
            └──────>│ 3) 指令 /点歌 /直接点歌       │ main.py:502 / :562
                    └───────────────┬──────────────┘
                                    │
                    _search_and_render()  (main.py:196)
                      ├─ api.search(limit)            → 网易云搜索接口
                      ├─ 并发拉取封面 fetch_bytes
                      ├─ renderer.render_selection()  → 图片
                      └─ self.pending[群#用户] = (时间, 候选)
                                    │
                              用户回复数字
                                    │
                    _play()  (main.py:323)
                      ├─ iter_play_info() 逐级取地址（见 4.2）
                      ├─ _download() / check_audio_url()
                      ├─ render_playing() 播放卡片
                      ├─ sender.send()    语音/文件/卡片（见 4.4）
                      └─ 热评卡片 + 歌词合并转发
```

**选歌状态隔离**：key 是 `unified_msg_origin#sender_id`（`main.py:174-181`），所以同群不同用户互不串台；有效期 `PENDING_EXPIRE = 300s`（`main.py:28`）。数字回复处理器 `pick_by_number`（`main.py:480`）只在"该用户确实有待选列表"时才 `stop_event()` 吞掉消息。

`/点歌` 额外用 `session_waiter(timeout=120)`（`main.py:517`）做同步式等待，并忽略同群其他用户的消息（`main.py:523-527`）；`_waiting` 字典用于让数字监听器知道"这条消息已经被指令等待器接管"，避免双重处理。

### 4.2 音源回退链路（本项目最核心的算法）

`NetEaseAPI.iter_play_info()`（`core/ncm_api.py:371-472`）是一个**异步生成器**：每 yield 一个候选，调用方就去真实下载/探测；失败后继续 `async for`，所以"拿到坏链接"不会终止回退。

```
调用方 _play()                        iter_play_info()
   │                                      │
   │                                      ├─ 快照 sources / cookie / generation  (:380-383)
   │                                      │
   │<── yield PlayInfo(ncm, 内置, api) ───┤  API 账号：内置 → 外部 API
   │    下载失败 → trace.record(...)      │  每个服务从所选档位向下逐级降质
   │<── yield PlayInfo(ncm, 内置, web) ───┤  网页 Cookie 账号：内置 → 外部
   │<── yield PlayInfo(web, 直连) ────────┤  官方网页接口（有 Cookie 用 Cookie，否则匿名）
   │<── yield PlayInfo(meting) ───────────┤  Meting 镜像（单条 URL，不做探测前校验）
   │<── yield PlayInfo(outer) ────────────┤  官方外链（最后兜底）
   └─ 全部失败 → 发送歌曲网页链接
```

关键细节：

* **档位只会向下**：`LEVEL_FALLBACK = list(reversed(QUALITY_LEVELS))`（`:34`），从用户选择的档位切片 `levels = LEVEL_FALLBACK[index:]`（`:384`），绝不请求高于用户选择的音质。
* **试听识别**：`_is_trial()`（`:320-331`）同时处理 `null`、字符串 `"null"`、对象；任何来源返回试听片段都不算成功，进入下一来源，并置 `trace.trial = True`（用于播放卡片灰框）。
* **URL 去重**：`seen` 集合防止同一 URL 被重复 yield。
* **不混用凭据**：一次点歌内 `cookie`/`web_cookie`/`sources`/`generation` 是快照，中途换号不会串。
* **异常分层**：`/song/url/v1` 请求异常 → 记录后 `break` 出档位循环，进下一个服务（每个服务每个凭据只打一次，不是 5 次）。
* **元数据可信**：`_play_info` 只接受 `http/https` URL，`type` 只接受白名单扩展名（`:355-358`），避免服务端返回内容变成任意路径。
* **网页直连档位合并**：`999000` 的无损/Hi-Res/母带在网页接口是同一个 `br`，用 `dict.fromkeys` 去重，避免 3 次重复请求（`:430`）。

### 4.3 账号与登录（历史 bug 最密集的区域）

三种凭据，两条账号通道：

| 凭据 | 来源 | 存储 | 用途 |
| --- | --- | --- | --- |
| API 账号 Cookie | 扫码 / 短信验证码 | `ncm_cookie.txt`（原子写） | `ncm` 音源、播放卡 VIP 判定 |
| 网页 Cookie | 用户手动从浏览器复制 | AstrBot 插件配置 `ncm_web_cookie` | 第二账号 + `web` 直连音源 |
| 游客/匿名 | 无 | 不保存 | 网页阶段匿名兜底 |

**Cookie 规范化**（`core/auth.py:23-59`）：这是 v1.5.0 的根因修复。上游扫码接口会把多条 `Set-Cookie` 用分号拼成 `Path=/;MUSIC_U=...`，若原样转发，上游严格解析会丢掉 `MUSIC_U`。处理方式：

* 丢弃 `Path/Domain/Expires/Max-Age/Secure/HttpOnly/SameSite/...` 等属性；
* 用 `partition("=")` 保留含 `=` 的 token（如 `MUSIC_U=user==`）；
* 同名取最后一个；
* 有 `MUSIC_U` 时移除 `MUSIC_A`（游客凭据不混入）；
* 鉴权同时用 POST 体 `{"cookie": {...}}` + 规范化后的 `Cookie` 头，并带 `X-Apicache-Bypass: true` 与随机 `_ncm_nonce`（`core/ncm_api.py:236-266`）。

**登录复核三道闸**（`core/ncm_api.py:615-692`）：`code != 200` → 无效；`account.anonimous` → 游客；`account.id != profile.userId` → 疑似串号。另外，扫码 `803` 后若 Cookie 不含 `MUSIC_U`，直接拒绝保存（`main.py:1053-1063`），这是"换号扫码却是同一游客账号"的根因。

**防覆盖（generation 机制）**：`_login_generation` 每次退出登录/重装 API 自增；所有异步流程（扫码轮询、验证码会话、复核）在写状态前检查代数，旧请求不能覆盖新登录（`main.py:964-980`、`core/ncm_api.py:694-739`）。

**DummyCookieJar**（`core/ncm_api.py:193-197`）：禁用 aiohttp 自动 Cookie，避免登录响应的 `Set-Cookie` 与手动 Cookie 叠加出重复/过期 `MUSIC_U`——这是"VIP 歌偶尔变 30 秒试听"的根因之一。

**扫码流程**（`main.py:1003-1073`）：`qr_key` → `qr_create`（base64 图片写入 cache）→ 发送图片 → 每 1.5s `qr_check` 最多 120 次；802 提示确认；803 建一个**独立 `NetEaseAPI` 实例**用新 Cookie 复核，复核通过才落盘 + `adopt_login`。

**短信流程**（`main.py:811-936`）：仅管理员私聊；必须只发命令本身（防手机号进群聊历史）；60s 冷却；手机号正则；验证码最多两次尝试；同样的 generation 校验。

### 4.4 发送链路

`core/sender.py` 的设计动机写在文件头：aiocqhttp 适配器会把 `Record` 统一转 base64 塞进 WebSocket，大音频会撑爆帧上限（`Max payload size exceeded`）。因此：

* 检测 `event.bot` 是否具备 `send_private_msg`/`send_group_msg`（aiocqhttp 原生 bot）→ 是则直接发 `{"type":"record","data":{"file": ...}}`；
* `load_mode=file` → `Path.as_uri()` 只传路径；`url` → 传直链；`base64` → 内嵌；
* 非 aiocqhttp 平台回退到 AstrBot 标准 `Record` 组件链路；
* 每级 `asyncio.wait_for(..., send_timeout)`；
* `auto`：语音 → 文件 → 音乐卡片 → 纯文本链接；显式组合（`voice_file_card` 等）只发选中的，全部失败仍用链接保底。

### 4.5 内置 API 服务生命周期

`core/ncm_server.py`。设计原则：**只管理自己创建的东西**。

* 资产名按 `platform.system()/platform.machine()` 映射（`:41-48`）；macOS arm64 走 x64 二进制（依赖 Rosetta）。
* 固定版本 `v4.40.1` + 每个资产的大小与 SHA-256（`:21-40`），下载后校验才 `os.replace` 原子安装。
* 下载源：可配置加速前缀 → 官方 GitHub；每个源最多 2 次；`Range` 断点续传且校验 `Content-Range` 的起点与总长（`:172-203`）；`tmp` 文件 `.part`，不同镜像**不复用已下字节**。
* 启动：`_check_port()` 先 bind 探测端口 → `ensure_binary()` → `runtime_dir` 必须是非符号链接的真实目录 → 子进程 env 继承后覆盖 `PORT/HOST/TMP/TEMP/TMPDIR` → 读日志分块（防上游超长行阻塞）→ `_wait_ready()` 校验 `/inner/version` 的 `code==200` **且版本号匹配**（不把任意 HTTP 200 当就绪）。
* 停止：`terminate` → 5s → `kill` → `wait()` 回收（Windows 下必须先回收才能删 exe）。
* 日志：`sanitize_api_log()` 对敏感行整体省略、手机号打码、去掉 ANSI、截断 1000 字符（`:58-63`）。

管理员命令：`/网易云删除API`、`/网易云重装API` 会**持久化改写 `ncm_api_embedded` 开关**（`_persist_embedded`，`main.py:623-638`），失败会回滚配置且不删文件（有测试覆盖）。

### 4.6 渲染

`core/renderer.py` 是纯 Pillow 绘制，无 HTML/浏览器依赖（部署简单，代价是样式靠手算坐标）。

* 字体：`_FONT_CANDIDATES` 覆盖 Linux/macOS/Windows 常见 CJK 字体，`glob` 找到即缓存到模块级 `_font_path`（`:29-75`）；找不到时 `ImageFont.load_default()`（中文会变方块，见 §7）。
* `_make_disc()`（`:123-238`）是视觉核心：圆形裁切 + 斜向彩虹色带 + 液态玻璃盘毂（模糊基底 + 提亮层 + 月牙高光 + 底部暗色 + 边缘折射）+ 银色内圈 + 中心孔。改动这里请同时看播放卡/选歌图两处调用。
* `_save()`（`:264-282`）文件名带 8 位随机串，解决"多人同时点歌互相覆盖"；每类保留最新 12 张。
* 文字溢出：`_ellipsize` / `_wrap_text` 按像素宽度处理，不依赖字形数量。

### 4.7 数据清理策略

| 对象 | 清理方式 | 位置 |
| --- | --- | --- |
| 选歌候选 | 5 分钟过期（读时惰性删除）+ 清理命令 | `main.py:183-194` |
| 卡片图 | 每类保留 12 张，渲染时顺带删旧 | `renderer.py:270-281` |
| 音频 | 发送后 `call_later(120)` 删除；下载失败/取消立即删 | `main.py:249-259, 370-375` |
| 登录二维码 | 发送后 240s 删除；新二维码替换旧的 | `main.py:995-1001, 1027` |
| 内置 API | `/网易云删除API` 删除整个 `ncm_api_server/` | `ncm_server.py:352-361` |
| 手动 `/网易云清理缓存` | 跳过正在下载、当前二维码、符号链接 | `main.py:581-607` |

---

## 5. 关键设计决策（改代码前先理解"为什么"）

| 决策 | 原因 | 如果改掉会怎样 |
| --- | --- | --- |
| `DummyCookieJar` 禁用会话 Cookie | 自动 CookieJar 与手动头叠加会产生重复/过期 `MUSIC_U`，VIP 歌偶发变试听 | 会员鉴权时好时坏，极难复现 |
| 凭据放 POST body + 规范化 Cookie 头 | 上游严格分隔符解析会丢 `MUSIC_U`；body 里的对象不受字符串规则影响 | 扫码成功但一直"未登录" |
| `X-Apicache-Bypass` + 随机 nonce/timestamp | 上游按完整 URL 做 apicache，旧响应会缓存 30 秒试听地址 | 登录后仍然只拿到试听 |
| `generation` 计数器 | 登录/复核/重装是并发长任务，旧结果不能覆盖新状态 | 换号后显示旧账号，重装后旧响应回写 |
| `iter_play_info` 用生成器而非返回列表 | 只有调用方知道"下载是否真的成功"，回退必须发生在下载之后 | 取得坏链接即终止，镜像/外链永远用不上 |
| 下载用 `sock_read` 停滞超时而非总超时 | 无损/母带 30-100MB，慢速网络下总超时会误杀正常下载 | 大文件永远失败 |
| 二进制不进仓库、按需下载 | v1.5.0 从 Git 历史移除约 221MB 二进制（`git-filter-repo`） | 仓库体积与克隆时间暴涨 |
| 子进程独立 `TMP/TEMP/TMPDIR` | 上游把 `anonymous_token` 等写系统临时目录，无法准确重置 | 删除/重装后残留旧身份 |
| 选歌 key = 群 + 用户 | 同群两人同时点歌会串台（v1.4.10 修复） | 回复序号播错人的歌 |
| 渲染图带随机文件名 | 并发渲染覆盖（v1.4.10 修复） | 用户看到别人的封面 |
| aiocqhttp 走原生 API 传路径 | `Record` 被适配器转 base64，撑爆 WebSocket | 大音频发送直接断连 |

---

## 6. 代码水平评估

### 6.1 做得好的地方（这些应该保留）

1. **失败路径比成功路径写得更认真**。音源回退、下载重试、发送降级、登录复核失败分支，都有明确文案和状态，不是 `except: pass` 了事。全仓库 0 处裸 `except:`。
2. **边界数据治理到位**。Cookie 属性污染、游客 Cookie、`freeTrialInfo` 的字符串 `"null"`、`account/profile` UID 不一致、`path` 符号链接、压缩包内二进制无执行权限——这些都是踩过坑之后写进去的防御，且大多有对应回归测试（`tests/test_auth_playback.py`）。
3. **资源生命周期清晰**。`_active_downloads` / `_active_qr_path` 保护"协议端还在读"的文件；取消下载在 `finally` 里删半成品；子进程先回收再删文件；配置保存失败回滚且不误删。
4. **测试是"真 HTTP"测的**，不是 mock 掉 aiohttp：本地起 aiohttp server，断言真实请求头（含 `Cookie`、`X-Apicache-Bypass`、`_ncm_nonce` 唯一性），这能抓住协议层回归，比纯 mock 值钱。
5. **没有把宿主依赖散落各处**：只有 `main.py` 顶部 import AstrBot；`core/` 里仅 `logger` 依赖 `astrbot.api`，整体可移植性好。
6. **配置/日志安全有意识**：诊断不打印 Cookie 值、手机号打码、API 输出脱敏、日志开关默认关闭。

### 6.2 不足（按严重度）

* **P0（先修）**：渲染同步阻塞事件循环、无并发/频控、`pending` 无清扫、版本号多处重复、无 CI。
* **P1（一个迭代内）**：`main.py` 1073 行门面过重、内置服务无看门狗、诊断调用私有方法、`_manage_api` 长锁、测试对 renderer/sender/登录流程覆盖为 0、依赖未锁版本。
* **P2（长期）**：`ncm_api.py` 混合了传输/模型/回退/复核/登录五类职责、无类型检查、无结构化日志与指标、无本地音频缓存、无 i18n、二进制升级靠手改摘要。

客观地说：**这份代码在"个人维护的 AstrBot 插件"里属于偏上水平**——领域知识密度高、故障复盘写得清楚（CHANGELOG 可当事故档案读）、并发与凭据的坑基本都填了。它的问题不是"写得糙"，而是**长出来的**：功能一层层加，`main.py` 和 `ncm_api.py` 成了两个"什么都放"的文件，工程化设施（CI/类型/缓存/限流）没跟上。

---

## 7. 已知问题清单（可直接开 issue）

### P0 · 影响线上稳定性/体验

| # | 位置 | 问题 | 影响 | 建议 |
| --- | --- | --- | --- | --- |
| P0-1 | `main.py:213, 268, 363`；`renderer.py` 全部 | Pillow 渲染（CD 盘面每张要画渐变、模糊、多层合成）**同步**跑在事件循环里 | 单次渲染几十到几百 ms，期间整个 AstrBot 的所有插件都被卡住 | `await asyncio.to_thread(renderer.render_selection, ...)`；renderer 保持同步 API |
| P0-2 | `main.py:562`、`:323` | `/直接点歌`、关键词点歌无任何频控；下载无并发上限 | 一人刷屏即可占满带宽/磁盘，其他人点歌全部超时 | 每用户/每群滑动窗口限流 + `asyncio.Semaphore` 限制同时下载数 |
| P0-3 | `main.py:94, 183-194` | `pending` / `_waiting` 只在**同一 key 再次访问**时才删过期项 | 用户搜完不回复，条目永久驻留，长期运行缓慢涨内存 | 加周期性清扫任务（或在 `_search_and_render` 时顺带清理） |
| P0-4 | `main.py:61`、`metadata.yaml:4`、`README.md:5`、`tests/test_plugin.py:217` | 版本号 4 处重复，且测试硬编码 `v1.5.0` | 每次发版必改 4 处，漏一处就"版本不一致" | 单一来源（读 `metadata.yaml` 或新建 `version.py`），`main.py` 与测试都引用它 |
| P0-5 | 仓库根目录 | 无 `.github/workflows` | lint/测试靠人自觉，回归在用户机器上发现 | 加 CI：3 OS × Python 3.10-3.13，跑 ruff + unittest |

### P1 · 架构与可维护性

| # | 位置 | 问题 | 影响 | 建议 |
| --- | --- | --- | --- | --- |
| P1-1 | `main.py` 全文（40 个函数） | 门面同时承担：三条入口、选歌状态、下载编排、发送编排、6 个管理员命令、扫码/短信两套登录状态机 | 改登录易碰点歌；单文件 1000+ 行，diff 噪音大 | 拆 `handlers/`（play / admin / login）或 AstrBot 多模块注册；状态机抽 `LoginFlow` 类 |
| P1-2 | `core/ncm_api.py`（819 行） | 传输 + 模型 + 回退 + 复核 + 登录接口混在一处 | 难单独测试与替换音源 | 拆 `models.py` / `transport.py` / `playback.py` / `account.py` / `login.py` |
| P1-3 | `ncm_server.py:242-243, 279` | 启动后无看门狗；子进程若崩溃，`state` 仍停在"运行中" | `/网易云诊断` 显示运行中但实际已死，点歌持续失败 | 周期性 `poll()` 检查 + 自动重启（指数退避）+ 状态改"已退出" |
| P1-4 | `main.py:690` | 诊断命令调用私有 `self.api._api_request()` | 封装泄漏，重构即断 | `NetEaseAPI.version(base)` 公开方法 |
| P1-5 | `main.py:643-670` | `_manage_api` 在 `_service_lock` 内做分钟级下载/重装 | 期间 `terminate()`（AstrBot 关闭）会被阻塞；`_probe_accounts` 也等锁 | 锁只保护状态切换，下载在锁外；`terminate` 加超时 |
| P1-6 | `tests/` | renderer、sender、`listen_keyword`、`pick_by_number`、`/点歌` waiter、验证码流程 **0 覆盖**；`sender.send` 在测试里被 AsyncMock 掉 | 视觉回归、发送降级、选歌交互改错无人发现 | 见 §8 的测试计划 |
| P1-7 | `requirements.txt` | `aiohttp`、`Pillow` 无版本约束 | 上游大版本破坏（如 aiohttp 4）直接炸 | `aiohttp>=3.9,<4`、`Pillow>=10,<12` |
| P1-8 | `CHANGELOG.md` | 缺 v1.4.8、v1.4.9 条目（git log 里有对应提交），且 1.4.10 排在 1.4.7 之前 | 交接时无法按版本回溯 | 补历史 + 统一格式（Keep a Changelog） |
| P1-9 | `main.py:105`、`:723` | 仅保存最后一次点歌描述，无历史 | 偶发问题无法追溯（"昨天那首歌为什么走了外链"） | 环形缓冲（最近 20 条，含 song_id/来源/耗时/失败原因） |

### P2 · 一致性/细节

| # | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| P2-1 | `ncm_api.py:346` | `trace.fee = int(data.get("fee") or trace.fee)`：`fee=0` 是假值，无法把之前的 1/4 归零 | 用 `if "fee" in data` 显式赋值；影响仅播放卡 VIP 徽标 |
| P2-2 | `ncm_api.py:772` | `fetch_bytes` 直接用 `self.proxy`，未走 `_proxy_for`（与下载不一致） | 封面/头像也是公网，建议统一走 `_proxy_for` |
| P2-3 | `main.py:967, 1009`、`sender.py:64` | QR 写盘、Cookie 落盘、base64 读整文件是同步 IO | 小文件影响有限；base64 大文件建议 `to_thread` |
| P2-4 | `renderer.py:75` | 找不到 CJK 字体时 `load_default()` → 中文显示为方块 | 探测失败时降级为纯文字列表（`main.py:216-220` 已有类似逻辑，可复用） |
| P2-5 | `core/ncm_api.py:196, 396-420`；`main.py:330-346` | 超时是串行叠加的：HTTP 总超时 15s ×（2 凭据 × 2 服务）+ 网页阶段 + 每个候选下载 2 次重试（含 3s 退避），最坏会**超过** LLM 工具 60s 预算 | 给整条链路加总预算（如 35s），超时直接跳到镜像/外链 |
| P2-6 | `main.py:378-381` | 热评/歌词在主流程后**串行**附加（各自最多 15s） | 改为并发 `gather`，或音频发送后异步补发 |
| P2-7 | `main.py:110, 229, 242` | 配置值直接 `int()`，无范围校验（端口、超时、上限） | 启动时集中校验并给出可读错误；非法值用默认值 |
| P2-8 | 仓库根 `logo.png` | 1.9MB，代码未引用（推测供插件市场展示） | 压缩到 200KB 内，或移到 `assets/` |
| P2-9 | `tests/support.py:80-119` | 自造 AstrBot 替身 → 与真实宿主 API 漂移不可见（`register` 签名、`session_waiter` 语义、`filter.llm_tool`） | 增加"真实宿主冒烟"文档/手动 checklist；锁定 AstrBot 版本区间 |
| P2-10 | `main.py:14` | `from astrbot.core.utils.session_waiter import ...` 是宿主内部路径 | 升级 AstrBot 时优先检查此处；可加 try/except 降级为无 waiter 模式 |
| P2-11 | `ncm_server.py:21-40` | 升级上游版本需手改 tag + 3 个 (size, sha256) | 写 `tools/fetch_digests.py` 自动生成 |
| P2-12 | 全仓库 | 46 处 `except Exception`，多数是合理兜底，但没有错误分类/计数 | 引入少量异常类型（`SourceError`/`AuthError`）或至少结构化日志字段 |

---

## 8. 未来改进路线图

> 排序原则：先"不修就会出事"，再"改起来收益最大"，最后"锦上添花"。括号内为粗略投入。

### 8.1 短期（1-2 周，建议接手第一件事）

| 目标 | 方案 | 涉及 | 工作量 |
| --- | --- | --- | --- |
| 工程化基线 | GitHub Actions：`ruff check` + `ruff format --check` + `unittest`（ubuntu/windows/macos × py3.10/3.13） | `.github/workflows/ci.yml` | 0.5d |
| 版本单一来源 | 新增 `version.py`（或 `importlib.metadata`），`main.py`/README/测试引用同一值；测试改为"三者相等"而非硬编码 | `version.py`、`main.py:57-62`、`tests/test_plugin.py:209` | 0.5d |
| 渲染不阻塞 | 所有 `renderer.render_*` 调用包 `asyncio.to_thread` | `main.py:213, 268, 363` | 0.2d |
| 频控与并发闸门 | 每用户 10s/次 + 每群令牌桶；`asyncio.Semaphore(3)` 包住 `_download` | `main.py` 新增 `RateLimiter` | 1d |
| 状态清扫 | `pending`/`_waiting` 定时清理（`asyncio` 周期任务，随 `terminate` 取消） | `main.py:94`、`:166` | 0.3d |
| 依赖锁定 | 版本区间写进 `requirements.txt` | 根目录 | 0.1d |
| 文档补洞 | CHANGELOG 补 1.4.8/1.4.9、排序修正；README 加"故障排查"小节 | 文档 | 0.5d |

### 8.2 中期（1-2 个月）

1. **同曲去重 + 本地音频缓存（收益最高）**
   现状：同一首歌反复点，每次都重新下载（`main.py:226-245`）。
   方案：cache key = `song_id + level + ext`，写 `<cache>/audio/<song_id>_<level>.<ext>`，命中且 TTL（如 6h）内直接复用；总容量 LRU（如 2GB）后台淘汰；发送侧仍走 120s 延迟删除，但缓存副本独立存在（或引用计数）。
   风险：磁盘占用、版权敏感；配置项 `audio_cache_enabled` / `audio_cache_max_mb` 默认关闭更稳妥。

2. **拆分 `main.py` / `ncm_api.py`**
   建议目录：

   ```
   core/
     models.py      # Song / PlayInfo / PlaybackTrace / Comment / AccountState
     transport.py   # ClientSession、代理、Cookie 头、_api_request、download
     playback.py    # 音源回退链（Source 协议：iter_candidates）
     account.py     # probe/membership
     login.py       # qr / captcha 状态机
     content.py     # comment / lyric / search / detail
   handlers/
     play.py  admin.py  login.py
   ```

   音源设计成可插拔 `Source`（`async def iter_candidates(song, quality) -> AsyncIterator[PlayInfo]`），未来加"UnblockNeteaseMusic 镜像""QQ 音乐""酷狗"只需新增一个 Source，不改主流程。

3. **内置服务看门狗**
   `asyncio` 每 30s `process.returncode` 检查；非 0 则重启（指数退避，最多 3 次），状态文案改为"运行中/已退出/重启中"；诊断显示重启次数与最后错误。

4. **超时预算与"先回执后补发"**
   为整条点歌链路加 `total_budget`（默认 35s）；热评/歌词改成发送音频后 `create_task` 异步补发，避免把 LLM 工具 60s 预算吃满。LLM 工具返回"已开始下载"而不是等全部完成。

5. **测试补齐（按性价比排序）**
   - `sender`：用假 bot 对象验证 4 级降级顺序、url/base64 三种 load_mode、超时；
   - `listen_keyword` / `pick_by_number`：表格驱动（触发词位置、叠词、疑问句、长度、`/` 开头、群友抢答）；
   - `renderer`：至少断言输出尺寸、文件存在、12 张保留策略、无封面时仍可出图（像素级比对可选）；
   - `_captcha_flow`：注入可控 `session_waiter` 替身，覆盖冷却、格式错误、两次失败、generation 失效；
   - 覆盖率门槛：先设 55%，逐季提高。

6. **结构化日志与历史**
   统一 `logger.info("[ncm_player] action=play song_id=%s source=%s level=%s ms=%d")`；`_last_playback` 换环形缓冲 20 条；`/网易云诊断` 增加"最近 5 次点歌"。

### 8.3 长期（按需）

* **插件 WebUI 页**：扫码/账号状态/缓存管理做成设置页（AstrBot 4.x 支持插件页面），比在聊天里发命令体验好得多；二维码可在页面实时刷新。
* **群级策略**：每群开关、黑名单/白名单、每日额度、音质上限（防有人点母带把磁盘打满）、静默时段。
* **播放队列与歌单**：`/点歌 歌单 <id>`、`/队列`、`/跳过`、`/收藏`；需要引入每群播放器状态机（注意与现有"点完即发"模型兼容，建议默认关闭）。
* **歌词增强**：逐句歌词图 / LRC 文件发送 / 卡拉OK 式时间轴渲染。
* **可观测性**：计数器（搜索数、点歌数、各来源成功率、平均耗时、下载字节），暴露 `/网易云统计`；便于判断"镜像是否值得保留"。
* **多平台音源抽象**：把 `iter_play_info` 抽象成 Source 链后，接 QQ/酷狗/咪咕，或接 `UnblockNeteaseMusic` 解决灰色歌曲。
* **i18n**：当前全中文硬编码；若要进 AstrBot 插件市场面向海外，抽 `messages.py` 或 AstrBot i18n 机制。
* **类型检查**：渐进式引入 mypy（先 `core/`，`ignore_missing_imports`），CI 只检查新增/修改文件。

### 8.4 如果只给你一周

按这个顺序做，收益最大：**CI → 版本单一来源 → 渲染 to_thread → 频控+并发闸门 → pending 清扫 → CHANGELOG 补齐**。这六项不改变架构，风险低，但能把"线上偶发卡顿/内存缓涨/发版漏改"三类问题一次性压住。

---

## 9. 开发规范与发布流程

### 9.1 提交前必须

```bash
python -m ruff check .            # 必须 0 告警
python -m ruff format --check .   # 必须无 diff（或直接 ruff format .）
python -m unittest discover -s tests -t . -v
```

涉及内置服务的改动，额外跑一次真实冒烟（联网）：

```bash
NCM_RELEASE_SMOKE=1 python -m unittest tests.test_release_smoke -v
```

### 9.2 发版清单

1. 改版本号：`main.py` 的 `register(...)`、`metadata.yaml`、README 徽章、`tests/test_plugin.py` 的断言（P0-4 修好后只剩一处）。
2. 写 CHANGELOG（格式对齐 Keep a Changelog，新条目在最上）。
3. 若升级内置 API：改 `RELEASE_TAG` + `ASSET_DIGESTS` 三个 (size, sha256)，跑真实冒烟。
4. 跑 §9.1 全绿后打 tag；tag 与 `metadata.yaml` 版本一致。
5. 提醒用户：v1.5.0 重写过 Git 历史，老克隆需重新 clone（README 已注明）。

### 9.3 代码约定（沿用现状，别引入新风格）

* 中文注释/日志/用户文案；错误信息要能指导用户下一步（"请检查端口/权限/网络"），而不是只抛异常名。
* 异步优先：网络用 aiohttp，CPU/阻塞用 `asyncio.to_thread`（目前 renderer 还没做到，见 P0-1）。
* 用户可见文案不要回显 Cookie、二维码令牌、手机号；日志同理（`_mask_name`、`sanitize_api_log`）。
* 新增配置项必须同时改：`_conf_schema.json`（含 hint）、README 配置表、`main.py` 读取处默认值。
* 新增音源必须遵守：只有调用方能判断成败，失败要 `trace.record()`，绝不吞掉后假装成功。

---

## 10. 交接清单（Checklist）

- [ ] 能本地跑通 `ruff` + `unittest`（23 项，1 skip）。
- [ ] 有一台能访问 GitHub/网易云的机器，跑过一次 `NCM_RELEASE_SMOKE=1`（确认真实二进制生命周期）。
- [ ] 有一个网易云账号（最好同时有 VIP 与普通账号各一）用于验证音质/VIP 徽标。
- [ ] 有一个测试 QQ 群 + NapCat/aiocqhttp 协议端（验证 `load_mode=file` 的原生路径发送）。
- [ ] 明确谁持有 GitHub 仓库与 Release 发布权限、AstrBot 插件市场的发布账号。
- [ ] 读一遍 CHANGELOG 的 v1.4.2～v1.5.0（那几版全是真实事故复盘，等价于设计文档）。
- [ ] 确认当前部署实例的数据目录位置与备份策略（`ncm_cookie.txt` 丢了要重新扫码）。

---

## 11. 附录

### 11.1 术语

| 术语 | 含义 |
| --- | --- |
| NCM | NetEase Cloud Music，网易云音乐 |
| 内置 API / 外部 API | 插件自己下载并托管的 NeteaseCloudMusicApiEnhanced 二进制 / 用户自建的服务 |
| apicache | 上游按 URL 缓存响应；本项目用 `X-Apicache-Bypass` + 随机 nonce 规避 |
| 游客态 | 只有 `MUSIC_A` 无 `MUSIC_U` 的匿名会话；必须拒绝保存 |
| 试听 | `freeTrialInfo` 非空，代表只返回 30s 片段；不算成功 |
| 档位 | standard / higher / exhigh / lossless / hires / jymaster，只会向下回退 |
| generation | 账号/服务快照代数，用于丢弃过期的异步结果 |

### 11.2 关键常量

| 常量 | 值 | 位置 |
| --- | --- | --- |
| `PENDING_EXPIRE` | 300s | `main.py:28` |
| `LYRICS_PER_NODE` | 30 行/条 | `main.py:29` |
| `INVALID_NOTICE_INTERVAL` | 600s | `main.py:30` |
| `CAPTCHA_COOLDOWN` | 60s | `main.py:31` |
| 扫码轮询 | 120 次 × 1.5s | `main.py:1033-1034` |
| 验证码会话超时 | 180s | `main.py:826` |
| `/点歌` 等待 | 120s | `main.py:517` |
| 音频清理延迟 | 120s | `main.py:373-375` |
| 二维码清理延迟 | 240s | `main.py:1027` |
| 登录复核防抖 | 60s | `ncm_api.py:613` |
| 下载停滞超时 | 配置，默认 20s（连接 10s） | `ncm_api.py:794-796` |
| 内置 API 就绪超时 | 60s | `ncm_server.py:293` |
| 内置 API 固定版本 | `v4.40.1` | `ncm_server.py:21` |
| 卡片保留数量 | 每类 12 张 | `renderer.py:275` |

### 11.3 排障速查

| 现象 | 先看哪里 |
| --- | --- |
| 点歌只回链接 | `_last_playback`（`/网易云诊断` 末行）→ 逐级回退原因 |
| 一直提示未登录 | 是否配置了 API；`/网易云诊断` 的"核验来源/结果"；Cookie 是否含 `MUSIC_U` |
| VIP 歌只有 30 秒 | 档位/账号会员；是否命中上游缓存；`ncm_api.py:320` 的试听判定 |
| 大音频发送失败 | `load_mode` 是否为 `file`；协议端与 AstrBot 是否同机；`send_timeout` |
| 内置服务启动失败 | `/网易云诊断` 的"最近启动错误"与最近输出；端口占用；加速站/代理 |
| 卡片中文变方块 | 系统缺 CJK 字体（`renderer.py:29-49`） |
| 重载插件卡住 | 是否在 `initialize` 里同步等待了网络/进程（当前已改为后台任务，回退需注意） |

### 11.4 相关文档

| 文档 | 用途 |
| --- | --- |
| `README.md` | 用户安装/配置/命令说明 |
| `CHANGELOG.md` | 版本历史与事故复盘（v1.4.2 起信息量最大） |
| `_conf_schema.json` | 配置项权威定义（含 hint） |
| `tests/support.py` | 宿主替身与测试夹具，改测试先看这里 |
| 上游 [api-enhanced v4.40.1](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced/releases/tag/v4.40.1) | 内置服务行为与接口语义 |
| 设计来源 [astrbot_plugin_music](https://github.com/Zhalslar/astrbot_plugin_music) | 整体思路参考（README 已致谢） |

---

*本文由代码静态阅读 + 实际运行 lint/测试得出，行号对应当前快照。改动代码后行号会漂移，建议以函数名为锚点搜索。*
