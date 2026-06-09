# Architecture Standards

本页是 `docs/ai-standards/` 中的通用架构速查版，沿用自模板仓库。

`zata-ops` 自身是一个 CLI 工具，没有四层后端架构，因此本页的 `src/<module>/api|core|engines|infrastructure` 约束在本仓库不强制；保留这里是为了让从 `zata_code_template` 派生而来的 AI 代理理解上游约定。详细架构文档见 `docs/architecture/system-design.md`（上游模板仓库，本仓库不包含）。

## Backend Layers

| Layer | Path | Responsibility |
|---|---|---|
| 接入层 | `src/backend/api/` | HTTP/CLI/WebSocket 入口、参数校验、DTO 转换 |
| 核心编排层 | `src/backend/core/` | 用例、编排、领域契约、纯业务规则 |
| 平台能力层 | `src/backend/engines/` | Skills、RAG、可插拔能力，实现 core 定义的接口 |
| 基础设施层 | `src/backend/infrastructure/` | LLM 客户端、数据库、HTTP、日志、配置等具体实现 |

## Agent-First Capabilities

新增面向业务的具体能力时，应优先作为 Agent 可编排能力接入，通常落在 `src/backend/engines/`，并通过 skill、tool 或 capability adapter 供 `src/backend/core/` 编排层调用。

这里的具体业务能力包括但不限于：单证识别、OCR、信息抽取、审核、检索、爬虫等。

除非详细架构文档明确批准新的服务边界，默认禁止仅为单一业务能力新增独立 HTTP 服务、独立端口或旁路的用户级 API。外部请求应统一经由 `src/backend/api/` 入口进入，再由 `src/backend/core/` 的用例或 Agent 编排层通过抽象契约与注册机制调用具体能力实现。

## Dependency Direction

```text
src/<module>/api/ -> src/<module>/core/ -> src/<module>/engines/ -> src/<module>/infrastructure/ -> third-party packages
```

禁止违反以下规则：

- `src/<module>/infrastructure/` 不得导入 `src/<module>/core/`、`src/<module>/engines/`、`src/<module>/api/`
- `src/<module>/core/` 不得导入 `src/<module>/engines/`、`src/<module>/infrastructure/`、`src/<module>/api/`
- `src/<module>/api/` 不得直接导入 `src/<module>/infrastructure/` 或 `src/<module>/engines/`
- 跨层依赖必须通过 `src/<module>/core/shared/interfaces/` 中的抽象接口

新增模块若采用相同的四层结构，同样受 `hooks/check_architecture.py` 约束。

## Placement Checklist

新增代码前先判断：

1. 这是入口适配、业务编排、平台能力，还是基础设施实现
2. import 方向是否仍然向内
3. 是否在已有模块职责内扩展，而不是偷塞到最近的文件
4. 是否需要先定义接口，再做跨层实现

## Composition Root

- `src/backend/main.py` 是真实后端 composition root
- 根目录 `main.py` 只是兼容包装器

## Frontend Boundary

`frontend/` 不属于后端四层的一部分。它是系统边界外的 Web 客户端，通过 HTTP 或 WebSocket 调用 `src/backend/api/`。
