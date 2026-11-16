# 翻译质量与请求体验改进：2026-11-16

状态：已通过独立计划评审。真实模型调用仍须等待完整清单和哈希前置条件满足。

Owner 本轮原话：
> 按这个优先级开下一批，先修官方名称和关键错译，英译中延迟和 provider 兼容并行推进。沿现有票做，H1 不再当新盲测。

## Outcome

沿 MY-380／MY-18 优先修官方名称、关键语义错；MY-376 延迟、MY-230 兼容并行。不要求本批消灭全部 13 个关键错。

## 基线与冻结

基线源为 `5c636cbf0953530380186a0909001a01de422df3`、Core 0.9.1，数据保持 3.7／`9218d612`。本批首次真实模型调用前（包括 baseline、D9 controlled 测量）冻结完整目标 case list、判据、预算和基线配置；每臂固定源码、DB、prompt、scorer、inputs、judging 配置哈希、matcher、model 及 settings。候选臂的实际提交与产物哈希在该臂首次调用前补入同一追加账本。旧 19/28 不作当前基线。保留 zh-en disabled、en-zh omit 及上批已接受回退，不修改冻结 ADR 0020 或旧结果。

## 三线并行写集

1. 主线独占 `src/wuwaterm/{sentence,builder,cjk_span}.py` 及对应测试。先 D-003/004/005/008/009/012；按完整上下文定位名称、否定、遗漏、指代、主体反转，修通用来源／reference 机制，保留普通词区分和 Tuner 冲突约束。
2. Provider 独占 `llm_transport.py`、专属测试及配置指引。复用 `fix/provider-thinking-compat` 和 35 条真实 Core＋MockTransport 离线基线断言，当前源码零 diff。仅明确 unsupported thinking/effort 触发专门诊断；提供现有方向变量显式 omit 指引，说明它交还 provider 默认。再用 strict 本地 HTTP 真实收包验证拒绝／omit 成功、普通 400/401/429/5xx 原错保留且各一次 POST。此线需要 0 次模型调用。不加同义 flag、不自动重发或静默去字段。
3. EN 独占新批私有 harness／分析记录。线上自然日志 baseline 与同生产 gateway/settings 的 D9 controlled 测量分开，保持 omit、L1800、模型 45 秒／请求 90 秒超时。用现有 pre_send/response 采 timing/usage；缺队列或 thinking／生成数据就记不知道。EN 不等待 provider 新接口；三线只共享既有 transport 接口，各自提交，集成人顺序合入后锁候选。

## Done

- D 各方向官方名准确率不降，关键错数、普通词错数不增；预冻完整目标中名称至少修正 2 处、语义关键错至少 1 处，禁止事后挑例；各方向 A＋B 不降。
- R 报告冻结字面 cue score：floor 664/675，Set 1 ≥119/124、Set 2 ≥38/39；664 是已接受发布基线，原 670 守卫没过。真实语义错、普通词误锁另列比较。
- EN 目标 p95 ≤30 秒、0 timeout，报告 n／p50／p95／max；provider 瓶颈或无净收益则以证据 no-code 结案，不提高 timeout 冒充加速。
- 对应行为测试及 `python scripts/validate.py` 通过。

## H2 与判分

固定选预留 H2，最终候选冻结且上述哈希完整提交后首次双臂验收；不据结果换集或继续调参。按 ADR 0020 披露 H1／H2 共享 1,841 个未清除 12 标量窗口，其中公告 1,623；不称完全独立新语料。四 genre 单列，各方向 A＋B ≥80%、90% stretch，A 另报；报告文档数、Wilson 区间及分母：段落率含全部计分段，失败／undecidable 不算可用；名称按标注出现次数，关键错按已判段及千源标量，失败排除量另列。

本批判分平台变更在首次真实调用前冻结并追加登记：当前执行环境使用 GPT-6.1 Sol，沿用同一 rubric，两名独立新上下文 reviewer 只读返回 judgments，两臂盲混、第三上下文仲裁、25% 基线漂移控制，由 evaluator 落盘。上一批 Claude 判分记录保留；不将本批写成沿用了相同评判平台。H1 暂只用既有聚合归因；D 不足再单独经 evaluator 导必要 used 错例、记录范围，作诊断／回归，不事后改主验收 case list；fixer 不遍历 H1／H2 root。

## Budget

旧账 182，450 stop 余 268、500 cap 余 318；新批 ≤200，总量最多 382。起步 D 双臂约 16＋D9 约 9；冻结后全 D＋R 双臂约 102；H2 双臂约 58；余 15 供必要诊断／失败重试。沿同 gateway 及累计账本，q4 私有行式记录新 method／arms，H2 另标；所有尝试 POST 累计，完整 reuse key 一致才复用。evaluator 串行实测、每次重算余量，不清零或借 phase 绕限。

## Ops 与回滚

执行时沿原票追加 owner 决定、因果和证据；既有 CI／审查后 PR → Core 发布 → Telegram pin 采用 → 现网版本／DB／双向 smoke，记录真实发布 SHA。保留旧代码／镜像／DB／方向配置用于回滚，保护 `.env`／`data/`。

## 其余真实交互验收并行

主代理另派原生验收 worker 沿 MY-201，在已发布 Windows 0.5.0 和现有 150% 单屏环境，检查真实中文 IME 组合输入、候选窗位置、提交／撤销；截取组合态与提交态证据，不重跑已接受的全部长稿验收。使用已有客户端与本机设施，不改长期设置，不新签凭据，不发送翻译请求。缺少实际输入法或控制能力时记录具体缺项。

混合 DPI 拖窗仍沿 MY-371；无第二块不同 DPI 实屏时直接记录没做，不用虚拟屏冒充。主代理负责把新增实机结果及缺项回填 MY-201／MY-371。Telegram 自然使用回执继续沿 MY-16／MY-282 观察已存在的自然使用证据，不发测试消息；无完整自然回执时仍记录不知道。剩余交互验收不阻塞本批可独立交付的后端改进，上批阶段结论保持关闭。

## Non-goals

不重开上批、不动 T3 遗留／旧私评；不做 H3、全名硬锁、逐词例外、新服务、四仓重排、客户端广改或额外 ops 审计。
