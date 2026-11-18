#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$here/.env.example"

if [ "$#" -eq 0 ]; then
    printf '请输入任务：'
    IFS= read -r task
    set -- "$task"
fi

# 恢复时沿用检查点里的模型，不用默认值替换它。
for arg do
    if [ "$arg" = --resume ]; then exec python3 "$here/cli.py" "$@"; fi
done
exec python3 "$here/cli.py" --model "$OLLAMA_MODEL" "$@"
