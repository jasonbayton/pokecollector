#!/usr/bin/env bash
# Which branches are not based on the newest upstream release, and does it matter.
#
# A stale base only matters for work that has not landed yet. Anything already
# merged into local-deployment is history, and its base is irrelevant.
set -uo pipefail

INTEGRATION="${INTEGRATION_BRANCH:-local-deployment}"

git fetch -q origin --tags 2>/dev/null || true

# Newest upstream tag first, so the first match is the best one.
TAGS=$(git tag -l 'v*' --sort=-v:refname)
NEWEST=$(printf '%s\n' "$TAGS" | head -1)

if [ -z "$NEWEST" ]; then
  echo "No upstream version tags found; is the origin remote configured?" >&2
  exit 1
fi

echo "Newest upstream release: $NEWEST"
echo

printf '%-34s %-9s %-8s %s\n' BRANCH BASE LANDED VERDICT
needs_rebase=0

for branch in $(git for-each-ref --format='%(refname:short)' refs/heads | sort); do
  base="none"
  for tag in $TAGS; do
    if git merge-base --is-ancestor "$tag" "$branch" 2>/dev/null; then
      base=$tag
      break
    fi
  done

  if git merge-base --is-ancestor "$branch" "$INTEGRATION" 2>/dev/null; then
    landed="yes"
  else
    landed="no"
  fi

  if [ "$base" = "$NEWEST" ]; then
    verdict="current"
  elif [ "$landed" = "yes" ]; then
    verdict="landed already, base is moot"
  else
    verdict="NOT ON $NEWEST and unlanded"
    needs_rebase=$((needs_rebase + 1))
  fi

  printf '%-34s %-9s %-8s %s\n' "$branch" "$base" "$landed" "$verdict"
done

echo
if [ "$needs_rebase" -eq 0 ]; then
  echo "Nothing needs rebasing."
else
  echo "$needs_rebase branch(es) need attention. For each one still wanted:"
  echo "  git rebase --onto $NEWEST <old-base-tag> <branch>"
  echo "Branches whose pull request is closed or superseded can simply be deleted."
fi

# Mirror check: main should be a pure upstream mirror.
if git show-ref -q --verify refs/heads/main; then
  behind=$(git rev-list --count main..origin/main 2>/dev/null || echo 0)
  ahead=$(git rev-list --count origin/main..main 2>/dev/null || echo 0)
  echo
  if [ "$ahead" != "0" ]; then
    echo "WARNING: main is $ahead commit(s) ahead of origin/main. It is meant to be a"
    echo "pure mirror; local work belongs on a feature branch."
  elif [ "$behind" != "0" ]; then
    echo "main is $behind commit(s) behind origin/main. Fast-forward it:"
    echo "  git checkout main && git merge --ff-only origin/main"
  else
    echo "main mirrors origin/main."
  fi
fi
