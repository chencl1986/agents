# intranet-docs MCP Server

## 用途

`intranet-docs` 是一个基于 Python FastMCP 的 STDIO MCP server，用于让 Codex 读取本机启动的 API 文档页面内容，并输出适合模型阅读的纯文本或 Markdown 文本。

适用场景：
- 读取本地运行的 API 文档分类页
- 读取带前端 hash 路由的具体接口文档 URL
- 将 HTML 页面转换为更适合大模型消费的可读文本

## 目录说明

```text
mcp/intranet-docs/
├── README.md
├── requirements.txt
└── server.py
```

- `server.py`：STDIO MCP server 入口，暴露工具 `fetch_intranet_doc`
- `requirements.txt`：运行该 server 所需 Python 依赖
- `README.md`：安装、启动、配置与使用说明

## 工具接口

- 工具名称：`fetch_intranet_doc`
- 建议签名：
  `fetch_intranet_doc(url: str, timeout_sec: int = 15, max_chars: int = 20000, output_format: str = "markdown") -> str`

输入参数说明：
- `url`：要读取的本地文档 URL，支持分类页 URL 和带 `#...` 的 hash 路由 URL
- `timeout_sec`：HTTP 请求超时时间，默认 `15`
- `max_chars`：最大返回字符数，默认 `20000`
- `output_format`：输出格式，支持 `markdown` 和 `text`

输出内容说明：
- 返回带元信息头部的字符串
- 元信息包含 `requested_url`、`fetched_url`、`hash_fragment`、`status_code`、`content_type`、`truncated`
- 当 URL 不在 allowlist 内或请求失败时，返回可读错误信息，不抛出 Python 堆栈

## URL 访问边界

默认只允许访问以下本地前缀：
- `http://127.0.0.1:8000/`
- `http://localhost:8000/`

可通过环境变量扩展 allowlist：

```bash
export INTRANET_ALLOWED_PREFIXES="http://127.0.0.1:8000/,http://localhost:8000/"
```

实现约束：
- 会先对 URL 执行 allowlist 校验
- 对带 hash 的 URL，会拆分为“基础页面 URL + hash 路由片段”
- 实际 HTTP 请求只抓取基础页面
- 输出中会保留 `hash_fragment`，并尽量提取与该片段相关的内容
- 若无法精确定位，会明确说明当前返回的是基础页面可读内容

## 环境安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r mcp/intranet-docs/requirements.txt
```

## 启动方式

```bash
python mcp/intranet-docs/server.py
```

这是一个 STDIO MCP server，启动后会等待 MCP 客户端通过标准输入输出进行通信。

## Codex 配置示例

以下示例仅演示本地配置方式，不包含任何敏感信息：

```toml
[mcp_servers.intranetDocs]
command = "python"
args = ["mcp/intranet-docs/server.py"]
cwd = "/path/to/agents"
env = { INTRANET_ALLOWED_PREFIXES = "http://127.0.0.1:8000/,http://localhost:8000/" }
```

## 本地示例 URL

- 文档根目录：`http://127.0.0.1:8000/`
- 分类页：`http://127.0.0.1:8000/userinfo.html`
- 具体接口页：`http://127.0.0.1:8000/userinfo.html#/paths/sapi-userinfo-avatarimage/get`

## 最小验证清单

1. 在本机启动文档站点，例如 `http://127.0.0.1:8000/`
2. 启动 MCP server：`python mcp/intranet-docs/server.py`
3. 通过 MCP 客户端调用 `fetch_intranet_doc`
4. 分别验证：
   - 分类页 URL 可返回可读正文
   - 带 hash 路由的 URL 会保留 `hash_fragment`
   - URL 不在 allowlist 内时返回清晰错误信息
   - 超长内容会被截断并标记 `truncated: true`
