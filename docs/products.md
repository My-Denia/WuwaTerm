# WuwaTerm 产品入口

WuwaTerm 可以按需要选择 Telegram、Windows 或 Web。核心仓库负责共享词典、翻译与审校逻辑、HTTP API、数据构建验证以及通用部署恢复工具。各产品独立维护源码和发布；[机器可读索引](../contracts/products.json)记录了截至 2026-11-15 的源码、制品和接口快照，四个仓库的正式版 tag、commit 和资产 SHA-256 都在其中。要安装新版本，请再核对各仓库的发布页与说明。

## 选择使用方式

- **只用 Telegram**：使用 [Telegram 安装与运行指南](https://github.com/My-Denia/WuwaTerm-Telegram/blob/da61ab136a7dde7424f5011e7ac17f011a5840b7/README.md)，准备 bot token、单独构建的术语数据库和指南要求的 Core wheel；不需要运行 HTTP API。当前 `0.3.0` 已正式发布，`core-lock.json` 固定的就是 Core `v0.9.0` 发布 wheel（`8435cb3b…`），[制品校验说明](https://github.com/My-Denia/WuwaTerm-Telegram/blob/da61ab136a7dde7424f5011e7ac17f011a5840b7/docs/artifact-verification.md)解释如何校验。
- **只用 Windows 客户端**：从 [Windows v0.4.2 发布页](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.2)下载并核对便携包；[使用与构建指南](https://github.com/My-Denia/WuwaTerm-Windows/blob/59b8a5f8eeff2afc947874ab20c7e59f44734348/README.md)列出了操作步骤。稿件可离线编辑、保存；在线查词、翻译和审校需要运营方提供的兼容 API 地址及设备令牌。程序未签名。
- **使用 Web 和 API**：公共工作台入口及使用说明见 [Web 产品指南](https://github.com/My-Denia/WuwaTerm-Web/blob/9ab24c4a45ced85eb867e8081767c40a80dd9b41/docs/sites.md)。自托管时先按 [Core 自托管指南](self-hosting.md)和[数据构建指南](data-refresh.md)准备 API 与词典，再按 Web 仓库自己的说明配置 Node 运行环境、设备令牌和带认证的 HTTPS 反向代理。根路径与路径前缀需要对应构建，[Web v0.3.0 发布页](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.3.0)分别提供 root 与 `/private` 两套包，`v0.2.0`、`v0.1.1` 与 `v0.1.0` 的包仍可下载。同一工作台支持公共和自托管方式，Sites 是托管目标。
- **一条命令安装 Core（可选加 Telegram 或 Web）**：在 Core 检出目录运行 `./deploy/setup.sh` 只启动共享翻译服务；`--with-telegram` 不再安装 Core 检出里的旧 bot，它会定位独立的 Telegram 产品、按本机路径输出该产品自己的安装步骤，并在给出 `--telegram-dir` 时把那本检出的 `.env`（只含 bot 自己的设置）和 `data/terms.db` 副本准备好；旧的内联 bot 保留在 `--with-legacy-bot` 之下：`./deploy/setup.sh` 默认不启动它，Compose 文件里仍保留该服务（`deploy/product-update.sh` 按服务名解析已装服务，放在 profile 后会让更新路径看不到它，因此不改 Compose 结构）。`--with-web` 在首次安装或已有服务上增量添加 Web；`--status`/`--check` 查看组件、数据版本与健康，两者分别报告独立 Telegram 产品（`telegram`）与旧内联 bot（`legacy bot`）。安装器默认使用官方预构建镜像、缺库时构建词典、等待就绪并签发首个设备令牌，详见 [自托管指南](self-hosting.md#one-command-deploysetupsh)。`--with-web` 直接读取 WuwaTerm-Web `v0.3.0` 已发布的 `manifest-{root|prefix}.json` 与 `SHA256SUMS-{root|prefix}`，校验并解包 `archive.name` 指向的归档，运行其中的 `bundle/start.mjs`；`--web-origin` 提供反向代理对外的 HTTPS 源，就绪检查会带 `X-WuwaTerm-Edge` 请求页面与 `/api/pool`。Web 的 SQLite 准入库由 `WUWATERM_LOCAL_DB_PATH` 决定，默认 `/var/lib/wuwaterm-web/pool.sqlite3`，宿主的 `state-web/data/` 挂到该文件所在目录，不落在只读的 `/app` 包里，所以 Docker 不需要在只读挂载下创建挂载点；旧 `web.env` 写的 `/app/data/pool.sqlite3` 仍可用，安装器会先在解包目录里建好挂载点，相对路径则直接拒绝。本地包可用 `--web-package`。只有真的拿到部署指针和它指向的 manifest 才算已接管，干净安装上的 `adopt` 失败会如实报告为未接管，并在下次运行时重试。

当前 Core 源码仍保留旧 Telegram 和私有 Python Web 入口；独立产品的源码合并与替代交付分开推进，替代交付以各产品的部署记录为准。迁移状态文件时，先按各产品指南备份和核对。

## 版本与兼容

下表中的“已合并”指该日期观察到的仓库 main 源码；发布和实际采用分别记录。候选制品是 CI 工件，正式版另按发布页登记；历史生产服务与本次新源码的部署各自记录。

| 产品 | 已合并源码快照 | 独立发布状态 | 本次新交付的实际采用 |
| --- | --- | --- | --- |
| Core / API | `d54957c`，源码版本 `0.9.0` | [v0.9.0](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.9.0) 已发布（wheel `8435cb3b…`，sdist `b7762e9c…`，manifest 与 SHA256SUMS 齐全），来源 `d54957c`，运行时镜像 `sha256:da581ce6…` 与构建器 `sha256:d7fb9bb4…` 已按 `v0.9.0`、`0.9` 重新打标；规则审校在句数不一致时按段落判定词条（匹配器标签 `wuwaterm-cjk-matcher-v7`），词库补入孤立区域标题与四阶物品基础名（中文文本中仅作参考），翻译请求新增按方向的思考设置（默认与原请求一致），Web 与 Windows 契约 pin 移到稿件 v3；[v0.8.7](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.8.7) 与更早版本仍保留 | 已采用：API 0.9.0（2026-11-16 完整更新，部署源 `d54957c`，`current` 为 `r4`，`r3` 保留；词库 12,089 条，较 0.8.6 新增 35 条、无改动与删除；数据 pin `9218d612` 不变；`/healthz`、`/readyz` 返回 200，OpenAPI 为 0.9.0，重启 0；旧内置 Bot 保持停止 `created` 态；翻译思考设置未配置，保持默认 `omit`） |
| Telegram | `da61ab13`，源码版本 `0.3.0` | [v0.3.0](https://github.com/My-Denia/WuwaTerm-Telegram/releases/tag/v0.3.0) 已发布（wheel `c2924d03…`，sdist、bundle、镜像与安装探针齐全），来源 `da61ab13`，pin 到 Core `v0.9.0`（wheel `8435cb3b…`），并收敛审校去重在 TTL、重复、投递与 `/review last` 上的边界；`v0.2.10` 与更早版本仍保留 | 已采用：独立 Telegram 0.3.0 + Core 0.9.0（2026-11-16；以独立 `wuwaterm-telegram` 容器运行，镜像 revision `da61ab1`，`getMe` 成功，重启 0，日志无 Conflict；旧内置 Bot 保持停止） |
| Windows | `910b1a8`，源码版本 `0.4.2` | [v0.4.2](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.2) 已发布（zip `824b694c…`，manifest 与 SHA256SUMS 齐全），来源 `14f0c36`，修复空状态卡片行高与查词 Tab 顺序；[v0.4.1](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.1)（zip `4d248a0e…`）与 `v0.4.0`、`v0.3.0` 仍保留。main 含 #19、#20（manuscript v3），源码版本仍为 `0.4.2`，尚未发布；CI 契约 pin 为 `e8880eb2` | 150% DPI 原生与离屏 `--self-check` 已通过；人工交互视觉、多屏混合 DPI 拖窗与输入法上屏没做 |
| Web | `9ab24c4`，源码版本 `0.3.0`（已发布） | [v0.3.0](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.3.0) 是最新 GitHub 发布，来源 `9ab24c4`，首个读写 manuscript v3 的 Web 发布；发布前 root 与 `/private` 候选已对 Core `v0.9.0` 重放。`v0.2.0`、`v0.1.1` 与 `v0.1.0` 仍在发布页上；CI 契约 pin 为 `3b9faa1c`，是 `v0.3.0` 的祖先提交 | Sites 站点没更新到 v0.3.0，仍是 saved version 30（2026-11-13，状态 `succeeded`，源 `5aabe80` / Web `v0.2.0`；根路径 <https://wuwaterm.denia-official.chatgpt.site> 返回 HTTP 200） |

四个正式版资产的 SHA-256 按发布页逐个记录在[索引](../contracts/products.json)。Web 发布包里 `archive.name` 指向归档，入口是 `bundle/start.mjs`，路径前缀记在 manifest 与 `bundle/BUILD-INFO.json`；`deploy/setup.sh` 按这份真实结构安装和检查，不再要求早期设想中实际并不存在的 `start`、`port`、`health_path` 字段（本地包若提供仍会采用）。

Windows 与 Web 的在线功能使用 Core `/v1` API；稿件审校需要 `review-v2`，两端稿件文件格式为 `wuwaterm-manuscript-v1`、`v2` 和 `v3`：`v1`、`v2` 保持原义，带按段落记录的长稿件使用 `v3`。`v3` 的段落术语结果只有在内容哈希、审校依据和请求输入都与一次实时 `review-v2` 依据一致时才复用，其余导入内容仍是历史，不当作新检查。`v3` 已随 Web v0.3.0 发布；Windows 的 main 已含 `v3`，尚未随 Windows 正式版发布。API 版本相同本身不足以证明审校或私有 Web 可用：私有 Web 还要求 `GET /v1/auth/status`，其代理也严格校验响应结构。Core 同时支持 `review-v1` 和 `review-v2`，默认是前者。Telegram 直接使用固定版本的 Core 包和 schema `2` 术语数据库；它的聊天设置是 JSON，频道回复索引版本为 `1`。API 设备库和私有 Web 的准入计数库各自独立于术语数据库，后者当前为 schema `1`。具体组合和 CI 固定版本见[索引](../contracts/products.json)，不要将某一组契约测试推广到所有版本组合。

Windows 现代界面已实现并通过 150% DPI 原生与离屏 `--self-check`；人工交互视觉、多屏混合 DPI 拖窗与输入法上屏没做。发布四个正式版与部署线上服务分别记录。

四个仓库共用 [WuwaTerm GitHub Project](https://github.com/users/My-Denia/projects/1)；现有 Linear WuwaTerm 项目仍是工作跟踪的权威来源。
