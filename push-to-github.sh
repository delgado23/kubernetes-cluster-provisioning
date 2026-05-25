#!/usr/bin/env bash
# Push all local branches to GitHub with vars/vault.yml stripped from history.
#
# Run from anywhere inside the repo:
#   ./push-to-github.sh
#
# The local repo and Gitea (origin) are never touched.
set -euo pipefail

GITHUB_REMOTE="github"
REPO_ROOT="$(git rev-parse --show-toplevel)"
GITHUB_URL="$(git -C "$REPO_ROOT" remote get-url "$GITHUB_REMOTE")"
WORK_DIR="$(mktemp -d)"

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

echo "==> Cloning local repo to $WORK_DIR ..."
git clone --no-local "$REPO_ROOT" "$WORK_DIR/repo"
cd "$WORK_DIR/repo"

echo "==> Checking out all branches ..."
for branch in $(git branch -r | grep -v 'HEAD' | sed 's|[[:space:]]*origin/||'); do
  git checkout -b "$branch" "origin/$branch" 2>/dev/null || true
done
git checkout master

echo "==> Stripping vars/vault.yml from all history ..."
PYTHON="${PYTHON:-/c/Users/danie/AppData/Local/Programs/Python/Python312/python.exe}"
FILTER_REPO="${FILTER_REPO:-/tmp/git-filter-repo}"
if [ ! -f "$FILTER_REPO" ]; then
  curl -fsSL https://raw.githubusercontent.com/newren/git-filter-repo/main/git-filter-repo > "$FILTER_REPO"
  chmod +x "$FILTER_REPO"
fi
"$PYTHON" "$FILTER_REPO" --path vars/vault.yml --invert-paths --force

echo "==> Verifying vault.yml is absent from master and production ..."
for branch in master production; do
  if git show "refs/heads/$branch:vars/vault.yml" >/dev/null 2>&1; then
    echo "ERROR: vars/vault.yml still present in $branch — aborting."
    exit 1
  fi
done
echo "    OK — vault.yml not found in any branch tree."

echo "==> Cleaning up loose objects ..."
git reflog expire --expire=now --all
git gc --prune=now --quiet

echo "==> Pushing to GitHub ..."
git remote add github "$GITHUB_URL"
git push github --force --all
git push github --force --tags

echo "==> Done."
