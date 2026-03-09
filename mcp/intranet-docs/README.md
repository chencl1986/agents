# intranet-docs MCP Server

## 用途

`intranet-docs` 是一个基于 Python FastMCP 的 STDIO MCP server，用于让 Codex 读取可访问的 API 文档站页面内容，并输出适合模型阅读的纯文本或 Markdown 文本。

适用场景：
- 读取文档站上的 API 文档分类页
- 读取带前端 hash 路由的具体接口文档 URL
- 将接口 path 或完整 Request URL 解析为对应的文档站链接
- 从内嵌 OpenAPI 数据中生成更适合模型消费的 Markdown 文档

## 目录说明

```text
mcp/intranet-docs/
├── README.md
├── requirements.txt
└── server.py
```

- `server.py`：STDIO MCP server 入口，暴露工具 `resolve_intranet_doc_url` 和 `fetch_intranet_doc`
- `requirements.txt`：运行该 server 所需 Python 依赖
- `README.md`：安装、启动、配置与使用说明

## 工具接口

- 工具名称：`resolve_intranet_doc_url`
- 建议签名：
  `resolve_intranet_doc_url(api_reference: str, method: str | None = None) -> dict`

输入参数说明：
- `api_reference`：接口 path、完整 Request URL，或带 `Request URL:` 前缀的原始文本
- `method`：可选 HTTP 方法。未提供时会优先读取索引中的唯一方法，或取首个方法

输出内容说明：
- 返回结构化结果，包含 `path`、`file`、`method`、`methods`、`docUrl`、`matchedBy`、`candidates`、`warnings`
- 解析顺序为：精确索引优先，规则猜测兜底
- 如果存在重复页，会按内置优先级自动选择，例如 `discovery.html` 优先于 `indexsearch.html`

- 工具名称：`fetch_intranet_doc`
- 建议签名：
  `fetch_intranet_doc(url: str, timeout_sec: int = 15, max_chars: int = 20000, output_format: str = "markdown", method: str | None = None) -> str`

输入参数说明：
- `url`：要读取的内容标识，支持以下三种输入：
  - 文档站 URL
  - 接口 path，例如 `/open/radiovipaccount/get`
  - 完整 Request URL，例如 `https://hkp-api.example.com/open/foo/bar?id=1`
- `timeout_sec`：HTTP 请求超时时间，默认 `15`
- `max_chars`：最大返回字符数，默认 `20000`
- `output_format`：输出格式，支持 `markdown` 和 `text`。其中 `markdown` 返回更适合模型阅读的 Markdown 风格文本，不保证为严格 Markdown
- `method`：可选 HTTP 方法。当 `url` 不是文档 URL，而是接口 path / Request URL 时用于消除歧义

输出内容说明：
- 返回带元信息头部的字符串
- 元信息包含 `requested_url`、`fetched_url`、`hash_fragment`、`status_code`、`content_type`、`truncated`、`hash_note`
- 当页面内存在 `window.apiDocs` 这类内嵌 OpenAPI JSON 时，会优先基于该数据生成 Markdown，而不是只提取静态 HTML 文本
- 当 URL 带 hash 路由时，会优先返回该 hash 对应的单个接口文档
- 当 URL 不带 hash 路由时，会返回该页面内所有接口文档的汇总 Markdown
- 当输入是接口 path / Request URL 时，会先解析为文档站链接，再发起 HTTP 抓取
- 当 URL 不在 allowlist 内或请求失败时，返回可读错误信息，不抛出 Python 堆栈

## URL 访问边界

默认只允许访问以下 URL 前缀：
- `http://127.0.0.1:8000/`
- `http://localhost:8000/`

可通过环境变量扩展 allowlist：

```bash
export INTRANET_ALLOWED_PREFIXES="http://127.0.0.1:8000/,http://localhost:8000/"
```

当需要把接口 path / Request URL 解析为文档链接时，还需要额外提供：

```bash
export INTRANET_DOCS_BASE_URL="http://docs.example.internal:9500/"
export INTRANET_DOCS_HTML_PAGES="radiovipaccount.html,activityreward.html"
```

含义说明：
- `INTRANET_DOCS_BASE_URL`：远端文档站访问根路径
- `INTRANET_DOCS_HTML_PAGES`：可选。若文档站根页无法列出全部 `*.html` 页面，可显式提供待索引页面文件名列表

实现约束：
- 会先对 URL 执行 allowlist 校验
- 当前 allowlist 采用基于 URL 前缀匹配的轻量校验
- 当输入不是文档站 URL 时，会先将其归一化为 API path，并尝试解析成文档 URL
- HTML 索引通过抓取远端 `*.html` 页面中的 `window.apiDocs.paths` 建立
- 默认会先抓取 `INTRANET_DOCS_BASE_URL` 根页，从页面链接中发现 `*.html`
- 如果根页不能完整列出页面，可通过 `INTRANET_DOCS_HTML_PAGES` 显式指定
- 若 exact path 命中多个文件，会优先按已知重复页策略选择
- 只有 exact path 未命中时，才会走文件名 heuristic，并且仍会验证目标文件确实包含该 path
- 对带 hash 的 URL，会拆分为“基础页面 URL + hash 路由片段”
- 实际 HTTP 请求只抓取基础页面
- 若页面中存在内嵌 OpenAPI spec，会优先解析该 spec
- 对 `#/paths/.../{method}` 这类 Stoplight Elements hash 路由，会直接解析为对应的 OpenAPI operation
- 若页面不带 hash，则会输出该页面 spec 中的全部接口文档
- 若无法从内嵌 spec 精确定位，才会回退到基础页面可读内容提取

