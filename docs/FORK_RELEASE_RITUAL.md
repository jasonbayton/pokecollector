# Taking a new upstream release into this fork

This fork tracks `Git-Romer/pokecollector` and carries local features on top.
The rules below exist because of specific failures, noted where relevant.

## The branches and what each is for

| Branch | Purpose | Rule |
|---|---|---|
| `main` | Mirror of upstream. Nothing local, ever. | Only ever fast-forwarded from `origin/main` |
| `feat-*` / `fix-*` | One local feature or fix | Cut from an **upstream tag**, not from the fork |
| `local-deployment` | What actually runs | Upstream baseline plus every landed feature. This is what deploys |

### Why features branch from upstream

If a feature's files all exist upstream, branch it from the latest upstream tag.
It then rebases cleanly onto each new release, stays submittable as a pull
request, and merges into `local-deployment` as an ordinary merge.

Only branch from the fork when the work genuinely depends on fork-only code.
The test is mechanical:

```bash
# Every file the feature will touch: does upstream already have it?
for f in <files>; do
  git cat-file -e v1.37.0:"$f" 2>/dev/null && echo "in upstream: $f" || echo "FORK ONLY: $f"
done
```

If everything is "in upstream", branch from the tag and keep any new shared
constants in their own module rather than adding them to a fork-only file.

## The release ritual

### 1. Move the mirror

```bash
git fetch origin --tags
git checkout main && git merge --ff-only origin/main
```

### 2. Merge forward, do not rebuild

Merge the new tag into `local-deployment`. Do **not** rebuild the branch by
replaying features onto a fresh baseline: rebuilding means re-resolving every
conflict at every release, and it is what creates an apparent ordering
dependency between features. Merging forward settles each conflict once.

```bash
git checkout local-deployment
git merge v1.38.0
```

Expect conflicts wherever a local change touches the same code upstream
rewrote. Resolve by keeping both sides unless one genuinely supersedes the
other.

### 3. Look for what merged *cleanly* and should not have

A clean merge is not a correct merge. When both sides move the same logic, git
can merge without conflict and still produce something wrong. Twice now:

- A local admin-only delete gate merged cleanly alongside upstream's new
  ownership model, and would have short-circuited it.
- Upstream added `user_id=` to a notification call, in a branch the fork no
  longer ran because it had moved that code. The argument vanished silently.

The check that catches it: count upstream's references to the new concept per
file and compare with the merged tree.

```bash
PAT="custom_owner_id|is_shared_template"
for f in $(git ls-tree -r --name-only origin/main -- backend | grep '\.py$'); do
  up=$(git show origin/main:$f | grep -cE "$PAT"; true)
  [ "${up:-0}" = "0" ] && continue
  mine=$(grep -cE "$PAT" "$f" 2>/dev/null; true)
  [ "$up" != "${mine:-0}" ] && echo "DIFFERS up=$up merged=$mine $f"
done
```

### 4. Rebase unlanded feature branches

Only branches that have not been merged into `local-deployment` need this.
Anything already landed is history; its base no longer matters.

```bash
scripts/branch-audit.sh          # lists what is not on the current tag
git rebase --onto v1.38.0 <old-tag> feat-something
```

### 5. Test against a copy of production, not a fresh database

**This is the step that catches upgrade bugs, and the one whose absence broke
the live install.** A suite that only ever builds a fresh schema cannot detect a
migration failure by construction: `create_all` runs before the migration list
and skips tables that already exist, so an established database keeps its old
constraints while new tables that reference them are still created.

```bash
scripts/verify-upgrade.sh backups/<latest>.sql
```

That restores a real backup into a throwaway database and boots the new build
against it. Nothing else proves the upgrade works.

### 6. Deploy

Back up first, deploy, then verify the schema actually changed on the live
database rather than trusting that the service came up.

Native deployments must configure `SCAN_UPLOAD_DIR` outside the release
checkout. Before tagging the first release with this arrangement, install all
three exact files, stop the writer, perform the one-time move, then reload and
restart it:

```bash
install -D -m 0644 ops/pokecollector.service.d/scan-storage.conf \
  /etc/systemd/system/pokecollector.service.d/scan-storage.conf
install -m 0755 ops/pokecollector-prepare-scan-storage \
  /usr/local/bin/pokecollector-prepare-scan-storage
install -m 0755 ops/pokecollector-deploy /usr/local/bin/pokecollector-deploy
systemctl stop pokecollector
/usr/local/bin/pokecollector-prepare-scan-storage /opt/pokecollector
systemctl daemon-reload
systemctl start pokecollector
systemctl is-active --quiet pokecollector
```

The helper copies legacy uploads before removing their checkout directory, so
it is safe to rerun but cannot resurrect a photo that later expires, is
replaced, or is deleted. Stopping the service closes the write window, and the
drop-in makes both a candidate release and any health-gated rollback use the
same durable storage.

```bash
ssh <host> 'lxc exec pokecollector -- systemctl is-active pokecollector'
```

Check `systemctl is-active` returns `active`, not `activating`: a crash loop
reports `activating` for a while and looks like a slow start.

## Deleting branches

A branch merged into `local-deployment` can go. Keep only:

- branches backing an **open** upstream pull request,
- work genuinely in progress.

Everything else is noise that makes the audit harder to read.
