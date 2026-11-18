# mini-harness 的设计与验收

以“写一份说明并读回”为例，模型 API 给出要调用的函数和参数，harness 检查当前角色能否调用写工具，再把执行结果交回模型。项目中，`ollama_chat.chat` 负责模型消息格式，`harness.run` 驱动循环，`tool_registry.connect_tools` 提供本轮允许的外部工具，`session_layer.dispatch` 保存并恢复会话。以下对照于 2026-11-19 核对。

## 模型 API：如何把工具结果交还模型

| 官方接口及可复用设计 | 本项目验收点 |
| --- | --- |
| **Ollama** 的 `/api/chat` 接收工具定义。下一次请求保留助手原始的 `tool_calls`，随后添加含 `tool_name` 的工具结果消息。[Ollama 工具调用](https://docs.ollama.com/capabilities/tool-calling) | 离线兼容用例一次请求列目录、写入、读回。在旧虚拟写工具的审批处暂停，恢复后检查模型历史中原调用完整保留，三个结果按顺序各出现一次。形似工具 JSON 的普通回复只作为文本展示。[原生循环测试](../test_native_loop.py) |
| **DeepSeek Chat Completions** 的自定义函数由调用方执行，结果携带原请求的 `tool_call_id`。同一函数可在一轮出现多次，适配器应保留每次调用的身份。[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/) | **若接入 DeepSeek**，用同名函数、不同参数和两个 ID 构造回放，逐项核对结果绑定的 ID。当前 Ollama 测试覆盖的是它自己的消息格式。 |

## Harness：谁控制循环和副作用

| 官方实现及可复用设计 | 本项目验收点 |
| --- | --- |
| **OpenAI Agents SDK** 的 Runner 执行工具、追加结果、再次调用模型；得到最终输出时结束，`max_turns` 限制模型轮次。[Runner 循环](https://openai.github.io/openai-agents-python/running_agents/#the-agent-loop) | 在 `run` 集中裁决何时停止。这里的步数还约束工具批次：剩余预算不足时，整批停止，第一项写入也不能发生。[批次预算测试](../test_native_loop.py) |
| **Codex** 分别控制命令能访问哪些资源，以及何时请求批准。可复用之处是让执行器始终检查操作范围。[Codex 沙箱与审批](https://developers.openai.com/codex/agent-approvals-security/) | 默认文件服务只接收会话产物目录。让子助手请求 `write_file`，模型历史中应收到权限错误，磁盘上不应出现该文件。宿主提供的固定词数统计命令另有白名单检查。[工具注册测试](../test_tool_registry.py) · [命令测试](../test_harness.py) |
| **Claude Agent SDK** 先按权限模式和规则处理工具请求，需要运行时决策时交给 `canUseTool`。**Gemini CLI** 将匹配规则的结果表示为允许、拒绝或询问。两者都把决定放在执行工具之前。[Claude 权限](https://code.claude.com/docs/en/agent-sdk/permissions) · [Gemini CLI 策略引擎](https://geminicli.com/docs/reference/policy-engine/) | 新入口授权主助手直接在产物目录执行 MCP 写入。逐次审批保留给旧检查点和离线虚拟工具示例：拒绝后文件仍不存在，批准后沿用原工具事件 ID 完成写入。[终端测试](../test_terminal_cli.py) · [流式审批测试](../test_stream_api.py) · [网页入口](../frontend/index.html) |

## MCP：外部工具如何接入

默认入口通过 stdio 完成 MCP 握手，从 `tools/list` 取得工具名与输入 Schema，再通过 `tools/call` 执行配置允许的调用。MCP 子进程以产物目录为工作目录，启动时传入空环境映射。主助手用 `write_file` 写入真实文件，子助手只能列目录、读文本；搜索服务仅在开启联网的轮次连接。两个服务均由外部 Node 启动。搜索入口 `external-tools/search.mjs` 以 stdio 加载 open-websearch，配置的引擎为 Bing。旧检查点调用 `web_search(query)` 时只连接 `search` 服务，传入 `query` 和 `limit=5`，原样回填工具返回的文本。[MCP 工具规范（2025-11-25）](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [注册表](../tool_registry.py) · [传输测试](../test_external_tools.py)

**文件验收**。已用 fake model 驱动真实 filesystem MCP，在临时会话写入并读回 `index.html`，核对了磁盘内容和会话返回值。网页文件区只收集根层、符合命名规则且至多 1 MiB 的 UTF-8 普通文本文件。检查轨迹时按 `call_id` 对照模型请求、写入参数和工具结果；页面外观与按钮行为仍需打开预览单独验收。[轨迹测试](../test_trace_events.py) · [网页入口](../frontend/index.html)
