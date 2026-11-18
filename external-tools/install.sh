#!/bin/sh
set -eu
cd "$(dirname "$0")"
node -e 'if (+process.versions.node.split(".")[0] < 24) { console.error("需要 Node.js 24 或更新版本"); process.exit(1) }'
npm ci --no-audit --no-fund --ignore-scripts
ln -sfn "$(node -p 'process.execPath')" node
