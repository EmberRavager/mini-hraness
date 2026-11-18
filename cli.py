import argparse, json, tempfile
from functools import partial
from pathlib import Path
from harness import run, start
from ollama_chat import chat
from workspace_tools import filename
from tool_registry import connect_tools

CHECKPOINT = Path(".mini-harness/run.json")

def save(state):
    with tempfile.NamedTemporaryFile(mode="w", dir=CHECKPOINT.parent, delete=False) as tmp:
        json.dump(state, tmp)
    Path(tmp.name).replace(CHECKPOINT)

def preview(name, text):
    print(f"写入 {name!r}，共 {len(text)} 字符。预览：{text[:1600]!r}")

def approve(name, text):
    preview(name, text)
    return input("允许写入？[y/N] ").strip().lower() == "y"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", nargs="?")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--tools", type=Path, help="外部 MCP 工具配置")
    parser.add_argument("--workspace", type=Path, help="终端聊天的工作区根目录")
    parser.add_argument("--expect")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    if args.task is None and not args.resume:
        if not args.model:
            parser.error("请用 --model 指定本地模型")
        from terminal_chat import interact
        root = args.workspace or Path(".mini-harness/workspaces")
        return interact(root, partial(chat, args.model), preview, tool_config=args.tools)
    if args.workspace is not None:
        parser.error("--workspace 适用于终端聊天或 --web")
    CHECKPOINT.parent.mkdir(exist_ok=True)
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
    if args.resume != (state.get("status") == "running") or bool(args.task) == args.resume:
        parser.error("有未结束任务时请用 --resume，否则请输入新任务")
    if not args.resume:
        state = start(args.task, steps=args.steps, seconds=args.seconds, expect=args.expect)
        if args.tools and args.steps > 0:
            state['tool_config'] = str(args.tools.resolve())
            state['directory'] = tempfile.mkdtemp(prefix='mini-harness-output-', dir=Path.cwd())
    state["_cli_model"] = args.model or state.get("_cli_model")
    if not state["_cli_model"]:
        parser.error("请用 --model 指定本地模型")
    save(state)
    model = partial(chat, state["_cli_model"])
    if state.get('tool_config'):
        directory = Path(state['directory'])
        with connect_tools(state['tool_config'], directory, state) as external:
            run(state, model, approve, save, external=external, directory=directory)
    else:
        run(state, model, approve, save)
    print(state["status"], state["result"])
    if state.get('directory') and state['files']:
        print("产物目录：", state['directory'])
    elif state["status"] in ("done", "review") and state["files"]:
        folder = Path(tempfile.mkdtemp(prefix="mini-harness-output-", dir=Path.cwd()))
        for name, text in state["files"].items():
            with (folder / filename(name)).open("x", encoding="utf-8") as out:
                out.write(text)
        print("产物目录：", folder)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        raise SystemExit(f"错误：{exc}")
