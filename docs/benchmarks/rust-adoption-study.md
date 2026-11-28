# Rust / PyO3 / WASM 跨端产品级采用研究与实测报告

日期：2026-11-28  
基线提交：`93c33af`（`origin/main`）  
复现脚本：[`scripts/benchmark_rust_adoption.py`](../../scripts/benchmark_rust_adoption.py)  
关联工作区：[`crates/`](../../crates/README.md)、[`src/wuwaterm/native_kernel.py`](../../src/wuwaterm/native_kernel.py)

---

## 1. 现状盘点（已验证事实）

### 1.1 代码与绑定现状

1. **共享 Rust 内核（[`crates/wuwaterm-text-kernel/`](../../crates/wuwaterm-text-kernel/)）**：纯 `std` 无外部依赖，当前实现 `text_spans`（`segment_core`、`paragraph_ranges`、`sentence_ranges`、`sentence_ranges_v2`、`review_sentence_ranges_v2`、标量↔UTF-16/UTF-8 坐标映射）、`segmentation`（`segment_text`、`pair_segments`）及 UTF-8 Aho-Corasick 多模匹配（`AhoCorasick`、`select_longest_non_overlapping`）。
2. **PyO3 绑定（[`crates/wuwaterm-text-kernel-py/`](../../crates/wuwaterm-text-kernel-py/)）与 Python 适配层（[`src/wuwaterm/native_kernel.py`](../../src/wuwaterm/native_kernel.py)）**：
   - `native_kernel.py` 仍是**未接线的可选模块**：仅在 [`tests/test_native_kernel.py`](../../tests/test_native_kernel.py) 与差分脚本 [`crates/scripts/run_differential_and_bench.py`](../../crates/scripts/run_differential_and_bench.py) 中被调用；生产路径 [`term_matching.py`](../../src/wuwaterm/term_matching.py)、[`sentence.py`](../../src/wuwaterm/sentence.py)、[`structural_checks.py`](../../src/wuwaterm/structural_checks.py) 与 [`normalize.py`](../../src/wuwaterm/normalize.py) 当前均未调用 `native_kernel`。
   - **关键基线变化**：`#354`（`5a881ee`）已将纯 Python Unicode 标量 Aho-Corasick（`TermSpanMatcher`）合入 `term_matching.py` 并缓存于 `DictionarySnapshot`。因此，当前 PyO3 对在线术语匹配的边际收益不再是早期调研报告（`#314`）中相对 `19,175` 次 `str.find()` 朴素循环的 `300x`，而是**原生 Rust AC 相对纯 Python `TermSpanMatcher` AC** 的差值。
   - **未覆盖的纯 Python 阶段**：`normalize_prose_text`（NFKC + 正则规范化）、`_matched_span_ok`（ASCII/大小写边界、单字人名上下文、jieba `crosses_cjk_word_boundary` 切片拦截）与 `structural_checks.check_segment_structure`（行/段布局、`Decimal` 数值配对、标签平衡与 `{Cus:...}` 模板解析）**目前均无 Rust 实现**。
3. **WASM 绑定（[`crates/wuwaterm-text-kernel-wasm/`](../../crates/wuwaterm-text-kernel-wasm/)）**：
   - 目标 `wasm32-unknown-unknown`，通过 JSON 线性内存 ABI + 零打包器 ESM 封装 [`js/index.mjs`](../../crates/wuwaterm-text-kernel-wasm/js/index.mjs) 向浏览器、Web Worker 和 Node.js 暴露上述同源内核（含可复用句柄 `TermAutomatonHandle` 与一次性 `scanTerms`）。

### 1.2 本机实测工具链与未测项说明

| 工具 / 目标 | 本机状态 | 说明 |
| --- | --- | --- |
| `cargo` / `rustc` | `1.98.0` | 可直接编译 `wuwaterm-text-kernel` 与 `wasm32-unknown-unknown` |
| `wasm32-unknown-unknown` | 已安装 | `cargo build -p wuwaterm-text-kernel-wasm --target wasm32-unknown-unknown --release` 成功且 Node.js `40/40` 契约全过 |
| `node` | `v22.22.0` | 用于实测 `.wasm` 冷加载、句柄构建与 JS↔WASM 跨边界开销 |
| `maturin` | 隔离临时虚拟环境 `1.16.0` | 全局未安装；在临时 venv（Python `3.12.3`，Unicode `15.0.0`）成功编出 `cp312-manylinux_2_34_x86_64` wheel 并完成 `1,112,064` 标量差分验证与端到端实测 |
| `wasm-pack` | 缺失 | **未运行，原因**：本仓 WASM 包采用零打包器原生 `cargo build` + [`js/index.mjs`](../../crates/wuwaterm-text-kernel-wasm/js/index.mjs)（与 CI [`.github/workflows/rust-text-kernel.yml`](../../.github/workflows/rust-text-kernel.yml) 一致），无需 `wasm-pack`；未启用 `wasm-opt` 二次裁剪，故下方 WASM 体积为 `rustc --release` 原始产物 |
| 真实语料与词典 | 可读 | `cjk-census-3.7.db`（`19,175` 锁定词条表面）、`eval-corpus`（`A3` 公告等）及 `wutheringdata` `MultiText.json`（实测 `277,917` 非空行，共 `6,729,837` 字符） |

