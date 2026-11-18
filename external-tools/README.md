# 外部工具

在 mini-harness 目录运行以下命令。首次安装需要 Node.js 24 或更新版本、npm 和 npm 公共仓库网络访问。macOS 和其他 POSIX 系统可使用此脚本。

```sh
./start.sh --setup
python3 external-tools/smoke.py
python3 external-tools/smoke.py --online
```

安装器以 `npm ci` 复用 package-lock.json 的版本和完整性哈希，创建本地 node 软链。移动或升级 Node 后重新安装。两项工具在运行时直接启动本地 Node。第二个验收命令只在临时目录读写，第三个还会向 Bing 发送一次公开查询。

配置入口为 [tools.json](../tools.json)。含 `./` 前缀的程序与脚本路径以该文件所在目录解析。客户端传入空环境、以当前会话专属文件目录作为 cwd，并把该目录的绝对路径替换到 `{workspace}`。客户端使用 stdio 握手、`tools/list` 发现参数 Schema，仅注册 allow 中的名称。

| 服务 | 注册工具 | 调用参数及结果 |
| --- | --- | --- |
| files | list_directory | `{"path":"."}` 列出文件 |
| files | read_text_file | `{"path":"index.html"}` 读取文本 |
| files | write_file | `{"path":"index.html","content":"…"}` 创建或覆盖，写入审批由宿主管理 |
| search | search | `{"query":"Python official website","limit":3}` 返回 MCP 文本块，内容为 JSON，含 results、totalResults、partialFailures。每条结果含 title、url、description 和 engine |

文件服务固定为官方 `@modelcontextprotocol/server-filesystem@2.4.0`，两个读工具列入 readOnly。[官方说明](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)

搜索服务固定为 `open-websearch@2.2.0`。启动脚本选择 stdio、HTTP 请求搜索和 Bing 引擎，只有开启联网的回合注册 search。公开站点可能限流或返回空结果，调用方应检查 results 和 partialFailures。实测包的握手版本字段为 1.2.0，安装版本以锁文件为准。[上游仓库](https://github.com/Aas-ee/open-webSearch)
