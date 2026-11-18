# Mini Harness：从一次决策读懂整个循环

网页、终端聊天和单次任务通过 Ollama 生成原生工具调用。文件由外部 filesystem MCP 服务直接读写，勾选联网后，主助手本轮还可以调用外部搜索服务。父子助手共用一个执行循环和本次任务的预算。

## 首次安装与日常启动

在本目录准备好 Node.js 24 或更新版本、npm，以及支持工具调用的本地 Ollama 模型。默认模型名由随项目提供的 `.env.example` 指定。

```sh
./start.sh --setup   # 首次安装，等同于运行 external-tools/install.sh
./start.sh --app     # 日常启动网页和 API
```

打开[本地对话页](http://127.0.0.1:8766/)。也可运行 `./start.sh` 进入终端聊天，或直接运行单次任务：

```sh
./start.sh '写 index.html，加一个切换深浅色的按钮，再读回检查。' --expect index.html
```

单次任务可用 `--model 模型名` 覆盖默认值，中断后用 `./start.sh --resume` 恢复。产物保存在启动目录下的 `mini-harness-output-*`。验收页面时先看轨迹中 `write_file` 和 `read_text_file` 的结果，再打开页面点击按钮。`--expect index.html` 只检查收集到的同名根层文本文件含非空白内容。

## 从一次工具调用读懂循环

| 职责 | 代码与运行行为 |
| --- | --- |
| 上下文与执行循环 | `harness.run` 给模型当前目标、最近六组反馈和可用工具。收到工具请求就执行并回填结果，收到最终回答就检查验收条件。 |
| 工具发现与执行 | `tool_registry.connect_tools` 按项目的 `tools.json` 连接 stdio MCP 子进程，取 `tools/list` 声明的 Schema，按配置的 `allow` 暴露工具，再用 `tools/call` 执行。子进程在本轮产物目录中运行，以空环境映射启动，不继承宿主环境变量。 |
| 角色与权限 | 主助手可列目录、读写文件、调用固定词数统计命令、委派；本轮开启联网时可搜索。子助手只得到 `readOnly` 配置中的 `list_directory` 和 `read_text_file`。执行时再次检查权限。 |
| 委派与预算 | `delegate` 为子任务压入独立栈帧，结束时将回答交还父助手。父子共享步数、截止时间和最多六次委派额度。 |
| 状态与轨迹 | CLI 保存待执行动作和预算；会话层保存问答、文件快照和最近一次运行的轨迹。工具错误回填给模型，网页轨迹可核对参数、角色及执行结果。 |

新任务的 `write_file` 在会话产物目录内直接落盘。旧检查点和离线示例仍可使用 `workspace_tools.toolbox` 的虚拟文件及写入审批；恢复旧的待审批动作时沿用原流程。运行 `python3 -m unittest -v` 可用模拟模型检查角色、审批兼容、工具结果回填及预算；官方设计对照和验收点见[设计说明](docs/harness-design.md)。

## Ollama 模型适配层

`ollama_chat.chat` 向本机 `127.0.0.1:11434/api/chat` 请求推理；网页主助手启用流式，终端保留非流式。每次只声明当前允许的函数。默认配置下子助手可见 `list_directory`、`read_text_file`，主助手另有写入、固定命令和委派；只有启用联网的轮次可以使用通用网页搜索。内部 `done` 不是模型工具。系统提示区分问候、追问和新的写入请求：过去的任务不是这次的授权；用工具结果核实成功与否，不能凭空声称写过文件。[Ollama 原生工具调用协议](https://docs.ollama.com/capabilities/tool-calling)

已有会话的 `previous_turns` 按顺序展开为 user 和 assistant 消息，随后追加当前用户问题。运行历史中的 `exchange` 由核心循环提供，包含一次 assistant 调用及其全部 tool 结果。适配层按原序追加整组消息，保留 `thinking` 和 `tool_calls`。同批标为 `native: true` 且尚无 `exchange` 的中间记录会跳过。每条旧动作和结果用一条标明执行反馈的 user 消息传递。

返回值是响应中的原始 assistant 消息字典。普通文本和形似 JSON 的正文都保留在 `content` 中。缺少有效 assistant 消息、消息顶层混入旧动作字段 `op`，或 Ollama 拒绝工具请求时，适配层直接报错。可在本目录运行 `python3 -m unittest -v test_ollama_chat` 离线检查请求和响应契约。

## 多轮对话与工作目录

终端运行 `./start.sh --workspace ~/mini-harness-lab`，可以连续提问；`/exit` 退出，下次从同一目录继续，`/new` 开新会话。
使用网页时，推荐在本目录运行 `./start.sh --app [--workspace DIR]`。例如，以下命令在同一终端启动 JSON API（端口 8765）和静态前端（端口 8766）两个进程：

```sh
./start.sh --app --workspace ~/mini-harness-lab
```

打开 [本地对话页](http://127.0.0.1:8766/)，选择或输入工作区名。只用默认工作目录时，直接运行 `./start.sh --app`。按 Ctrl-C 可同时停止两个进程。如果其中一个进程退出，启动器会停止另一个并以非零状态退出。

需要单独启动或自定义 API 端口时，可在两个终端分别运行：

```sh
./start.sh --api --workspace ~/mini-harness-lab   # 终端一：JSON API，127.0.0.1:8765
./start.sh --ui                                 # 终端二：静态前端，127.0.0.1:8766
```

后端如果用了 `--port 8767`，用 `http://127.0.0.1:8766/?apiPort=8767` 打开前端。浏览器记住当前工作区的会话 ID，消息和文件由**后端**保存，刷新或服务重启后可恢复。新会话不继承旧会话。网页和终端默认使用各自的会话 ID。

网页侧栏可以切换当前工作区里保存过的会话。「轨迹」展示最近一次任务实际记录的动作，不是模型的思维过程；升级前的会话没有保存这项数据。输入框里的模型选择器读取本机 Ollama 的安装列表；更换模型只影响下一条消息，不会新建会话。一次任务中的子助手、写入确认和恢复沿用启动该任务时的模型。请选择支持原生工具调用的对话模型，安装新模型后可以刷新列表。

| Web API | 用途 |
| --- | --- |
| `GET /health` | API 进程健康探针，返回 `{"status":"ok"}`。模型和 MCP 的可用性需分别验证。 |
| `GET /models` | 从本机 Ollama 的 `/api/tags` 返回 `models`（名称列表）和 `default_model`。不下载模型。 |
| `POST /sessions` | 输入 `workspace`，按最近变动返回该工作区的 `sessions: [{id,title}]`。 |
| `POST /chat` | 可选 `model` 指定已安装模型；布尔值 `online` 允许主助手本轮调用外部 `search` 搜索公开网页，默认关闭。 |
| `POST /session`、`/chat`、`/approve` | 保留旧版 `trace` 摘要，同时返回最近一次运行的 `events`；模型传输失败也保留调用快照，终态为 `failed`。 |

**实时轨迹**：网页收到 NDJSON `activity` 帧就更新当前步骤，不等待最终 `result`。事件 `id` 标识同一步骤的开始、等待和终态，`call_id` 将工具与发起它的模型调用关联起来；主、子助手均有角色标记。模型开始请求时可展开实际请求体，返回后看到响应和实际用量。工具发起时可看参数，结束后才有执行结果。旧记录只显示已有摘要，不能重放成实时事件。轨迹不是模型的内部思考过程。

对话里按当前动作显示进度。Ollama 发出第一个思考信号时显示「正在思考怎么回答」，产生正文后逐段显示回复。搜索、读写文件和委派直接显示本次工具的关键词、文件名或子任务。模型名称、调用编号和参数可在轨迹中展开。

每条新回复下方显示本轮输入和输出 token 总数、生成速度、最近一次输入与已加载的上下文容量，以及各次模型调用的累计耗时。运行时另行显示本轮已经过去的秒数。速度按总输出 token 除以总生成秒数计算。Ollama 未提供的数值显示为「—」，旧会话沿用已有记录。

任务结束后，回复下方的「查看操作」保留动作、对象和结果状态。刷新时，`POST /session` 返回与 `turns` 顺序对应的 `turn_actions`、`turn_usage`。旧会话缺少的记录留空。生成的根目录 `index.html` 可以直接在「会话文件」中预览或下载。输入框的未发送草稿按工作区和会话保存在当前浏览器标签页。

快照用于可信本机调试，不是原始报文：不会采集 HTTP 头，隐藏内部推理并限制长内容。提示、历史消息和文件片段仍可能含有私人信息，不要将会话文件或轨迹截图直接公开。

**网页流式输出**：`/chat` 和 `/approve` 接受 `Accept: application/x-ndjson`。每行一个事件：`reset` 开始新一轮模型回复，`delta.text` 是 Ollama 已发出的正文片段，`result.data` 是权威终态或写入断点；流内错误为 `error.error`。子助手的内部回复不推到网页，只有完整工具调用才执行。没带该头的客户端继续获得普通 JSON。输入框保留输入法的选词与换行行为。选好字后可点击发送，或按 ⌘/Ctrl + Enter。

**联网搜索**。网页勾选「网页搜索」后，搜索服务收到本轮工具调用中的关键词，由外部 Node 运行 `external-tools/search.mjs`，通过 open-websearch 查询 Bing。文件服务也由外部 Node 启动。本项目只开放该服务的 `search` 工具。旧检查点的 `web_search(query)` 也调用同一服务，传入 `query`、`limit=5`，并把服务返回的文本回填给模型。[搜索服务入口](external-tools/search.mjs)

**分层**：`frontend/index.html` → JSON API（`web.py`）→ 会话层（`session_layer.py`）→ 原有 `harness.py` / 模型 / 工具；终端直接调用同一个会话层。后端不托管网页，页面不直接调用 Ollama。

**记忆边界**：会话保存完整问答和供网页展示的文件快照。每条新消息创建一次新运行，重置步数、时限和委派额度；主助手收到最近 6 轮问答（每条问与答分别最多 2000 字符），子助手不继承对话。没有跨会话偏好或语义检索。

**目录与网页文件区**。`--workspace` 选择根目录，默认是当前目录的 `.mini-harness/workspaces`。多轮会话的 filesystem 服务以 `根目录/工作区名/.mini-harness/会话ID/files/` 为允许目录。网页文件区只收集该目录根层、符合简单文件名规则、至多 1 MiB 的 UTF-8 普通文本文件，跳过符号链接。子目录中的文件仍在磁盘上。可下载收集到的文件，或点「预览 index.html」在隔离的 iframe 中检查页面交互。
