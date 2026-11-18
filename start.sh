#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$here/.env.example"

case "${1-}" in
    --setup) exec "$here/external-tools/install.sh" ;;
    --app)
        shift
        if [ "$#" -ne 0 ] && { [ "$#" -ne 2 ] || [ "$1" != --workspace ]; }; then
            echo '用法：./start.sh --app [--workspace 目录]' >&2
            exit 2
        fi
        "$here/start.sh" --api "$@" & api=$!
        "$here/start.sh" --ui & ui=$!
        # 任一服务退出或用户按 Ctrl-C 时，同时收回两个子进程。
        trap 'kill "$api" "$ui" 2>/dev/null || :; wait || :' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM HUP
        echo '对话页：http://127.0.0.1:8766/ （Ctrl-C 停止前后端）'
        while kill -0 "$api" 2>/dev/null && kill -0 "$ui" 2>/dev/null; do sleep 1; done
        echo '服务已退出，请检查上方错误或端口占用。' >&2
        exit 1
        ;;
    --ui)
        exec python3 -m http.server 8766 --bind 127.0.0.1 --directory "$here/frontend"
        ;;
    --api|--web)
        shift
        exec python3 "$here/web.py" --model "$OLLAMA_MODEL" "$@"
        ;;
esac

# 恢复时沿用检查点里的模型，不用默认值替换它。
for arg do
    if [ "$arg" = --resume ]; then exec python3 "$here/cli.py" "$@"; fi
done
exec python3 "$here/cli.py" --model "$OLLAMA_MODEL" --tools "$here/tools.json" "$@"
