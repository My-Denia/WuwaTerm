# Architecture Decision Records

Accepted decisions that shape the **running** system. Each ADR links to
current evidence (modules, deploy scripts, tests). Hollow or future-only
records are intentionally absent.

| ID | Title | Status |
|----|-------|--------|
| [0001](0001-telegram-as-presentation-layer.md) | Telegram as presentation layer | Accepted |
| [0002](0002-modular-monolith.md) | Modular monolith | Accepted |
| [0003](0003-long-polling-not-webhook.md) | Long polling (not webhook) | Accepted |
| [0004](0004-sqlite-terminology-data.md) | SQLite terminology data | Accepted |
| [0005](0005-file-backed-single-instance-state.md) | File-backed single-instance state | Accepted |
| [0006](0006-dictionary-first-before-llm.md) | Dictionary-first before LLM | Accepted |
| [0007](0007-runtime-builder-separation.md) | Runtime / builder separation | Accepted |
| [0008](0008-candidate-verification-and-transactional-deployment.md) | Candidate verification and transactional deployment | Accepted |
| [0009](0009-http-api-adapter.md) | HTTP API as a second presentation adapter | Accepted |
| [0010](0010-device-principal-authentication.md) | Device-principal authentication | Accepted |
| [0011](0011-pc-client-stack.md) | PC client stack and transport policy | Accepted |
| [0012](0012-client-transport-selection.md) | How the desktop client reaches the service | Accepted |
| [0013](0013-client-ui-visual-and-layout.md) | 桌面客户端的视觉系统与布局选型 | Partly superseded by 0016 |
| [0014](0014-private-web-presentation-layer.md) | Private web presentation layer | Accepted |
| [0015](0015-desktop-manuscript-workflow.md) | 桌面稿件审校与保存 | Accepted |
| [0016](0016-desktop-resonance-workbench.md) | 以双语稿件为中心的桌面工作台 | Accepted direction; implementation subject to validation |
| [0017](0017-contextual-term-references.md) | Contextual term references on the model path | Accepted; amends 0006 |
| [0018](0018-semantic-review.md) | Semantic review keeps model findings and dictionary constraints apart | Accepted (contract); engine pending |
| [0019](0019-document-context-and-human-precedents.md) | Document context and human precedents for model calls | Accepted (contract); supplements 0017 |

Maintainer map: [Architecture](../architecture.md).
