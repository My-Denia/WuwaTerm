# WuwaTerm 仓库盘点与审计

- **仓库**：<https://github.com/My-Denia/wuwa-translate-bot>
- **审计对象**：当前 `main` 的 `HEAD`
- **已核实 SHA**：`291d82d80c40a870b7bde9483486e60d37cc5669`（`feat: ship private Sites product v1 (#98)`）
- **审计日期**：2026-08-27
- **性质**：只读盘点。本文不改变产品或运行时行为；它记录树里实际存在的结构、契约与风险。
- **后续**：同一分支上后来的提交按 §8.4 第 1 条补上了主文档（`docs/sites.md` 与入口/架构/安全/校验的交叉引用）。下面「主文档不提 `site/`」描述的是审计当时的 `291d82d`，不是补文档之后的工作树。
- **证据规则**：标为 **已核实** 的内容来自本检出中的文件、已合并 PR 文本，或本审计过程中实际跑过的命令。标为 **假设** 的内容是推理，不是树里能直接证明的事实。本文不发明通过率、覆盖率或线上流量数字。

---

## 1. 产品是什么，给谁用

### 1.1 一句话（已核实）

`README.md` 把产品写成：

> 自建的《鸣潮》官方本地化翻译服务：中文术语译为官方英文，英文亦可反向译回中文，并支持两个方向的术语锁定整句翻译。

它是 **dictionary-first（词典优先）** 的：精确命中时，从本地 SQLite 逐字节返回官方字符串，不调用 LLM。术语库在本地构建，**不随任何发布物分发**。上游游戏数据版权归 Kuro Games；本仓库只做稀疏检出与派生词典。

### 1.2 五条对外表面，不是四条（已核实）

主文档（`README.md`、`CONTRIBUTING.md`、`docs/architecture.md`）仍把系统写成四条表面：

| 表面 | 角色 | 位置 |
|------|------|------|
| Telegram bot | presentation adapter：命令、群授权、频道自动翻译、HTML | `src/wuwaterm/bot.py`、`channel.py` |
| 版本化 HTTP API | presentation adapter：`/v1/*`、设备认证、统一错误信封、纯文本 | `src/wuwaterm_api/` |
| Owner 私有 web（进程内） | 第三个表示层，**默认关闭**，挂在 API 进程里 | `src/wuwaterm_api/web/`，`/wuwaterm-web` |
| Windows 桌面客户端 | **不是** adapter，只消费已发布 `/v1` 契约 | `client/` |

`HEAD` 上还有第五条，已进 CI，但 **主文档几乎完全没写**：

| 表面 | 角色 | 位置 |
|------|------|------|
| Sites 私有工作台 v1 | 独立的浏览器 BFF：同源 `/api/*` 代理到 VPS `/v1` | `site/`（PR #96 可行性，PR #98 产品 v1） |

这不是措辞问题。进程内 `/wuwaterm-web` 与 `site/` 是两套托管、两套鉴权、两套文档覆盖。把它们当成同一个「私有网页」会读错信任边界。详见第 4 节。

### 1.3 受众（已核实，`README.md`「从这里开始」）

| 读者 | 入口 |
|------|------|
| 桌面用户 | GitHub Releases 的 Windows zip；`client/README.md` |
| Telegram 群管理员 | `docs/telegram-behavior.md` |
| 自建部署者 | `docs/self-hosting.md` |
| 贡献者 | `CONTRIBUTING.md`；`python scripts/validate.py` |
| Owner 生产运维 | `docs/deployment.md`（作者自己的 VPS 手册，不是通用指南） |

`SUPPORT.md` / `SECURITY.md` 的维护立场：个人业余项目、尽力而为、**没有托管服务**、没有私人支持频道。安全报告走 GitHub Security Advisory，不公开邮箱。

### 1.4 版本与发布物（已核实）

| 组件 | 版本写在哪 | 本检出值 |
|------|------------|----------|
| 服务端包 `wuwaterm` | `pyproject.toml`、`uv.lock` | **0.4.1**（CHANGELOG 称为未单独打 GitHub release 的私有补丁） |
| 桌面客户端 `wuwaterm-client` | `client/pyproject.toml` | **0.2.0**（`Private :: Do Not Upload`） |
| Sites 包 `sites-project` | `site/package.json` | **0.1.0**、`private: true` |
| 最近带日期的 CHANGELOG 发布 | `CHANGELOG.md` | **0.4.0**（2026-08-19） |

自 v0.4.0 起的发布物（`docs/release-checklist.md`、`.github/workflows/release.yml`）：

- GitHub Releases：wheel、sdist、`WuwaTerm-<client-version>-windows-x64.zip`、`SHA256SUMS`、`release-manifest.json`
- GHCR：`ghcr.io/my-denia/wuwaterm`（runtime）与 `ghcr.io/my-denia/wuwaterm-builder`
- 客户端 **未代码签名**；SmartScreen 警告是文档里写明的预期行为
- 术语库永远本地构建；镜像只省掉「本地 build 镜像」这一步

### 1.5 刻意不做的事（已核实）

`CONTRIBUTING.md` 的 Non-Goals 由 `scripts/check_non_goals.py` 做全文门禁，四个产品禁令是：

1. 不为 Telegram 注册 callback-URL 投递（只用 long polling；ADR 0003）
2. 不做 inline-query 表面
3. 不在官方词典上再叠一层非官方同义命名
4. 除关联频道自动转发外，不再加自由文本监听器（`src/wuwaterm/bot.py` 里只允许那一个）

该扫描器读整个树的文本后缀（含 `.md`）。本文因此避免写出被禁标识符本身。

---

## 2. 仓库地图

### 2.1 顶层（已核实）

