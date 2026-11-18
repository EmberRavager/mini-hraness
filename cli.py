import argparse, http.client, json, tempfile
from contextlib import closing
from functools import partial
from pathlib import Path
from harness import run, start
from workspace_tools import filename

CHECKPOINT = Path(".mini-harness/run.json")
PROMPT = ('Return one allowed JSON action, e.g. {"op":"write","name":"index.html","text":"..."}. '
          'Fields by op: list(), read(name), write(name,text), command(name="word_count",file=filename), '
          'delegate(task), done(text). Use flat filenames. Only use actions listed for this turn.')

def chat(model, task, history, actions, timeout):
    context = json.dumps(dict(task=task, history=history, actions=actions), ensure_ascii=False)
    messages = [dict(role="system", content=PROMPT), dict(role="user", content=context)]
    body = json.dumps(dict(model=model, messages=messages, format="json", stream=False))
    with closing(http.client.HTTPConnection("127.0.0.1", 11434, timeout=timeout)) as conn:
        conn.request("POST", "/api/chat", body, {"Content-Type": "application/json"})
        reply = conn.getresponse()
        if reply.status != 200:
            raise OSError(f"Ollama HTTP {reply.status}: {reply.read(512)!r}")
        return json.loads(json.load(reply)["message"]["content"])

def save(state):
    with tempfile.NamedTemporaryFile(mode="w", dir=CHECKPOINT.parent, delete=False) as tmp:
        json.dump(state, tmp)
    Path(tmp.name).replace(CHECKPOINT)

def approve(name, text):
    print(f"写入 {name}，共 {len(text)} 字符。预览：{text[:1600]!r}")
    return input("允许写入？[y/N] ").strip().lower() == "y"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", nargs="?")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--expect")
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--seconds", type=int, default=120)
    args = parser.parse_args()
    CHECKPOINT.parent.mkdir(exist_ok=True)
    state = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {}
    if args.resume != (state.get("status") == "running") or bool(args.task) == args.resume:
        parser.error("有未结束任务时请用 --resume，否则请输入新任务")
    if not args.resume:
        state = start(args.task, steps=args.steps, seconds=args.seconds, expect=args.expect)
    state["_cli_model"] = args.model or state.get("_cli_model")
    if not state["_cli_model"]:
        parser.error("请用 --model 指定本地模型")
    save(state)
    run(state, partial(chat, state["_cli_model"]), approve, save)
    print(state["status"], state["result"])
    if state["status"] in ("done", "review") and state["files"]:
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