---

## 2. 产品级实测结果与边界拐点

以下全部数据均由 `scripts/benchmark_rust_adoption.py --json` 在 Linux `x86_64`（CPython `3.12.3`、Unicode `15.0.0`、Node.js `v22.22.0`）单次完整运行实测获得；关闭原生扩展基线使用 `WUWATERM_DISABLE_NATIVE_KERNEL=1` 验证。

### 2.1 制品体积、冷启动与内存增量

| 制品 / 指标 | 原始体积 | Gzip-9 体积 | 冷启动耗时（p50） | RSS 内存增量 |
| --- | --- | --- | --- | --- |
| 纯 Python `text_spans` + `segmentation` | 轮子内置 | - | `10.069 ms` | `+2.07 MiB`（基线 `11.24 MiB`） |
| PyO3 扩展 `.so`（`cp312-linux_x86_64`） | `832,096 B`（`812.59 KiB`） | `316,201 B`（`308.79 KiB`） | `0.516 ms` | `+0.64 MiB` |
| PyO3 Wheel（`.whl`） | `329,853 B`（`322.12 KiB`） | `328,861 B`（`321.15 KiB`） | - | - |
| WASM 二进制（`wasm32` release） | `318,682 B`（`311.21 KiB`） | `107,262 B`（`104.75 KiB`） | `13.403 ms`（`loadKernel`） | `+6.62 MiB`（Node 基线 `48.86 MiB`） |
| WASM ESM 包装（`js/index.mjs`） | `6,901 B`（`6.74 KiB`） | `1,810 B`（`1.77 KiB`） | - | - |
| 19,175 词表 AC 自动机构建（Python） | - | - | `167.059 ms` | `+5.67 MiB` |
| 19,175 词表 AC 自动机构建（PyO3） | - | - | `20.352 ms`（`8.2x`） | `+16.84 MiB` |
| 19,175 词表 AC 自动机构建（WASM） | - | - | `26.505 ms` | `+23.50 MiB` |

> **关键内存观察**：生产 VPS `wuwaterm-api` 容器配置了 `mem_reservation: 96 MiB` 与宿主 `memory.low: 128 MiB`（见 [`docs/products.md`](../products.md)）。Rust UTF-8 状态机（`169,387` 状态）虽然构建比 Python 快 `8.2x`，但一次性 RSS 增量约 `+16.8 MiB`（vs Python `TermSpanMatcher` `+5.7 MiB`），在低内存容器上并非“免费午餐”。

### 2.2 FFI / 跨边界微调用拐点（p50 ms）

#### Python ↔ PyO3 边界

| 操作 | 负载规模（标量） | Pure Python（ms） | PyO3 `native_kernel`（ms） | 直调 `_wuwaterm_text_kernel`（ms） | 净倍率 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `scalar_to_utf16` | `47`（短句） | `0.0009` | `0.0007` | - | `1.29x` | 基本打平；纯 ASCII 短串 Python fast-path 无优势 |
| `scalar_to_utf16` | `2,000`（长段） | `0.0319` | `0.0015` | - | `21.27x` | 拐点之后 Rust 线扫明显胜出（省 `~30 µs`） |
| `scalar_to_utf16` | `10,000`（超长） | `0.1023` | `0.0029` | - | `35.28x` | 随标量线性拉大 |
| `segment_text` | `47`（短句） | `0.0035` | `0.0017` | - | `2.06x` | 绝对收益仅 `1.8 µs` |
| `segment_text` | `2,000`（长段） | `0.1091` | `0.0083` | - | `13.14x` | 省 `~0.10 ms` |
| `segment_text` | `10,000`（超长） | `0.5455` | `0.0312` | - | `17.48x` | 省 `~0.51 ms` |
| `term_scan_select` | `47`（短句） | `0.0067` | `0.0020` | `0.0015` | `3.35x` | 绝对收益 `4.7 µs` |
| `term_scan_select` | `2,000`（长段） | `0.2106` | `0.0499` | `0.0429` | `4.22x` | `native_kernel.py` 元组重打包占 Rust 调用 `~14%` |
| `term_scan_select` | `10,000`（超长） | `0.9263` | `0.1034` | `0.0811` | `8.96x` | 长文本下 `native_kernel.py` 重建列表开销占比升至 `21.6%` |