```
wuwa-translate-bot/
├── README.md / README.en.md     中英入口；受众路由；不提 site/
├── CONTRIBUTING.md / SUPPORT.md / SECURITY.md / LICENSE
├── CHANGELOG.md                 Unreleased 写了 0.4.1 API /grant / CJK 门；未写 Sites
├── pyproject.toml / uv.lock     服务端包 wuwaterm 0.4.1；入口 wuwaterm、wuwaterm-api
├── MANIFEST.in                  仅 prune tests
├── .env.example                 bot / API / 进程内 web 的环境变量模板
├── src/wuwaterm/                应用层 + Telegram + 词典构建
├── src/wuwaterm_api/            HTTP adapter + 可选 /wuwaterm-web
├── client/                      独立 Windows 桌面包
├── site/                        独立 Next/Vinext/Cloudflare Sites 工作台
├── deploy/                      Dockerfile、Compose、vps-update.sh
├── docs/                        架构、ADR、OpenAPI 快照、运维指南
├── scripts/                     validate.py 与各离线门
├── tests/                       服务端 pytest（29 个 test_*.py + conftest.py）
├── .github/workflows/           ci / release / selfhost-smoke / claude*
└── .cursor/                     Cloud Agent 安装脚本
```

`deploy/Dockerfile` 只 `COPY` `pyproject.toml`、`uv.lock`、`README.md`、`src/`、`scripts/` 与入口脚本。`client/` 被 `.dockerignore` 排除。`site/` 不在 ignore 里，但也 **没有被 COPY**——runtime / builder 镜像都不含 Sites。

### 2.2 `src/wuwaterm/`：应用层、Telegram、构建器

| 模块 | 职责 |
|------|------|
| `application.py` | **唯一** dictionary-first 流水线：`prepare → 方向 → 精确命中 → 可信模糊 → 长度门 → 切分 → 术语锁定 LLM` |
| `lookup.py` | `TermService`：精确等值 + 全表模糊打分 |
| `sentence.py` | 术语锁定、占位符、LLM HTTP |
| `normalize.py` / `models.py` / `translation_policy.py` | 规范化、共享对象、长度/失败文案常量 |
| `db.py` / `builder.py` / `build_pinyin.py` / `data_source.py` / `constants.py` | SQLite 与上游钉住（Arikatsu `6ce8d5eda49f2930da84d8846c144432142c7465`，GameVer 3.6.0） |
| `bot.py` / `channel.py` / `channel_*.py` | Telegram 命令与关联频道自动翻译 |
| `settings.py` | 群允许名单 / 公开模式（`chat_settings.json`） |
| `telegram_html.py` / `telegram_text.py` | HTML 保护与 UTF-16 切分 |
| `logging_utils.py` / `runtime_keys.py` | 日志脱敏、bot_data 键 |
| `cli.py` | `refresh-data`、`build-db`、`counts`、`lookup`、`sentence`、`bot` |

入口：`wuwaterm` → `wuwaterm.cli:main`。

### 2.3 `src/wuwaterm_api/`：HTTP adapter

| 模块 | 职责 |
|------|------|
| `app.py` | FastAPI：`/healthz`、`/readyz`、`/openapi.json`、`/v1/meta`、`/v1/terms`、`/v1/translations` |
| `auth.py` | 设备主体：`wtd1.<device_id>.<secret>`；只存加盐 scrypt |
| `settings.py` | `validate_loopback_bind`；`DEFAULT_WEB_ENABLED = False` |
| `errors.py` | 稳定错误码 ↔ HTTP 状态 |
| `cli.py` | `serve`；`device issue|list|revoke` |
| `web/` | 进程内 owner web：表单、会话、无页面脚本 |

`wuwaterm_api` 对 `wuwaterm` 的导入允许名单只有 `application`、`models`、`translation_policy`、`logging_utils`（`scripts/check_architecture_boundaries.py`）。

### 2.4 `site/`：Sites 工作台（文档真空）

完整应用面（排除 lockfile / 构建产物）：

```
site/
├── app/page.tsx、layout.tsx、robots.ts、globals.css
├── app/components/translation-workbench.tsx
├── app/api/{meta,terms,translations}/route.ts
├── lib/wuwaterm-proxy.js
├── scripts/verify-no-client-secret.mjs
├── tests/feasibility.test.mjs、product-v1.test.mjs
├── .openai/hosting.json
└── package.json（vinext / Next 16 / React 19 / Cloudflare plugin）
```

**已核实：主文档不提 `site/`。** 在 `README.md`、`README.en.md`、`CONTRIBUTING.md`、`SUPPORT.md`、`SECURITY.md`、`CHANGELOG.md`、`docs/*.md`、`docs/adr/*.md` 中搜索 `site/`、`Sites`、`sites-project`、`WUWATERM_SITE`，命中为零。唯一的工作流引用是 `.github/workflows/ci.yml` 的 `site` job。

### 2.5 `client/`：Windows 消费方

独立包，`requires-python >=3.12`。PySide6 + httpx + keyring + qasync。凭据进 Windows Credential Manager（`SERVICE_NAME="WuwaTerm"`），`config.json` 只存 URL 与超时。`client/build.ps1` 打 one-folder zip。21 个 `test_*.py` + `conftest.py`。

### 2.6 `deploy/`、`scripts/`、`docs/`

- **Compose**（`deploy/docker-compose.yml`）：`wuwaterm`（bot）、`wuwaterm-api`（`WUWATERM_API_BIND: 127.0.0.1` 写死，不从 `.env` 插值）、`wuwaterm-builder`（profile `builder`，无 `env_file`）。`network_mode: host`。
- **事务式更新**：`deploy/vps-update.sh`——候选库、镜像提升、冒烟、失败回滚。
- **校验入口**：`scripts/validate.py` = hygiene → non-goals → architecture → api-contract → ruff → pytest。
- **ADR**：0001–0014 全部 Accepted。没有 Sites ADR。

### 2.7 对 `site/` 刻意不在范围内的东西（已核实）

Sites v1 **不**代理、不暴露、不实现：

