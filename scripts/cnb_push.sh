#!/usr/bin/env bash
# cnb_push.sh — 推当前技能仓到 CNB，自动处理 token 与死代理，不落盘。
# 用法: cnb_push.sh [分支] [额外 git 参数...]   默认分支 main
# 设计要点（防复犯 403）:
#   1. 绝不读 Windows 凭据管理器的旧 token（那是 403 元凶）——直接从 ~/.cnb/personal-token 取正确 token
#   2. token 经 GIT_CONFIG_KEY_*/VALUE_* 环境变量注入 insteadOf 重写：既不写 .git/config、
#      不落盘，也不会出现在 git 进程命令行被同机其他进程读到（审计 E7）
#   3. 绕过已死的全局代理 127.0.0.1:9098（env -u http_proxy + 把 http.proxy 置空）
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

echo ">> 推送 $REPO_ROOT -> origin/$BRANCH（token 经环境变量注入，不落命令行、不落盘）"
env -u http_proxy \
  GIT_CONFIG_COUNT=2 \
  GIT_CONFIG_KEY_0="http.proxy" GIT_CONFIG_VALUE_0="" \
  GIT_CONFIG_KEY_1="url.https://cnb:${TOKEN}@cnb.cool/.insteadOf" GIT_CONFIG_VALUE_1="https://cnb.cool/" \
  git push origin "$BRANCH" "${EXTRA[@]}"
