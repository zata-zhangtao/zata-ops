# Claude Project Adapter

本文件是 Claude 的项目入口适配层，不是完整规范正文。

- 跨工具入口摘要见 `AGENTS.md`
- 统一规范源在 `docs/ai-standards/`

@docs/ai-standards/index.md
@docs/ai-standards/architecture.md
@docs/ai-standards/naming.md
@docs/ai-standards/comments-docstrings.md
@docs/ai-standards/documentation.md
@docs/ai-standards/testing.md
@docs/ai-standards/tooling.md

## Claude Notes

- 本仓库是一个 CLI 工具 (`zata-ops`)，不存在四层后端架构；上游规范中关于 `src/backend/api|core|engines|infrastructure` 的硬约束在本仓库不强制
- 共享规范应放回 `docs/ai-standards/`，不要在本文件中复制成长篇正文
- 公共代码位于 `src/zata_ops/`，按命令域 (`db/`, `env/`, `logs/`, `observability/`) 划分
