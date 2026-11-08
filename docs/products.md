# WuwaTerm 产品入口

WuwaTerm 可以按需要选择 Telegram、Windows 或 Web。核心仓库负责共享词典、翻译与审校逻辑、HTTP API、数据构建验证以及通用部署恢复工具。各产品独立维护源码和发布；[机器可读索引](../contracts/products.json)记录了截至 2026-11-08 的源码、制品和接口快照。要安装新版本，请再核对各仓库的发布页与说明。

## 选择使用方式

- **只用 Telegram**：使用 [Telegram 安装与运行指南](https://github.com/My-Denia/WuwaTerm-Telegram/blob/6ef554f5d840cf8757b56f6592c64d0914489015/README.md)，准备 bot token、单独构建的术语数据库和指南要求的 Core wheel；不需要运行 HTTP API。当前 `0.1.0.dev1` 依赖固定的 `0.7.0.dev1` Core 候选包，[制品校验说明](https://github.com/My-Denia/WuwaTerm-Telegram/blob/6ef554f5d840cf8757b56f6592c64d0914489015/docs/artifact-verification.md)解释如何校验。正式版尚未发布，跨仓获取候选制品的可用性需按指南核实。
- **只用 Windows 客户端**：从 [Windows v0.3.0 发布页](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0)下载并核对便携包；[使用与构建指南](https://github.com/My-Denia/WuwaTerm-Windows/blob/cda93f01bf7803efe66ca695701cae678b320ef1/README.md)列出了操作步骤。稿件可离线编辑、保存；在线查词、翻译和审校需要运营方提供的兼容 API 地址及设备令牌。程序未签名。
- **使用 Web 和 API**：公共工作台入口及使用说明见 [Web 产品指南](https://github.com/My-Denia/WuwaTerm-Web/blob/c9dde012cf88b934a70aac18c12778e8057ffc63/docs/sites.md)。自托管时先按 [Core 自托管指南](self-hosting.md)和[数据构建指南](data-refresh.md)准备 API 与词典，再按 Web 仓库自己的说明配置 Node 运行环境、设备令牌和带认证的 HTTPS 反向代理。根路径与路径前缀需要对应构建。同一工作台支持公共和自托管方式，Sites 是托管目标。
- **一条命令安装 Core（可选加 Telegram 或 Web）**：在 Core 检出目录运行 `./deploy/setup.sh` 只启动共享翻译服务；`--with-telegram` 或 `--with-web` 在首次安装或已有服务上增量添加；`--status`/`--check` 查看组件、数据版本与健康。安装器默认使用官方预构建镜像、缺库时构建词典、等待就绪并签发首个设备令牌，详见 [自托管指南](self-hosting.md#one-command-deploysetupsh)。`--with-web` 需要 WuwaTerm-Web 已发布的 `manifest-{root|prefix}.json` 与 `SHA256SUMS-{root|prefix}` 包；该仓库当前尚未发布正式版，所以命令会如实停下，本地包可用 `--web-package`。

当前 Core 源码仍保留旧 Telegram 和私有 Python Web 入口；独立产品源码合并不等于替代交付已完成。迁移状态文件时，先按各产品指南备份和核对。

## 版本与兼容

下表中的“已合并”指该日期观察到的仓库 main 源码；发布和实际采用分别记录。候选制品不能当作正式版，历史生产服务的存在也不能证明本次新源码已部署。

| 产品 | 已合并源码快照 | 独立发布状态 | 本次新交付的实际采用 |
| --- | --- | --- | --- |
| Core / API | `dcd0e20`，主线源码版本 `0.7.0.dev1`；`0.7.0` 改动在 `feat/product-delivery-and-contextual-terms` 分支待合并 | `0.7.0` 尚未发布；Telegram 固定的 wheel 是较早 CI checkout 的候选制品，不代表本批源码 | 未确认 |
| Telegram | `6ef554f5`，`0.1.0.dev1` | `0.1.0` 尚未发布 | 未确认 |
| Windows | `974160e7`，后续仅增加历史 Core 验证配方 | [v0.3.0](https://github.com/My-Denia/WuwaTerm-Windows/releases/tag/v0.3.0) 已发布，来源 `cda93f01` | 未确认 |
| Web | `0607c7e8`，源码版本 `0.1.0`，发布流程已合并 | `0.1.0` 尚未正式发布；来源 `c9dde012` 的[根路径候选](https://github.com/My-Denia/WuwaTerm-Web/actions/runs/42210869041)和 [`/private` 候选](https://github.com/My-Denia/WuwaTerm-Web/actions/runs/42210872354)已构建，四项回放命令退出码均为 0；[托管发布校验](https://github.com/My-Denia/WuwaTerm-Web/actions/runs/42297334636)已通过，实际发布尚未执行 | 未确认 |

Windows 与 Web 的在线功能使用 Core `/v1` API；稿件审校需要 `review-v2`，两端稿件文件格式为 `wuwaterm-manuscript-v1`。API 版本相同本身不足以证明审校或私有 Web 可用：私有 Web 还要求 `GET /v1/auth/status`，其代理也严格校验响应结构。Core 同时支持 `review-v1` 和 `review-v2`，默认是前者。Telegram 直接使用固定版本的 Core 包和 schema `2` 术语数据库；它的聊天设置是 JSON，频道回复索引版本为 `1`。API 设备库和私有 Web 的准入计数库各自独立于术语数据库，后者当前为 schema `1`。具体组合和 CI 固定版本见[索引](../contracts/products.json)，不要将某一组契约测试推广到所有版本组合。

Windows 现代界面已实现并经过部分原生检查，完整 UX 和 owner 审美验收尚未确认。此快照期间没有进行新的生产、Sites 或 VPS 部署。

四个仓库共用 [WuwaTerm GitHub Project](https://github.com/users/My-Denia/projects/1)；现有 Linear WuwaTerm 项目仍是工作跟踪的权威来源。
