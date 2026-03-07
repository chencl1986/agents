# Agents Repository Skeleton

## 仓库用途概述

本仓库用于统一管理以下内容：
- Agents 提示词与规范文档
- MCP 工具的模板与约定
- 本地配置模板（仅示例，不含真实敏感信息）

本阶段仅初始化目录骨架与最小文档占位，不包含可运行实现。

## 目录结构说明

```text
.
├── AGENTS.md
├── README.md
├── docs/
│   └── SECURITY.md
├── agents/
│   ├── company-dev/
│   │   └── .gitkeep
│   └── templates/
│       └── agent.md
├── mcp/
│   └── templates/
│       └── README.md
├── configs/
│   └── codex/
│       └── config.toml.example
└── scripts/
    └── .gitkeep
```

说明：
- `agents/templates/`：Agent 规范模板
- `agents/company-dev/`：后续放置具体 Agent 定义，当前用占位文件保留目录
- `mcp/templates/`：MCP 工具模板说明
- `configs/`：配置示例（真实配置不入库）
- `docs/`：安全与治理文档
- `scripts/`：后续自动化脚本占位，当前未放入具体实现

## 快速开始（TODO）

1. 从 `agents/templates/agent.md` 复制并创建第一个 Agent 文档。
2. 从 `mcp/templates/README.md` 复制并创建第一个 MCP 工具说明。
3. 参考 `configs/codex/config.toml.example` 在 `~/.codex/config.toml` 或项目级 `.codex/config.toml` 创建本地配置（不要提交真实配置）。
4. 阅读 `docs/SECURITY.md` 并按安全边界执行。