- VPS `/healthz`、`/readyz`、`/openapi.json`
- `wuwaterm-api device issue|list|revoke`
- Telegram / 频道
- 进程内 `/wuwaterm-web` 的 basic_auth、边缘头、会话 cookie
- 浏览器侧重排、过滤、去重术语结果
- 浏览器侧方向判定（可省略 `to`，由 VPS 决定）
- D1 / R2（`site/.openai/hosting.json` 里均为 `null`）
- 分析、WebSocket、cookie、`localStorage`（`verify-no-client-secret.mjs` 禁止）
- 历史记录、账号系统、自定义域名、持久存储（PR #98 正文）

---

## 3. 运行时架构与数据流

### 3.1 拓扑（已核实，`docs/architecture.md` + Compose）

```
Telegram 用户 ──long poll──► wuwaterm-bot ──► application.py
桌面客户端 ──HTTPS──► 反代 /wuwaterm-api/* ──► wuwaterm-api ──► 同一条流水线
Owner 手机浏览器 ──HTTPS──► 反代 /wuwaterm-web/* ──► 同进程 in-process 调用（默认关）
Sites 浏览器 ──同源 /api/*──► Cloudflare Worker ──Bearer──► 同一 /v1（独立部署）
                                                      │
                                                      ▼
                                            data/terms.db (ro)
                                            可选 OpenAI 兼容 LLM
Builder（无 env_file）：refresh-data → build-db → verify-* → vps-update.sh 提升
```

两个服务容器同一 runtime 镜像、同一只读 `data/`：

- bot 写 `state/`（`chat_settings.json`、`channel_replies.json`）；把 web 相关环境变量置空
- API 写 **兄弟目录** `state-api/`（设备库）；把 `TELEGRAM_BOT_TOKEN`、`OWNER_USER_ID`、`WUWATERM_REDACTION_SECRET` 置空
- 模型凭据两边共享（有意）

### 3.2 词典优先 vs LLM（已核实）

`src/wuwaterm/application.py` 流水线：

```
prepare → 解析方向 → lookup_exact → 可信 ASCII 模糊短路 → 长度门
        → 长文本切分 → SentenceTranslator（锁术语 → LLM）
```

结果 `kind`：`noop | exact | fuzzy | llm | error`。错误码集合 `TRANSLATION_ERROR_CODES` 是适配器必须映射、不得私自发明的契约。

**不调用 LLM 的路径：**

| 条件 | 结果 |
|------|------|
| 规范化后为空 | `noop` |
| 精确命中且官方串非空 | `exact` |
| 可信模糊短路（见下） | `fuzzy` |
| 超长（进 LLM 前） | `error=input_too_long` |
| `GET /v1/terms`、`/v1/meta`、健康检查 | 无翻译 |
| 进程内 web「查词」 | 只走 `lookup_exact_terms` |
| 非法 `--to`（Telegram） | 只回用法 |
| 未授权 / 限流 | 到不了流水线 |

**可信模糊短路**（`application.py`，须同时满足）：查询为 ASCII 且匹配短查询正则；`lookup(..., limit=5)` 最高分 ≥ 80；reason 属于 `{exact, pinyin, pinyin-abbrev}`，或较长的 pinyin-prefix/substring，或缩写等于查询。然后返回官方 zh/en，**不调 LLM**。

**会调 LLM 时：** 词典阶段都未命中，且长度门通过。`sentence.py` 再做一次 `lookup_exact`；仍未命中则把已知跨度锁成 `__WUWA_TERM_<nonce>_<n>__`，再 `POST {base}/chat/completions`，`temperature: 0`。系统提示含方向、不信任源、以及 `Locked terms: {placeholder} = {官方目标语}`。用户消息是锁定后的可见文本。

LLM 已配置当且仅当 base URL、API key、`WUWATERM_OPENAI_MODEL` 都非空（`WUWATERM_OPENAI_*`，base/key 可回退到 `OPENAI_*`）。

**LLM 未配置且本会走到 `kind=llm`：**

| 表面 | 行为 |
|------|------|
| Telegram | 还原占位符后的原文（不假装译过） |
| HTTP API / 进程内 web 翻译 | **503** `llm_unavailable` |
| 频道 | 跳过或只走精确分支 |

### 3.3 各表面怎么进流水线（已核实）

| 表面 | 查词 | 整句 |
|------|------|------|
| Telegram `/tr` `/term` `/sentence` `/sent` | 同一 `_translation_command` → `translate_request_async`（差别只在用法文案） | 同上；可注入 HTML markup translator |
| HTTP `GET /v1/terms?q=` | `lookup_terms()`：精确 **或** 模糊，最多 5 条，后端排序（0.4.1 / #97） | — |
| HTTP `POST /v1/translations` | — | 全流水线，纯文本，无 markup |
| `/wuwaterm-web` 查词 | **仅精确** `lookup_exact_terms`，进程内 | — |
| `/wuwaterm-web` 翻译 | — | 进程内 `translate_request_async`，不是 HTTP 回环 |
| 桌面客户端 | HTTPS `GET /v1/terms` | HTTPS `POST /v1/translations` |
| Sites | 同源 `GET /api/terms?q=` → 服务端代理 `/v1/terms` | 同源 `POST /api/translations` → `/v1/translations` |

`TERM_QUERY_MAX_LENGTH = 200`（`src/wuwaterm_api/__init__.py`）约束 JSON 与进程内 web 查词。Sites 代理允许查询 ≤ **4096**（`site/lib/wuwaterm-proxy.js`），比 VPS 宽——超长查询会在上游被拒，再投影成站点错误。

### 3.4 凭据与身份（已核实）

两套控制面 **互相不能授予或吊销**（`docs/architecture.md`）：

