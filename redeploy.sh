#!/usr/bin/env bash
set -euo pipefail

BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [[ "$BRANCH" != "main" ]]; then
  echo "Remote deploys only run from main. Current branch: $BRANCH"
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is not clean. Commit or stash changes before remote deploy."
  exit 1
fi

echo "Pushing main to origin..."
git push origin main
echo "Push complete. Woodpecker will build and deploy f1-stream remotely."
