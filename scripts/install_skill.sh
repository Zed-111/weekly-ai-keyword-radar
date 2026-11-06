#!/usr/bin/env bash
set -euo pipefail

# Install this repository as a local skill via symlink.
# Usage:
#   ./scripts/install_skill.sh                 # OpenCode global
#   ./scripts/install_skill.sh ~/.claude/skills
#   ./scripts/install_skill.sh /path/to/skills

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_ROOT="${1:-$HOME/.config/opencode/skills}"
TARGET="$TARGET_ROOT/weekly-ai-keyword-radar"

mkdir -p "$TARGET_ROOT"
if [ -e "$TARGET" ] && [ ! -L "$TARGET" ]; then
  echo "目标已存在且不是符号链接：$TARGET" >&2
  echo "先移动或删除它，再重试。" >&2
  exit 1
fi
ln -sfn "$REPO_DIR" "$TARGET"
echo "已安装：$TARGET -> $REPO_DIR"
echo "技能 ID：weekly-ai-keyword-radar"
