#!/usr/bin/env bash
set -euo pipefail

# One-command GitHub deployment.
# Prerequisite: `gh auth login` with repo + workflow scopes.
#
# Usage:
#   ./scripts/deploy_github.sh
#   REPO_NAME=weekly-ai-keyword-radar ./scripts/deploy_github.sh

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_NAME="${REPO_NAME:-weekly-ai-keyword-radar}"
DESCRIPTION="${DESCRIPTION:-Weekly AI & embodied-AI keyword radar: RSS + LLM + fixed JSON schema}"
VISIBILITY="${VISIBILITY:-public}"

cd "$REPO_DIR"

if ! command -v gh >/dev/null 2>&1; then
  echo "未找到 gh。先安装：brew install gh" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "GitHub CLI 未登录。先运行：" >&2
  echo "  gh auth login --hostname github.com --git-protocol https --web --scopes repo,workflow" >&2
  exit 1
fi

OWNER="$(gh api user -q .login)"
FULL_NAME="$OWNER/$REPO_NAME"

if [ ! -d .git ]; then
  git init -b main
fi

if ! git config user.name >/dev/null 2>&1; then
  git config user.name "weekly-ai-keyword-radar"
fi
if ! git config user.email >/dev/null 2>&1; then
  git config user.email "weekly-ai-keyword-radar@users.noreply.github.com"
fi

if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
  git add -A
  git commit -m "feat: weekly AI keyword radar"
fi

if gh repo view "$FULL_NAME" >/dev/null 2>&1; then
  echo "仓库已存在：$FULL_NAME"
  if ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "https://github.com/$FULL_NAME.git"
  fi
  git push -u origin main
else
  gh repo create "$FULL_NAME" \
    --"$VISIBILITY" \
    --description "$DESCRIPTION" \
    --source=. \
    --remote=origin \
    --push
fi

gh repo edit "$FULL_NAME" \
  --description "$DESCRIPTION" \
  --add-topic ai \
  --add-topic rss \
  --add-topic llm \
  --add-topic keyword-extraction \
  --add-topic embodied-ai \
  --add-topic weekly-report \
  --add-topic opencode-skill 2>/dev/null || true

echo
echo "已部署：https://github.com/$FULL_NAME"
echo
echo "下一步（可选）：配置每周自动运行"
echo "  gh secret set LLM_API_KEY --repo $FULL_NAME --body \"sk-...\""
echo "  gh variable set LLM_BASE_URL --repo $FULL_NAME --body \"https://api.deepseek.com/v1\""
echo "  gh variable set LLM_MODEL --repo $FULL_NAME --body \"deepseek-chat\""
echo
echo "不配置 LLM_API_KEY 也可以手动跑，但工作流会使用 --no-llm 的抽取式兜底。"