| | Telegram | HTTP / 设备 |
|---|---|---|
| 主体 | Telegram user id + chat id | 运营者登记的 device |
| Owner | `OWNER_USER_ID` | 没有「owner 设备」，只有设备 |
| 发放 | `/grant`（`/authorize` 已在 #95 删除） | `wuwaterm-api device issue`，secret 只从 stdin 读 |
| 存储 | `state/chat_settings.json` | `state-api/devices.db`：盐 + scrypt，**永不存 secret** |
| 收回 | `/revoke` | `device revoke` 打 `revoked_at` |
| 范围 | 按群 + owner 命令 | `translate`、`meta`（默认两者都有） |
| 失败 | 静默或聊天文案 | `401` 未证明 / `403` 缺 scope |

设备 token 格式（`src/wuwaterm_api/auth.py`）：

```
wtd1.<device_id>.<secret>
```

secret ≥ 32 个可打印 ASCII、无空格。服务端 **从不打印 secret**。商店缺失 → **503**，不是 401。

其他秘密：

| 秘密 | 谁持有 | 浏览器看得到吗 |
|------|--------|----------------|
| `TELEGRAM_BOT_TOKEN` | 仅 bot 容器 | 否 |
| `WUWATERM_OPENAI_API_KEY` | bot + API | 否 |
| `WUWATERM_API_WEB_DEVICE_TOKEN` + `WUWATERM_API_WEB_EDGE_SECRET` | 仅 API 进程 + 反代 | 否；浏览器只有不透明 HttpOnly `wuwaterm_session` |
| 桌面 `wtd1...` | Windows Credential Manager | 否（不进 `config.json`） |
| `WUWATERM_SITE_DEVICE_TOKEN` | Sites Worker 环境 | 否（构建扫描禁止进包） |

Sites 的 token 是 **第三处** 设备凭据存放点，与 VPS `.env`、进程内 web token 分开。

### 3.5 浏览器允许碰什么（已核实）

**`/wuwaterm-web`（ADR 0014）：**

- 允许：GET 表单；POST `application/x-www-form-urlencoded` 的 `q` 或 `text`
- Cookie：`wuwaterm_session`，HttpOnly，`Path=/wuwaterm-web`，`SameSite=Strict`
- 页面 **不带脚本**；CSP `default-src 'none'; style-src 'unsafe-inline'; form-action 'self'`
- 门闩：Caddy `basic_auth` → 注入 `X-Wuwaterm-Edge` → 会话。直连 loopback 过不了边缘头
- 已记录残留：同站 CSRF（无 per-session anti-forgery token）

**`site/`（PR #98）：**

- 恰好 3 次同源 `fetch`：`/api/meta`、`/api/terms?q=`、`/api/translations`
- 无 `Authorization`、无 `/wuwaterm-api/`、无 `WUWATERM_*`、无绝对 URL
- `robots.ts`：`disallow: /`；layout `noindex/nofollow`；代理响应带 `x-robots-tag: noindex, nofollow, noarchive`
- **代码里没有访问者鉴权。** UI 上的「私有翻译产品」不是访问控制。

### 3.6 请求路径与契约（已核实）

已发布 HTTP 路径（`docs/api/openapi.json`，`scripts/check_api_contract.py` 漂移门）：

| 路径 | 认证 | 作用 |
|------|------|------|
| `GET /healthz`、`GET /readyz`、`GET /openapi.json` | 无 | 存活 / 就绪 / 契约；Compose 注释写明这是入口决策，不是服务强制 |
| `GET /v1/meta` | Bearer + scope `meta` | 版本、词条数、`llm_configured` |
| `GET /v1/terms?q=` | Bearer + `meta` | 最多 5 条后端排序匹配 |
| `POST /v1/translations` | Bearer + `translate` | `{text, to?}` → `{kind, text, direction, dictionary_miss, request_id}` |

错误信封：

```json
{"error": {"code": "<枚举>", "message": "<短英文>"}, "request_id": "<服务端铸造>"}
```

码：`unauthorized` 401、`forbidden` 403、`rate_limited` 429、`payload_too_large` 413、`invalid_request` 400、`input_too_long` 422、`llm_unavailable` / `llm_budget_exhausted` 503、`internal` 500（超时中间件 504 仍用 `internal`）。入站 `X-Request-Id` **被忽略**。

Sites 成功体是上游字段的投影子集。Sites 错误体 **不同**：

```json
{"status": "unavailable", "reason": "<站点词汇>", "request_id": "<可选>"}
```

反代约定（`docs/deployment.md`）：`/wuwaterm-api/*` **剥前缀**；`/wuwaterm-web/*` **不剥**。API 只绑定数值 loopback（`validate_loopback_bind` 拒绝 `0.0.0.0`、主机名、甚至 `localhost`）。

### 3.7 预算（已核实）

限流器是 **每进程内存对象**。最坏情况是 **相加**（架构文档原话）：默认 bot 并发 4 + API 并发 2 = 6 路模型调用。重启清零。没有跨进程共享预算。

Sites 流量在 VPS 上算 **一个** 设备主体：一个滑动窗口、一份 LLM 预算。

---

## 4. Sites 产品 v1（PR #98）

### 4.1 怎么到 `HEAD` 的（已核实）

