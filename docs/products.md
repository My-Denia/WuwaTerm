# WuwaTerm 产品入口

WuwaTerm 可以按需要选择 Telegram、Windows 或 Web。核心仓库负责共享词典、翻译与审校逻辑、HTTP API、数据构建验证以及通用部署恢复工具。各产品独立维护源码和发布；[机器可读索引](../contracts/products.json)记录了截至 2026-11-09 的源码、制品和接口快照，四个仓库的正式版 tag、commit 和资产 SHA-256 都在其中。要安装新版本，请再核对各仓库的发布页与说明。

## 选择使用方式

- **只用 Telegram**：使用 [Telegram 安装与运行指南](https://github.com/My-Denia/WuwaTerm-Telegram/blob/86a927c4bce20a434bf913ea46c229c9b3846e32/README.md)，准备 bot token、单独构建的术语数据库和指南要求的 Core wheel；不需要运行 HTTP API。当前 `0.2.0` 已正式发布，`core-lock.json` 固定的就是 Core `v0.8.0` 发布 wheel（`06233748…`），[制品校验说明](https://github.com/My-Denia/WuwaTerm-Telegram/blob/86a927c4bce20a434bf913ea46c229c9b3846e32/docs/artifact-verification.md)解释如何校验。
- **只用 Windows 客户端**：从 [Windows v0.4.0 发布页](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.0)下载并核对便携包；[使用与构建指南](https://github.com/My-Denia/WuwaTerm-Windows/blob/3b04b28d62bc22613a05f44f8d040d1a783f237a/README.md)列出了操作步骤。稿件可离线编辑、保存；在线查词、翻译和审校需要运营方提供的兼容 API 地址及设备令牌。程序未签名。
- **使用 Web 和 API**：公共工作台入口及使用说明见 [Web 产品指南](https://github.com/My-Denia/WuwaTerm-Web/blob/31fdc14b1a14563990d0acaf89dc302f024cebdc/docs/sites.md)。自托管时先按 [Core 自托管指南](self-hosting.md)和[数据构建指南](data-refresh.md)准备 API 与词典，再按 Web 仓库自己的说明配置 Node 运行环境、设备令牌和带认证的 HTTPS 反向代理。根路径与路径前缀需要对应构建，[Web v0.1.1 发布页](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.1.1)分别提供 root 与 `/private` 两套包，`v0.1.0` 的包仍可下载。同一工作台支持公共和自托管方式，Sites 是托管目标。
- **一条命令安装 Core（可选加 Telegram 或 Web）**：在 Core 检出目录运行 `./deploy/setup.sh` 只启动共享翻译服务；`--with-telegram` 不再安装 Core 检出里的旧 bot，它会定位独立的 Telegram 产品、按本机路径输出该产品自己的安装步骤，并在给出 `--telegram-dir` 时把那本检出的 `.env`（只含 bot 自己的设置）和 `data/terms.db` 副本准备好；旧的内联 bot 保留在 `--with-legacy-bot` 之下：`./deploy/setup.sh` 默认不启动它，Compose 文件里仍保留该服务（`deploy/product-update.sh` 按服务名解析已装服务，放在 profile 后会让更新路径看不到它，因此不改 Compose 结构）。`--with-web` 在首次安装或已有服务上增量添加 Web；`--status`/`--check` 查看组件、数据版本与健康，两者分别报告独立 Telegram 产品（`telegram`）与旧内联 bot（`legacy bot`）。安装器默认使用官方预构建镜像、缺库时构建词典、等待就绪并签发首个设备令牌，详见 [自托管指南](self-hosting.md#one-command-deploysetupsh)。`--with-web` 直接读取 WuwaTerm-Web `v0.1.1` 已发布的 `manifest-{root|prefix}.json` 与 `SHA256SUMS-{root|prefix}`，校验并解包 `archive.name` 指向的归档，运行其中的 `bundle/start.mjs`；`--web-origin` 提供反向代理对外的 HTTPS 源，就绪检查会带 `X-WuwaTerm-Edge` 请求页面与 `/api/pool`。Web 的 SQLite 准入库由 `WUWATERM_LOCAL_DB_PATH` 决定，默认 `/var/lib/wuwaterm-web/pool.sqlite3`，宿主的 `state-web/data/` 挂到该文件所在目录，不落在只读的 `/app` 包里，所以 Docker 不需要在只读挂载下创建挂载点；旧 `web.env` 写的 `/app/data/pool.sqlite3` 仍可用，安装器会先在解包目录里建好挂载点，相对路径则直接拒绝。本地包可用 `--web-package`。只有真的拿到部署指针和它指向的 manifest 才算已接管，干净安装上的 `adopt` 失败会如实报告为未接管，并在下次运行时重试。

当前 Core 源码仍保留旧 Telegram 和私有 Python Web 入口；独立产品源码合并不等于替代交付已完成。迁移状态文件时，先按各产品指南备份和核对。

## 版本与兼容

下表中的“已合并”指该日期观察到的仓库 main 源码；发布和实际采用分别记录。候选制品不能当作正式版，历史生产服务的存在也不能证明本次新源码已部署。

| 产品 | 已合并源码快照 | 独立发布状态 | 本次新交付的实际采用 |
| --- | --- | --- | --- |
| Core / API | `5f0d962`，源码版本 `0.8.1` | [v0.8.1](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.8.1) 已发布（wheel `80799d03…`，sdist `2c087374…`，manifest 与 SHA256SUMS 齐全），来源 `5f0d962`，自 v0.8.0 来源 `3e07413` 起 24 个提交（22 个到准备基线 `6a3ec3a`，另加 #226 与 #227），含截断不再冒充成功、评测不再把无法判定或空样本当通过、术语与词库修复、依赖审计门禁与首装入口改造；[v0.8.0](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.8.0) 已发布（wheel `06233748…`，sdist `92a2d3f1…`），来源 `3e07413`，含 semantic-review-v1、segmentation-v1、`GET /v1/capabilities` 与带上下文的翻译，语义审校默认关闭；[v0.7.2](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.7.2)（wheel `1faea9af…`，含 #195 的 Web 数据目录挂载修复）、`v0.7.1`（wheel `4f7307a1…`，来源 `c1d6843`，不含该修复）与 `v0.7.0`（wheel `721b825e…`）仍保留在发布页上 | 已采用：API + 内置 Bot（2026-11-12，部署源 `4a003aa`；独立 Telegram 未切换） |
| Telegram | `45f38a7`，源码版本 `0.2.2` | [v0.2.2](https://github.com/My-Denia/WuwaTerm-Telegram/releases/tag/v0.2.2) 已发布（wheel `c1c14ab7…`，sdist、bundle、镜像与安装探针齐全），来源 `45f38a7`，新增 `/help` 与 `/start` 命令清单，依赖 Core `v0.8.1`；[v0.2.1](https://github.com/My-Denia/WuwaTerm-Telegram/releases/tag/v0.2.1)（wheel `56577602…`，来源 `ec7aa8b`）与更早版本仍保留 | 已采用：独立 Telegram 0.2.2（2026-11-12；旧内置 Bot 已停止） |
| Windows | `14f0c36`，源码版本 `0.4.2` | [v0.4.2](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.2) 已发布（zip `824b694c…`，manifest 与 SHA256SUMS 齐全），来源 `14f0c36`，修复空状态卡片行高与查词 Tab 顺序；[v0.4.1](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.4.1)（zip `4d248a0e…`）与 `v0.4.0`、`v0.3.0` 仍保留 | 未确认 |
| Web | `6e1337f`，源码版本 `0.2.0`（未发布） | [v0.1.1](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.1.1) 仍是最新 GitHub 发布，来源 `31fdc14b`：root（归档 `9f88dd71…`）与 `/private`（归档 `32b52a3a…`）两套包；`v0.1.0` 仍在发布页上。公测 Sites 已按 main `6e1337f` 部署，移动端头部、额度区与 `/limits` 表格使用本次合并的布局 | 已部署：公测 Sites（2026-11-12，源码 main `6e1337f`；未发 GitHub release） |

四个正式版资产的 SHA-256 按发布页逐个记录在[索引](../contracts/products.json)。Web 发布包里 `archive.name` 指向归档，入口是 `bundle/start.mjs`，路径前缀记在 manifest 与 `bundle/BUILD-INFO.json`；`deploy/setup.sh` 按这份真实结构安装和检查，不再要求早期设想中实际并不存在的 `start`、`port`、`health_path` 字段（本地包若提供仍会采用）。

Windows 与 Web 的在线功能使用 Core `/v1` API；稿件审校需要 `review-v2`，两端稿件文件格式为 `wuwaterm-manuscript-v1`。API 版本相同本身不足以证明审校或私有 Web 可用：私有 Web 还要求 `GET /v1/auth/status`，其代理也严格校验响应结构。Core 同时支持 `review-v1` 和 `review-v2`，默认是前者。Telegram 直接使用固定版本的 Core 包和 schema `2` 术语数据库；它的聊天设置是 JSON，频道回复索引版本为 `1`。API 设备库和私有 Web 的准入计数库各自独立于术语数据库，后者当前为 schema `1`。具体组合和 CI 固定版本见[索引](../contracts/products.json)，不要将某一组契约测试推广到所有版本组合。

Windows 现代界面已实现并经过部分原生检查，完整 UX 和 owner 审美验收尚未确认。此快照期间没有进行新的生产、Sites 或 VPS 部署；发布四个正式版不等于已经上线或已经替换生产服务。

四个仓库共用 [WuwaTerm GitHub Project](https://github.com/users/My-Denia/projects/1)；现有 Linear WuwaTerm 项目仍是工作跟踪的权威来源。