- **包装层实测浪费**：[`native_kernel.py`](../../src/wuwaterm/native_kernel.py#L383-L391) 在 `find_all` 与 `scan_and_select` 中对 PyO3 已返回的 `list[tuple[int, int, int]]` 再做一次 `[(int(a), int(b), int(o)) for a, b, o in raw]`，在 `10,000` 标量与 277k 批量调用中平白增加 `~22%` 耗时。

#### JS ↔ WASM 边界（Node.js `v22.22.0`）

| 操作 | 负载规模（标量） | Pure JS 基线（ms） | WASM 句柄复用（ms） | WASM 一次性 `scanTerms`（ms） | 结论 |
| --- | --- | --- | --- | --- | --- |
| `scalarToUtf16` | `47`（短句） | `0.0013` | `0.0037` | - | **WASM 慢 `2.8x`**（JSON 序列化 + `TextEncoder` 占主导） |
| `scalarToUtf16` | `2,000`（中文长段） | `0.0234` | `0.0180` | - | 刚过拐点（`1.3x`） |
| `scalarToUtf16` | `10,000`（英文超长） | `0.0167` | `0.0358` | - | **WASM 慢 `2.1x`**（V8 对纯 ASCII `for..of` 极快，JSON ABI 反成负收益） |
| `segmentText` | `47` / `2,000` / `10,000` | - | `0.0040` / `0.0267` / `0.0793` | - | 短串 JSON 开销 `~4 µs`，长串吞吐高 |
| `pairSegments` | `15,585`（`A3` 双语） | - | `0.1535` | - | 整篇双语公告切分+配对仅 `0.15 ms` |
| `term_scan_select` | `47`（短句） | `0.4342`（`indexOf`） | `0.0084`（`51.7x`） | **`20.6790`（慢 `47.6x`）** | 一次性 `scanTerms` 每次重序列 `19,175` 词条，绝对禁止用于热路径 |
| `term_scan_select` | `2,000`（长段） | `3.4090`（`indexOf`） | `0.1041`（`32.7x`） | **`15.5934`（慢 `4.6x`）** | 必须持有持久化 `TermAutomatonHandle` |
| `term_scan_select` | `10,000`（超长） | `13.7218`（`indexOf`） | `0.2324`（`59.0x`） | **`16.0843`（慢 `1.17x`）** | 长文本配合复用句柄净胜显著 |

### 2.3 产品级端到端工作流对照

| 产品工作流 | 当前纯 Python 生产基线 | 接入 PyO3 `native_kernel`（替换切分与 AC 扫描） | 净倍率 | 真实瓶颈归因 |
| --- | --- | --- | --- | --- |
| **短句单请求 E2E（47 标量）**：`normalize` + `segment` + `lock_terms` + `check_segment_structure` | `0.1919 ms`（其中 `structure_check` `0.1200 ms`，`lock_terms` `0.0485 ms`） | `0.1911 ms`（`structure_check` `0.1200 ms`，`lock_terms` `0.0443 ms`） | **`1.00x`** | FFI 与未加速的 Python 门禁/结构检查完全抵消内核收益 |
| **长段单请求 E2E（2,000 标量）**：`normalize` + `segment` + `lock_terms` + `check_segment_structure` | `5.6896 ms`（`structure_check` `3.5809 ms` 占 `62.9%`） | `4.8808 ms`（省 `0.81 ms`） | **`1.17x`** | 相对 1,000–5,000 ms LLM 网络延迟占比 `<0.08%`；`structure_check` 仍在 Python |
| **500 段文稿全流程（`55,430` 字符）** | **`102.013 ms`**（`normalize` `1.916` + `seg/pair` `6.854` + `structure` `82.413` + `lock` `10.830`） | **`93.430 ms`**（`normalize` `1.916` + `seg/pair` `2.831` + `structure` `82.413` + `lock` `6.270`） | **`1.09x`** | **`structure_check` 占总耗时 `80.8%`（`82.4 ms`）**，拉低整体上限 |
| **277,917 行 `MultiText.json` 纯 AC 扫描（`6.73M` 字符）** | `1,388.38 ms`（纯 Py `TermScanner`） | `569.03 ms`（包装）/ `426.66 ms`（直调） | `2.44x` / `3.25x` | 277,917 次短行跨越 FFI 边界累计 ~`420 ms`；`#354` 已落地纯 Py AC |
| **277,917 行 `MultiText.json` 含门禁 E2E（`select_term_spans`）** | **`3,081.95 ms`**（`134,194` 命中） | **`1,675.92 ms`**（PyO3 AC + Python `_matched_span_ok`） | **`1.84x`** | Python `_matched_span_ok` + jieba 边界检查吃掉 `~1,107 ms`（`66%`） |
| **WASM 500 段文稿 `segment + scan`（Node v22）** | - | `5.145 ms`（含跨边界 JSON 开销，不含 `structure_check`） | - | 若在 Web 端替代 `indexOf` 扫描+分段，CPU 极低 |

**端到端核心判断**：
1. **单次在线 API 请求（短句/长段）**：PyO3 对端到端延迟只带来 `0%–15%`（`0.00–0.81 ms`）的微弱下降，对外部 LLM 响应时间（秒级）完全不可感知，反而引入跨平台 ABI 构建和容器内存开销。
2. **多段本地审校与结构校验**：只要 `structural_checks.py`（正则 + `Decimal`）和 `_matched_span_ok`（jieba 词界）仍留在 Python，即便 500 段批量工作流，PyO3 净收益也仅有 **`1.09x`**。
3. **离线 `build-db` 277k 行大语料扫描**：纯扫描有 `2.44x–3.25x`、含门禁 E2E 有 `1.84x`（从 `3.08s` 降至 `1.68s`），属于省下 `~1.4 秒` 的离线构建优化，并非紧急瓶颈。

---

## 3. 三端工程采用建议与量化决策表

### 3.1 总体采用立场

| 端 | 当前生产状态 | 近期是否默认接入 | 建议定位 | 必须先满足的采用门槛（Gate） |
| --- | --- | --- | --- | --- |
| **Core（Python API + Builder）** | 纯 Python（`py3-none-any` wheel `589,987 B`；`#354` 已合入 `TermSpanMatcher`） | **否**（保持可选） | 维持 `native_kernel.py` 为 opt-in，暂不将 `_wuwaterm_text_kernel` 设为必需或默认依赖 | 仅当以下两项同时成立时开启可选 wheel 部署：① 离线批处理/高频 `/v1/structure-checks` 实测单核 CPU 打满（端到端节省 `>= 20 ms/req` 或构建省 `>= 30s`）；② 将 `_matched_span_ok` + `structure-check-v1` 一并迁入 Rust 并提供**批量 FFI 接口**（单次跨边界传 `list[str]`），且容器内存余量能承受 `+17 MiB` 常驻自动机 |
| **Web（`WuwaTerm-Web`）** | `v0.7.0`（`df0a4d0c`，发行包 `33.6 MB`），纯 JS/TS | **有条件推进**（仅作为共享契约单源实现，不急于大改） | **三端中最高结构收益点**：用一份 `318 KiB`（gzip `104.7 KiB`，占发布包 `<1%`）的 `.wasm` 统一 `segmentation-v1` + Unicode 15.0 `isdigit()` + UTF-16 映射 | ① **必须**在 Web Worker 中异步调用 `loadKernel`（`~13.4 ms` 编译实例化不阻塞 UI）；② **必须**持有持久 `TermAutomatonHandle`（严禁调用一次性 `scanTerms`）；③ 单函数微调用如 `scalarToUtf16` 留在 JS，仅对 `segmentText` / `pairSegments` / 多词匹配跨边界调用（批量或单段 `>= 500` 标量） |
| **Windows（`WuwaTerm-Windows`）** | `v0.9.0`（`e6835f38`，PyInstaller `>= 6.10` one-folder，CPython `3.12`，ZIP `58.56 MB`） | **否** | 纯 HTTP 客户端本地仅做编辑器轻量文本分段/稿件格式处理（`<1 ms` 级别），无需引入本地 C/Rust 原生扩展 | 仅当桌面端未来新增**本地离线词库检索/整本数千段即时结构+术语检查**且纯 Python 卡顿超 UI 帧预算（`>16 ms`）时，才在 `windows-latest` MSVC CI 中打包 `cp312-win_amd64.pyd` 并绑定 PyInstaller 版本锁 |

### 3.2 可执行工程改进清单（不阻塞当前架构）

1. **消除 `native_kernel.py` 冗余解包开销（低风险）**：在后续触及 [`src/wuwaterm/native_kernel.py`](../../src/wuwaterm/native_kernel.py#L383-L391) 时，移除 `TermScanner.find_all` / `scan_and_select` 中对 PyO3 返回值再次构造 `[(int(a), int(b), int(o)) ...]` 的列表推导（本次实测此项在 277k 行批量扫描中造成 `569.03 ms` vs `426.66 ms` 的 `25%` 额外开销）。
2. **禁止在 Web 端暴露一次性 `scanTerms` 给交互路径**：在 [`crates/wuwaterm-text-kernel-wasm/js/index.d.mts`](../../crates/wuwaterm-text-kernel-wasm/js/index.d.mts) 与前端接入规范中明确标注 `scanTerms` 仅用于单次测试，生产调用必须复用 `createTermAutomaton()` 句柄。
3. **CI 回归基线**：可用 `python scripts/benchmark_rust_adoption.py --quick --json` 在纯 Python（无原生扩展）与 WASM 环境下做轻量结构回归检查，不引入真实语料或原生编译强依赖。