| PR | 合并 commit | 做了什么 |
|----|-------------|----------|
| [#96](https://github.com/My-Denia/wuwa-translate-bot/pull/96) | `f2b9e49` | 加入 `site/`：只证明 Hosted→VPS 的 `/v1/meta` 可行性；查词/翻译保持锁定；CI 增加 `site` job |
| [#97](https://github.com/My-Denia/wuwa-translate-bot/pull/97) | `e704579` | `/v1/terms` 改为返回后端排序的精确到模糊候选（Sites v1 依赖这份列表） |
| [#98](https://github.com/My-Denia/wuwa-translate-bot/pull/98) | `291d82d`（squash） | 换成工作台：`wuwaterm-proxy.js`、`/api/terms`、`/api/translations`、`translation-workbench.tsx`、`product-v1.test.mjs` |

#98 自身说明（已核实）：VPS API 运行时不变；不含持久存储、公开访问、自定义域名、分析、账号、Telegram 或 Windows 客户端改动。+1650 / −477，14 个文件。

#96 记录过一次已部署可行性结果（`api_version: v1`、`service_version: 0.4.0`、`term_count: 10951`）。那是 PR 正文里的历史观察，不是本检出能复现的线上状态。

### 4.2 运了什么（已核实）

- 单页：`WuwaTerm 私有翻译工作台`；徽章「私有翻译产品」；页脚「请求全程不在浏览器持有设备凭据」
- 两块面板：术语查询、整句翻译；文案写明查询/方向/排序/翻译决策都在 WuwaTerm 服务完成
- 服务端三个路由，全部 `dynamic = 'force-dynamic'`，只转给 `wuwaterm-proxy.js`
- 上游必须是 **恰好** `https://${WUWATERM_API_ALLOWED_HOST}/wuwaterm-api/`（可省略末尾 `/`）
- 主机钉死：小写 FQDN；拒绝 IP、端口、凭据、query/hash、`localhost`、`.local`、`.internal`、`.home.arpa`
- `redirect: 'manual'`；`credentials: 'omit'`；JSON Content-Type 严格；入站/上游 64 KiB
- 成功/错误体 `exactKeys` 白名单；投影字段不得反射 token / base URL / host
- 超时：meta/terms 8s；translations **100s**
- 本审计在检出上跑过：`cd site && node --test tests/*.test.mjs` → **55 passed, 0 failed**

### 4.3 与 VPS API 的隔离（已核实）

**隔离得住的：**

- 浏览器永远不持有设备 token、上游 URL、`Authorization`
- 客户端源与 `dist/client`、`dist/assets` 被 `verify:no-client-secret` 扫描
- 上游路径写死为 `v1/meta`、`v1/terms?q=`、`v1/translations`
- 不 follow 3xx；配置无效则 503 `site_not_configured` 且 **不 fetch**
- Python `tests/` **不** import `site/`；`scripts/validate.py` **不**跑 site
- Docker runtime **不**打进 `site/`

**隔离不住的：**

- Sites 边缘 **没有** 访问者认证。能打到部署 URL 的人（含 curl）就能花掉 `WUWATERM_SITE_DEVICE_TOKEN` 的配额与 LLM 预算
- 与 `/wuwaterm-web` 不同：没有 basic_auth、没有边缘头、没有会话
- 所有访问者共享 **一个** 上游设备主体

### 4.4 剩余风险（代码可见 = 已核实；其余 = 假设）

**已核实：**

1. 无访问者鉴权的 BFF；非浏览器客户端可直接 POST `/api/translations`
2. 输入上限不一致（查询 4096 vs 200；翻译体 64 KiB vs API 默认 32 KiB）
3. 主文档、CHANGELOG、SECURITY 范围、ADR 都未收录这条表面
4. 第三处凭据存放（Cloudflare / OpenAI Sites 环境）
5. 单设备限流桶；取消翻译只取消浏览器等待，文案写明服务端 LLM 可能继续
6. `proxyMetaRequest` 默认 `projectErrors: false`；非 200 meta 走较粗的状态映射，不像 terms/translations 那样解析上游错误 JSON
7. CI job 名仍是 `site feasibility security`，实际已跑 `npm test`（可行性 + 产品）

**假设（本树无法核实）：**

- **H1**：OpenAI Sites 平台用 URL 保密或账号 ACL 限制谁打得开——`hosting.json` 只有 `project_id` + 空 D1/R2
- **H2**：生产在 Sites 主机前还有 Cloudflare Access / IP 允许名单——仓库未表达
- **H3**：`WUWATERM_SITE_DEVICE_TOKEN` 被做成低配额专用设备——运维选择，不是代码

---

## 5. 安全与信任边界

### 5.1 边界清单（已核实，`docs/architecture.md` + 本审计补上 Sites）

1. **Telegram 更新是不信任输入。** 命令、回复 HTML、频道帖当数据不当指令（提示 + HTML 保护/还原）。
2. **HTTP 表面在 TLS 之后仍不信任。** 反代证明的是 **服务器** 身份。每个 `/v1` 都要设备凭据；体与时间有上限；`request_id` 只由服务端铸造。
3. **公网 HTTPS 终止在应用外。** 删一条反代路由即可收回暴露；API 绑定不变。
4. **秘密不进镜像、不进 builder。** serving 用 Compose `env_file`；builder 没有。
5. **`terms.db` 在候选校验后才信任**，运行时只读挂载；提升走 owner 脚本。
6. **可变状态本地、私有、按表面拆开。** bot ≠ API 凭据库。
7. **聊天控制面仍只通过 Bot API**（long polling）。
8. **Sites Worker 是新的半信任跳。** 它持有能花 LLM 的 Bearer。浏览器被锁住了；Worker 的调用方没有。

### 5.2 秘密处理（已核实）

- 设备 secret：stdin → scrypt 校验值；发行命令只打印 device id
- 桌面：keyring；配置文件无 token
- 进程内 web：token 留在服务器；cookie 不透明
- Sites：token 只在 Worker；客户端扫描
- 日志：API 完成记录带 `request_id`、路由模板、脱敏 `device=id:<8>`；不记 query、token、正文。`tests/test_api_request_logging.py` 卡住这条
- 校验错误不回显原始环境变量值（`docs/privacy-and-llm.md`）

### 5.3 泄漏门 vs 仓库级密钥扫描（已核实）

`docs/validation.md` 原文：

> Neither one looks for tokens, API keys or real Telegram identifiers, and **no gate in this repository does.**

挡凭据进提交的是：GitHub secret scanning + push protection（仓库设置，不在 `scripts/`）、`.gitignore`（`.env`、`state*`、`*.db`）、`scripts/check_package_artifacts.py`（wheel/sdist 成员）。

Sites 的 `verify-no-client-secret.mjs` 是 **包/源扫描**，不是 git 密钥扫描。它找环境变量名、金丝雀、`Authorization`、`Bearer`、禁止的浏览器存储 API。

### 5.4 主机钉死与 schema（已核实）

| 层 | 钉什么 |
|----|--------|
| API bind | 数值 loopback；Compose 写死 `127.0.0.1` |
| 桌面 | HTTPS（仅 localhost 允许 HTTP）；证书校验开；`trust_env=False`；请求目标必须与配置 origin 相同 |
| 文本门 `tests/test_client_transport_policy.py` | 禁止 SSH 转发配方、`verify=False`、`curl -k` |
| Sites | HTTPS + 主机 + `/wuwaterm-api/` 路径；拒绝重定向；JSON/字段白名单 |
| 频道回复索引 | `channel_reply_schema.py` 版本与类型 |
| OpenAPI | 提交的快照必须等于应用生成物 |

### 5.5 公开 vs owner-private（已核实）

| 表面 | 文档意图 | 代码实际门闩 |
|------|----------|--------------|
| Telegram 私聊 `/tr` | owner | `OWNER_USER_ID`；缺失则拒绝所有人 |
| Telegram 群 | 允许名单 / 公开模式 | `chat_settings.json` |
| `/v1/*` | 已登记设备 | Bearer + scope + 吊销重检 |
| `/healthz` 等 | 按设计无凭据 | 无认证 |
| `/wuwaterm-web` | owner-private，默认关 | 开关 + basic_auth + 边缘头 + 会话 |
| `site/` | PR 正文写 owner-private | **无访问者门闩**；只有 crawler 拒绝与 URL/平台假设 |

`SECURITY.md`「Scope / In scope」列出 bot、`/v1`、owner-private web、`client/`、data-build、`deploy/`。**没有 `site/`。** 这是文档漂移，不是「Sites 因此安全」。

`SECURITY.md`「Supported Versions」表仍写 **0.3.x 为最新已发布线**。CHANGELOG / README 的已发布线是 **0.4.0**。这是已核实的过期表，不是运行时缺陷。

---

## 6. 测试与 CI 地图

### 6.1 本地入口（已核实）

```bash
# 与 CI pytest (py3.x) 矩阵同一条命令
python scripts/validate.py
python scripts/validate.py --list
python scripts/validate.py --quick          # 跳过 pytest
python scripts/validate.py --client         # 再跑 client/.venv 的套件

# Sites（validate.py 不含）
cd site && npm ci && npm test && npm run typecheck && npm run lint
npm run build && npm run verify:no-client-secret

# 锁漂移（独立 CI job）
uv lock --check
```

`validate.py --list` 本审计跑过，六步为：`hygiene`、`non-goals`、`architecture`、`api-contract`、`ruff`、`pytest`。

### 6.2 测试文件（已核实：文件存在；条数不臆造未收集的函数总数）

**服务端 `tests/`：29 个 `test_*.py` + `conftest.py`。** 覆盖 API 认证/契约/loopback、进程内 web、请求日志泄漏、bot/频道、流水线、部署脚本文本、release workflow 不变量、客户端传输政策与文档断言、架构/卫生/非目标、构建器/DB、包产物。

**客户端 `client/tests/`：21 个 `test_*.py` + `conftest.py`。** 覆盖 API 客户端、TLS、config、keyring、Qt smoke、CJK 字面量门、打包 `--self-check`。本环境无 `client/.venv`，本审计 **未** 跑客户端套件。

**Sites `site/tests/`：2 个文件。** 本审计跑过 `node --test tests/*.test.mjs`：**55 passed**。另有构建后扫描 `scripts/verify-no-client-secret.mjs`（本审计未跑 `vinext build`，故未声称扫描通过）。

### 6.3 CI jobs（`.github/workflows/ci.yml`，每个 push / PR）

| Job | 跑什么 | 密钥 |
|-----|--------|------|
| `test` | Python 3.11–3.14 × `python scripts/validate.py` | 无 |
| `lock` | `uv==0.11.3` + `uv lock --check` | 无 |
| `site` | `npm ci`；`npm audit --json`（完整 + `--omit=dev`）；`npm test`；typecheck；lint；build；`verify:no-client-secret` | 无 |
| `package` | `python -m build`；`twine check --strict`；`check_package_artifacts.py`；干净 venv 安装冒烟 | 无 |
| `client` | Windows 3.12：pytest + `build.ps1` + 工作树必须干净 | 无 |
| `deploy-boundary` | `sh -n deploy/*.sh`；compose config；build runtime/builder；runtime 对 `build-db`/`refresh-data`/`verify-db` 退出 64 | 无 |

无 `needs:`；任一红则 workflow 红。

**路径过滤的 PR 工作流：**

- `release.yml`：改到 workflow / `deploy/**` / `client/**` / `pyproject.toml` 时 dry-run 构建；`dry_run=false` 的 `workflow_dispatch` 才推 GHCR、建 draft release
- `selfhost-smoke.yml`：改到 `deploy/**` 或 `docs/self-hosting.md` 时按指南走通容器路径（最长 60 分钟）

**咨询性：** `claude-code-review.yml`（Dependabot 跳过）；`claude.yml`（`@claude` 评论）。`CONTRIBUTING.md` 还提到 CodeQL 默认设置；本审计未打开仓库 Settings 核实。

### 6.4 门禁实际证明什么

| 门 | 证明 | 不证明 |
|----|------|--------|
| `validate.py` | 卫生、产品禁令、导入方向、OpenAPI 快照、ruff、服务端 pytest | 锁、打包、Windows 客户端、Docker、**Sites**、候选库、线上 Telegram |
| `site` job | 代理契约、无客户端秘密、能 build | 真 VPS 集成、访问者 ACL、hosted 环境注入 |
| `selfhost-smoke` | 指南的容器路径能装、能发凭据、能查、能译（mock LLM） | 每个 PR（路径过滤）；Telegram E2E |
| `release` dry-run | 资产能从该 commit 构建 | 发布已发生（draft + 人工 publish） |
| 候选 `verify_*.py` | 构建出的 `terms.candidate.db` 结构/种子/抽样精确命中/幂等 | 每次提交（有意排除） |

`docs/validation.md` 写「整 PR 还包含」锁、打包、Windows 客户端、Docker **四** 个 job。`site` job 已在 `ci.yml` 里，**该页没点名**。这是已核实的文档缺口。

### 6.5 缺口（已核实 vs 假设）

**已核实：**

- 仓库脚本不做 token/API key 扫描
- 候选库脚本不在 CI / `validate.py`
- 无 Telegram handler E2E
- Sites 测试 mock 上游
- `validate.py --client` 不在默认 Linux 路径
- 无 macOS/Linux 桌面构建
- 进程内 web 无浏览器 E2E（`tests/test_api_web.py` 是进程内 ASGI）
- 支持矩阵写服务端套件在 Windows host 上不受支持（目录 fsync、子进程 handle）

**假设：**

- branch protection 的 required checks 列表（不在树里）
- `npm audit --json` 在有 advisory 时是否让 job 失败：workflow 没有 `--audit-level`；**未在本审计中对有漏洞的树实测 exit code**
- CodeQL 是否为 required check

---

## 7. 影响当前 `main` 的近期历史

只记改变「现在该怎么读这棵树」的合并，不重放整个 0.4.0 战役。

| 合并 | 对当前 main 的意义 |
|------|-------------------|
| **#94** `5c3d31e` | 客户端 CJK 字面量门补上太玄经区 U+1D300–U+1D35F。纯测试。docstring 现在承认：门只覆盖文件枚举的东亚/标点/符号块，**不是**「任意 CJK」。Unicode 名扫描结构性看不到 MONOGRAM/DIGRAM/TETRAGRAM（与易经卦画同类）。`CHANGELOG` Unreleased 的 Desktop Client 段覆盖 #65/#93/#94 这条线。 |
| **#96** `f2b9e49` | 仓库第一次有 `site/`。只证明 metadata BFF。引入 Worker 持有 Bearer 的新信任跳。 |
| **#97** `e704579` | `/v1/terms` 开始返回模糊候选。服务端 0.4.1。Sites 工作台按后端顺序渲染这张列表。 |
| **#98** `291d82d` | 当前 HEAD。Sites 从「可行性预览」变成查词+翻译工作台。VPS 运行时按 PR 说明不变。主文档仍按四条表面说话。 |

相邻、仍影响读树的提交：#95 把 `/authorize` 换成 `/grant`；#93 把 CJK 门从「白名单 setter」扩成 AST 全量；#86/#87 定下无 tag 的 draft release 与 0.4.0 资产政策。

---

## 8. 发现

### 8.1 扎实之处（已核实）

1. **应用层只持有一次。** `application.py` 协议无关；架构门限制 API 导入。Telegram 与 HTTP 不能各写一套翻译。
2. **词典优先是代码，不是口号。** 精确命中不碰 LLM；模糊短路有明确谓词；占位符完整性失败则 fail-closed。
3. **两套身份互相不能提权。** 设备吊销不影响群；`/grant` 不影响 API。
4. **API 默认攻击面小。** loopback 写死；设备 scrypt；体/时/鉴权池有上限；完成日志被测过不泄漏。
5. **进程内 web 的门闩是认真的。** 默认关、无脚本、边缘头、不进 OpenAPI。
6. **桌面是消费方。** 无翻译逻辑；TLS 与「禁止经管理通道到达服务」有文本+运行时测试。
7. **构建器与运行时分离。** runtime 拒数据命令（退出 64）；builder 无 runtime 秘密。
8. **Sites 代理本身测得很密。** 主机钉死、重定向拒绝、schema 白名单、金丝雀不反射、客户端恰好三次同源 fetch——55 个 node 测试在本检出上绿。
9. **发布管线谨慎。** 无 tag 触发；PR 路径 dry-run；write scope 到不了 PR；`tests/test_release_workflow.py` 钉住这些不变量。

### 8.2 脆弱或不一致（已核实）

1. **文档仍画四条表面，代码已有五条。** README、架构、CONTRIBUTING、SECURITY、CHANGELOG、ADR 都不提 `site/`。陌生审阅者会把 Sites 误认为 `/wuwaterm-web`，从而读错鉴权。
2. **两条「私有 web」信任模型相反。** `/wuwaterm-web`：多层门闩，默认关。`site/`：公开 BFF + 服务端 token，靠平台/URL 假设。
3. **`SECURITY.md` 过期。** 支持表停在 0.3.x；范围不含 `site/`。
4. **`docs/validation.md` 的 CI 叙述过期。** 仍写四个额外 job；`site` 是第五个始终跑的 job。
5. **CHANGELOG Unreleased 没有 #96/#98。** 有 0.4.1 terms、`/grant`、CJK 门。按本仓库自己的规则，这是文档缺陷。
6. **输入上限漂移。** Sites 比 VPS 更宽，再投影错误——能用，但操作者/用户看到的是站点词汇，不是第一道对齐的拒绝。
7. **LLM 预算是每进程之和。** 再加 Sites 作为第二个 API 调用者，最坏情况仍是「API 桶 + bot 桶」，但 Sites 与桌面 **共享** 那个设备桶——若共用一个 token，桌面与站点会互相占额度。
8. **ruff 只开 E4/E7/E9/F。** 有意做成正确性门，不是风格门。

### 8.3 具体风险（按置信度）

| ID | 风险 | 证据 | 级别 |
|----|------|------|------|
| R1 | 任何能到达 Sites URL 的人都能触发查词与 **计费 LLM 翻译** | `site/app/api/*/route.ts` 无鉴权；代理附加 Bearer | 高（若 URL 或平台 ACL 弱）；若 H1/H2 为真则降为中 |
| R2 | 单设备主体被 Sites 流量耗尽 → 桌面/其他 API 调用者 429 或 `llm_budget_exhausted` | 每设备限流；Sites 共用一个 token | 中 |
| R3 | Worker 密钥泄露 = 完整 API 能力（若 token 带 `translate`+`meta`） | 与桌面同一设备模型 | 中；取决于签发范围（**假设 H3**） |
| R4 | 未认证调用方占满 scrypt 鉴权池 → 合法调用者 429 | ADR 0010/0012 已记录；树内无入口限流器 | 中（已知残留） |
| R5 | `/wuwaterm-web` 同站 CSRF | ADR 0014 已记录 | 低–中，需同站恶意页 |
| R6 | 运维按 README 行事会完全看不见 Sites | 文档真空 | 中（配置漂移 / 忘记轮换第三处 token） |
| R7 | 未来有人把 Sites 当成「已文档化的 owner 门闩」 | SECURITY 范围 + UI 文案 | 中（社会/审查风险） |

### 8.4 下一步最值钱的审计或修复（建议，不是本 PR 的工作）

按杠杆排序：

1. **给 `site/` 补文档，或显式标成「无文档、owner 自担」。** 最低成本一致性：`docs/architecture.md`、`SECURITY.md` 范围、`CHANGELOG`、`docs/validation.md` 的 job 列表。不要把 Sites 写进 `/wuwaterm-web` 指南。
2. **Sites 访问者门闩。** 平台 ACL、Cloudflare Access，或应用层秘密。现在的「私有」是标签。
3. **专用、收紧的 Sites 设备。** 只签必要 scope；比桌面更低的 VPS 配额；轮换与第三处存储写进 `docs/deployment.md`。
4. **把 Sites 输入上限与 VPS 对齐**（200 / 32 KiB），或在代理里预检并返回同一批 reason。
5. **修正 `SECURITY.md` 支持表**（0.4.x）并决定 Sites 是否属于漏洞报告范围。
6. **入口侧缓解鉴权池耗尽**（ADR 已点名的缺口）。
7. **进程内 web 的 CSRF token**（ADR 0014 残留）。
8. **Sites↔VPS 集成测试**（mock 之外）：钉死的 staging 主机，或录制契约。不要把生产 token 放进 CI。

### 8.5 假设 vs 已核实（总表）

| 陈述 | 标签 |
|------|------|
| `HEAD` = `291d82d`，#98 squash | 已核实 |
| 四条文档表面 + 第五条 `site/` | 已核实 |
| Sites 浏览器不持有设备 token | 已核实（源 + 测试 + 扫描脚本） |
| Sites 无访问者鉴权 | 已核实（路由代码） |
| 生产 Sites URL 仅 owner 可开 | **假设 H1/H2** |
| Sites token 是低配额专用设备 | **假设 H3** |
| 服务端 0.4.1 / 客户端 0.2.0 / site 0.1.0 | 已核实（包元数据） |
| 最新 GitHub release 线是 0.4.0 | 已核实（CHANGELOG 日期）；SECURITY 表仍写 0.3.x |
| 本审计中 site node 测试 55 绿 | 已核实 |
| 本审计跑过完整 `scripts/validate.py` pytest | 见文末验证节 |
| branch protection 集合 | **假设** |
| 线上 VPS 主机名、token 数量、LLM 花费 | **未检查**（有意） |

---

## 附录 A. 审阅者速查命令

```bash
git rev-parse HEAD
# 期望：291d82d80c40a870b7bde9483486e60d37cc5669

python scripts/validate.py --list
# hygiene, non-goals, architecture, api-contract, ruff, pytest

python scripts/validate.py          # CI test job
cd site && npm test                 # 55 个可行性+产品测试
cd client && python -m pytest       # Windows / client/.venv
```

关键契约文件：

- 流水线：`src/wuwaterm/application.py`
- 设备 token：`src/wuwaterm_api/auth.py`
- 已发布 API：`docs/api/openapi.json`
- 进程内 web：`docs/web-presentation-layer.md`、ADR 0014
- Sites 代理：`site/lib/wuwaterm-proxy.js`
- 拓扑与预算：`docs/architecture.md`
- 校验范围：`docs/validation.md`（记住：它还没把 `site` job 算进「整 PR」）

## 附录 B. 本审计跑过的命令

在 SHA `291d82d80c40a870b7bde9483486e60d37cc5669` 上：

| 命令 | 结果 |
|------|------|
| `git rev-parse HEAD`（审计开始时的 `main`） | `291d82d80c40a870b7bde9483486e60d37cc5669` |
| `.venv/bin/python scripts/validate.py --list` | 六步如上 |
| `cd site && node --test tests/*.test.mjs` | 55 passed, 0 failed |
| 全树搜索主文档中的 `site/` / `WUWATERM_SITE` | 除 `ci.yml` 与 `site/` 自身外无命中 |
| `.venv/bin/python scripts/validate.py`（加入本文件后） | 前五步绿；pytest **1041 passed**，**3 failed**，49 warnings |

那 3 个失败全在 `tests/test_builder.py`，根因相同且与本文件无关：`legacy_checkout` 夹具检出的 `origin` 被写成 `https://x-access-token:[REDACTED]@github.com/Dimbreath/WutheringData.git`，而 `inspect_data_source` 要求与 profile 里的裸 `https://github.com/Dimbreath/WutheringData.git` 字节相等。这是本 Cloud Agent 检出注入 tokenized remote 的环境现象，不是 `291d82d` 上的产品回归。本审计未改 `src/`、`site/` 运行时或任何测试。
