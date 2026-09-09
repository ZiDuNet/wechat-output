#!/usr/bin/env bash
# cnb_push.sh — 推当前技能仓到 CNB，自动处理 token 与死代理，不落盘。
# 用法: cnb_push.sh [分支] [额外 git 参数...]   默认分支 main
# 设计要点（防复犯 403）:
#   1. 绝不读 Windows 凭据管理器的旧 token（那是 403 元凶）——直接从 ~/.cnb/personal-token 取正确 token
#   2. 用 git -c url...insteadOf 在【内存】里重写 URL 注入 token，不写 .git/config、不落盘、不回显
#   3. 绕过已死的全局代理 127.0.0.1:9098
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BRANCH="main"
EXTRA=()
for a in "$@"; do
  if [[ "$a" == -* ]]; then EXTRA+=("$a"); else BRANCH="$a"; fi
done

TOKEN_FILE="$HOME/.cnb/personal-token"
if [ ! -f "$TOKEN_FILE" ]; then
  echo "ERROR: token 文件缺失: $TOKEN_FILE（应为 wechat-skills-publish 令牌）" >&2
  exit 1
fi
TOKEN="$(cat "$TOKEN_FILE")"

URL="$(git remote get-url origin)"
if [[ "$URL" != *cnb.cool* ]]; then
  echo "ERROR: origin 不是 cnb.cool 仓库: $URL" >&2
  exit 1
fi

echo ">> 推送 $REPO_ROOT -> origin/$BRANCH（token 取自 ~/.cnb/personal-token，内存注入不落盘）"
env -u http_proxy git -c http.proxy= \
  -c "url.https://cnb:${TOKEN}@cnb.cool/.insteadOf=https://cnb.cool/" \
  push origin "$BRANCH" "${EXTRA[@]}"