## 环境安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r mcp/intranet-docs/requirements.txt
```

如果不想激活虚拟环境，也可以直接显式使用 `.venv` 下的解释器和 `pip`：

```bash
python3 -m venv .venv
.venv/bin/pip install -r mcp/intranet-docs/requirements.txt
```

## 启动方式

```bash
.venv/bin/python mcp/intranet-docs/server.py
```

这是一个 STDIO MCP server，启动后会等待 MCP 客户端通过标准输入输出进行通信。

## 使用 MCP Inspector 测试

### 启动 Inspector

推荐直接让 Inspector 代为拉起本 MCP server：

```bash
npx @modelcontextprotocol/inspector .venv/bin/python mcp/intranet-docs/server.py
```

如果已经激活虚拟环境，也可以写成：

```bash
npx @modelcontextprotocol/inspector python mcp/intranet-docs/server.py
```

默认情况下，Inspector 会启动：
- Web UI：`http://127.0.0.1:6274`
- Proxy：`http://127.0.0.1:6277`

### 关于 Proxy Session Token

MCP Inspector 自带本地 proxy，默认要求鉴权。启动时控制台通常会打印：

- `Session token`
- 带 `MCP_PROXY_AUTH_TOKEN=...` 的 Inspector 打开链接

有两种方式让 Inspector 正常连接：

1. 直接打开控制台打印出来的完整链接，让 token 自动带入页面
2. 如果页面已经打开，点击 `Configuration`，在 `Proxy Session Token` 中手动填入控制台打印的 token

如果不这样做，Inspector 通常会在连接时提示：

```text
Connection Error - Did you add the proxy session token in Configuration?
```

仅用于本机临时调试时，也可以关闭 Inspector proxy 的鉴权：

```bash
DANGEROUSLY_OMIT_AUTH=true npx @modelcontextprotocol/inspector .venv/bin/python mcp/intranet-docs/server.py
```

不建议在不可信网络环境中这样使用。

### 在 Inspector 中调用工具

连接成功后：

1. 打开 `Tools`
2. 选择 `resolve_intranet_doc_url` 或 `fetch_intranet_doc`
3. 填入参数并执行

接口地址解析示例：

```json
{
  "api_reference": "/open/radiovipaccount/get"
}
```

预期结果示例：

```json
{
  "path": "/open/radiovipaccount/get",
  "file": "radiovipaccount.html",
  "method": "get",
  "docUrl": "http://docs.example.internal:9500/radiovipaccount.html#/paths/open-radiovipaccount-get/get"
}
```

带 hash 的示例：

```json
{
  "url": "http://docs.example.internal:9500/userinfo.html#/paths/sapi-userinfo-avatarimage/get",
  "timeout_sec": 15,
  "max_chars": 20000,
  "output_format": "markdown"
}
```

不带 hash 的示例：

```json
{
  "url": "http://docs.example.internal:9500/userinfo.html",
  "timeout_sec": 15,
  "max_chars": 20000,
  "output_format": "markdown"
}
```

直接传 Request URL 的示例：

```json
{
  "url": "Request URL: https://hkp-api.shangshangji.cn/open/activityreward/lastmessages?code=lingong_2nd",
  "timeout_sec": 15,
  "max_chars": 20000,
  "output_format": "markdown"
}
```

预期行为：
- `resolve_intranet_doc_url`：只返回结构化链接解析结果
- 带 hash：返回对应单个接口文档
- 不带 hash：返回页面内全部接口文档汇总
- 传接口 path / Request URL：先解析到文档站链接，再返回对应接口文档
- 返回头部会包含 `hash_fragment`、`status_code`、`content_type`、`hash_note` 等元信息

## Codex 配置示例

以下示例仅演示配置方式，不包含任何敏感信息：

```toml
[mcp_servers.intranetDocs]
command = ".venv/bin/python"
args = ["mcp/intranet-docs/server.py"]
cwd = "/path/to/agents"
env = {
  INTRANET_ALLOWED_PREFIXES = "http://docs.example.internal:9500/",
  INTRANET_DOCS_BASE_URL = "http://docs.example.internal:9500/",
  INTRANET_DOCS_HTML_PAGES = "radiovipaccount.html,activityreward.html"
}
```

## 输出示例

带 hash 的接口页会返回单个接口文档，例如：

```md
# 获取用户头像图片(302重定向)

get
/sapi/userinfo/avatarimage

返回302重定向到头像图片URL，如果用户没有头像则重定向到默认头像

## Request

### Query Parameters

- `user_id` `integer<int64>` required
  用户ID

## Responses

### 302
重定向到头像图片URL

#### Headers

- `Location` `string`
  头像图片URL
```

不带 hash 的分类页会返回该页面内所有接口文档的汇总 Markdown。

## 最小验证清单

1. 确保文档站可从 MCP 所在环境访问，例如 `http://docs.example.internal:9500/`
2. 启动 MCP server：`.venv/bin/python mcp/intranet-docs/server.py`
3. 通过 MCP 客户端调用 `resolve_intranet_doc_url` 或 `fetch_intranet_doc`
4. 分别验证：
   - 接口 path / Request URL 可被解析为正确的文档站链接
   - 分类页 URL 可返回该页面全部接口文档
   - 带 hash 路由的 URL 会返回对应单个接口文档，并保留 `hash_fragment`
   - 页面内存在 `window.apiDocs` 时，输出优先基于内嵌 OpenAPI spec 生成
   - URL 不在 allowlist 内时返回清晰错误信息
   - 超长内容会被截断并标记 `truncated: true`
