# WuwaTerm 产品入口

WuwaTerm 可以按需要选择 Telegram、Windows 或 Web。核心仓库负责共享词典、翻译与审校逻辑、HTTP API、数据构建验证以及通用部署恢复工具。各产品独立维护源码和发布；[机器可读索引](../contracts/products.json)记录了截至 2026-11-09 的源码、制品和接口快照，四个仓库的正式版 tag、commit 和资产 SHA-256 都在其中。要安装新版本，请再核对各仓库的发布页与说明。

## 选择使用方式

- **只用 Telegram**：使用 [Telegram 安装与运行指南](https://github.com/My-Denia/WuwaTerm-Telegram/blob/b0e126eef8b6fd541d11c9c16705d558cd9e4605/README.md)，准备 bot token、单独构建的术语数据库和指南要求的 Core wheel；不需要运行 HTTP API。当前 `0.1.1` 已正式发布，`core-lock.json` 固定的就是 Core `v0.7.0` 发布 wheel（`721b825e…`），[制品校验说明](https://github.com/My-Denia/WuwaTerm-Telegram/blob/b0e126eef8b6fd541d11c9c16705d558cd9e4605/docs/artifact-verification.md)解释如何校验。
- **只用 Windows 客户端**：从 [Windows v0.3.0 发布页](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0)下载并核对便携包；[使用与构建指南](https://github.com/My-Denia/WuwaTerm-Windows/blob/cda93f01bf7803efe66ca695701cae678b320ef1/README.md)列出了操作步骤。稿件可离线编辑、保存；在线查词、翻译和审校需要运营方提供的兼容 API 地址及设备令牌。程序未签名。
- **使用 Web 和 API**：公共工作台入口及使用说明见 [Web 产品指南](https://github.com/My-Denia/WuwaTerm-Web/blob/31fdc14b1a14563990d0acaf89dc302f024cebdc/docs/sites.md)。自托管时先按 [Core 自托管指南](self-hosting.md)和[数据构建指南](data-refresh.md)准备 API 与词典，再按 Web 仓库自己的说明配置 Node 运行环境、设备令牌和带认证的 HTTPS 反向代理。根路径与路径前缀需要对应构建，[Web v0.1.1 发布页](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.1.1)分别提供 root 与 `/private` 两套包，`v0.1.0` 的包仍可下载。同一工作台支持公共和自托管方式，Sites 是托管目标。
- **一条命令安装 Core（可选加 Telegram 或 Web）**：在 Core 检出目录运行 `./deploy/setup.sh` 只启动共享翻译服务；`--with-telegram` 或 `--with-web` 在首次安装或已有服务上增量添加；`--status`/`--check` 查看组件、数据版本与健康。安装器默认使用官方预构建镜像、缺库时构建词典、等待就绪并签发首个设备令牌，详见 [自托管指南](self-hosting.md#one-command-deploysetupsh)。`--with-web` 直接读取 WuwaTerm-Web `v0.1.1` 已发布的 `manifest-{root|prefix}.json` 与 `SHA256SUMS-{root|prefix}`，校验并解包 `archive.name` 指向的归档，运行其中的 `bundle/start.mjs`；`--web-origin` 提供反向代理对外的 HTTPS 源，就绪检查会带 `X-WuwaTerm-Edge` 请求页面与 `/api/pool`。Web 的 SQLite 准入库由 `WUWATERM_LOCAL_DB_PATH` 决定，默认 `/var/lib/wuwaterm-web/pool.sqlite3`，宿主的 `state-web/data/` 挂到该文件所在目录，不落在只读的 `/app` 包里，所以 Docker 不需要在只读挂载下创建挂载点；旧 `web.env` 写的 `/app/data/pool.sqlite3` 仍可用，安装器会先在解包目录里建好挂载点，相对路径则直接拒绝。本地包可用 `--web-package`。只有真的拿到部署指针和它指向的 manifest 才算已接管，干净安装上的 `adopt` 失败会如实报告为未接管，并在下次运行时重试。

当前 Core 源码仍保留旧 Telegram 和私有 Python Web 入口；独立产品源码合并不等于替代交付已完成。迁移状态文件时，先按各产品指南备份和核对。

## 版本与兼容

下表中的“已合并”指该日期观察到的仓库 main 源码；发布和实际采用分别记录。候选制品不能当作正式版，历史生产服务的存在也不能证明本次新源码已部署。

| 产品 | 已合并源码快照 | 独立发布状态 | 本次新交付的实际采用 |
| --- | --- | --- | --- |
| Core / API | `086dcf5`，源码版本 `0.7.2` | [v0.7.2](https://github.com/My-Denia/WuwaTerm/releases/tag/v0.7.2) 已发布（wheel `1faea9af…`，sdist `787fd10f…`），来源 `086dcf5`，含 #195 的 Web 数据目录挂载修复；`v0.7.1`（wheel `4f7307a1…`，来源 `c1d6843`，不含该修复）与 `v0.7.0`（wheel `721b825e…`）仍保留在发布页上 | 未确认 |
| Telegram | `b0e126ee`，`0.1.1` | [v0.1.1](https://github.com/My-Denia/WuwaTerm-Telegram/releases/tag/v0.1.1) 已发布（wheel `07f4ffcf…`，sdist、bundle、镜像与安装探针齐全），依赖 Core `v0.7.0` 正式 wheel；`v0.1.0` 仍保留 | 未确认 |
| Windows | `974160e7`，后续仅增加历史 Core 验证配方 | [v0.3.0](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0) 已发布，来源 `cda93f01` | 未确认 |
| Web | `31fdc14b`，源码版本 `0.1.1` | [v0.1.1](https://github.com/My-Denia/WuwaTerm-Web/releases/tag/v0.1.1) 已发布：root（归档 `9f88dd71…`）与 `/private`（归档 `32b52a3a…`）两套包，各自带 `manifest-{root|prefix}.json` 和 `SHA256SUMS`；`v0.1.0` 的 root `4e5e9ba8…` 与 `/private` `2dc6d722…` 仍在发布页上 | 未确认 |

四个正式版资产的 SHA-256 按发布页逐个记录在[索引](../contracts/products.json)。Web 发布包里 `archive.name` 指向归档，入口是 `bundle/start.mjs`，路径前缀记在 manifest 与 `bundle/BUILD-INFO.json`；`deploy/setup.sh` 按这份真实结构安装和检查，不再要求早期设想中实际并不存在的 `start`、`port`、`health_path` 字段（本地包若提供仍会采用）。

Windows 与 Web 的在线功能使用 Core `/v1` API；稿件审校需要 `review-v2`，两端稿件文件格式为 `wuwaterm-manuscript-v1`。API 版本相同本身不足以证明审校或私有 Web 可用：私有 Web 还要求 `GET /v1/auth/status`，其代理也严格校验响应结构。Core 同时支持 `review-v1` 和 `review-v2`，默认是前者。Telegram 直接使用固定版本的 Core 包和 schema `2` 术语数据库；它的聊天设置是 JSON，频道回复索引版本为 `1`。API 设备库和私有 Web 的准入计数库各自独立于术语数据库，后者当前为 schema `1`。具体组合和 CI 固定版本见[索引](../contracts/products.json)，不要将某一组契约测试推广到所有版本组合。

Windows 现代界面已实现并经过部分原生检查，完整 UX 和 owner 审美验收尚未确认。此快照期间没有进行新的生产、Sites 或 VPS 部署；发布四个正式版不等于已经上线或已经替换生产服务。

四个仓库共用 [WuwaTerm GitHub Project](https://github.com/users/My-Denia/projects/1)；现有 Linear WuwaTerm 项目仍是工作跟踪的权威来源。
