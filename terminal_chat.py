"""终端只处理输入、展示和会话选择，聊天和审批统一交给会话层。"""
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from session_layer import dispatch


def current_session(marker):
    if marker.is_symlink():
        raise ValueError("终端会话记录不能是符号链接")
    try:
        return str(UUID(marker.read_text(encoding="utf-8").strip()))
    except FileNotFoundError:
        # 首次进入时，同一工作区的多个终端会选中同一个会话。
        return str(uuid5(NAMESPACE_URL, marker.parent.resolve().as_uri()))
    except ValueError as exc:
        raise ValueError(f"终端会话记录无效，请检查 {marker}") from exc


def remember_session(marker, session):
    # 先完整写入临时文件，再切换会话，避免中断留下半个 UUID。
    with TemporaryDirectory(dir=marker.parent) as directory:
        path = Path(directory) / "session"
        path.write_text(session, encoding="utf-8")
        path.replace(marker)


def interact(root, model, preview):
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".terminal-session"
    session = current_session(marker)

    def post(route, session_id, **fields):
        body = dict(workspace="default", session=session_id, **fields)
        return dispatch(root, route, body, model)

    state = post("/session", session)
    if state.get("turns"):
        print(f"已继续上次对话，共 {len(state['turns'])} 轮。")
    print("输入消息开始聊天，/new 新开对话，/exit 退出。")
    while True:
        approval = state.get("approval")
        if approval:
            preview(approval["name"], approval["text"])
        try:
            message = input("允许写入？[y/N] " if approval else "你：").strip()
        except EOFError:
            print()
            return
        if message == "/exit":
            return
        if message == "/new":
            new_session = str(uuid4())
            state = post("/session", new_session)
            remember_session(marker, new_session)
            session = new_session
            print("已开始新对话。")
            continue
        if not message and not approval:
            continue
        try:
            if approval:
                state = post("/approve", session, allow=message.lower() in ("y", "yes"))
            else:
                state = post("/chat", session, message=message)
        except (ValueError, OSError) as exc:
            print(f"请求失败：{exc}", file=sys.stderr)
            if isinstance(exc, ConnectionError):
                print("请检查本地 Ollama 是否已启动。", file=sys.stderr)
            # 请求可能已写入部分进度，重新读取当前会话再接受输入。
            state = post("/session", session)
            continue
        if state.get("reply"):
            print("助手：", state["reply"])
