# Releasing this fork

Deployment is pull-based and automatic. You tag; the container notices, builds,
health-checks and either keeps the release or puts the old one back.

Nothing is ever pushed to the container from a workstation. The `dist/` is
built on the box from source at the tagged commit.

## Making a release

```bash
# 1. Annotated tag. Not lightweight - see "Why annotated" below.
git tag -a bayton-v1.37.0-4 -m "Scanner re-take and viewfinder rework"

# 2. Push the branch and the tag.
git push origin local-deployment bayton-v1.37.0-4
```

**Do not bump `VERSION` for a fork release.** That file is upstream's and
merges forward with every upstream release, so writing fork numbering into it
would create a conflict on each merge and misreport the app's upstream lineage.
The fork release is identified by its tag, and the deployed tag is recorded on
the box in `/var/lib/pokecollector-deploy/deployed`:

```bash
ssh jason@192.168.1.200 "lxc exec pokecollector -- cat /var/lib/pokecollector-deploy/deployed"
```

If the running fork release should be visible in the app itself, that wants a
separate field rather than a hijacked `VERSION`.

Within five minutes the container picks it up. To watch:

```bash
ssh jason@192.168.1.200 "lxc exec pokecollector -- journalctl -u pokecollector-update -n 40"
```

## The tag format

`bayton-v<upstream-baseline>-<downstream-release>`, for example
`bayton-v1.37.0-4`: the fourth fork release built on upstream's v1.37.0.

Two reasons for the `bayton-` prefix rather than bare `v1.37.0-4`:

- **It cannot be confused with an upstream tag.** The deploy script selects on
  `bayton-v*`, so upstream's own tags are invisible to it no matter what they
  are named.
- **`v1.37.0-4` is a semver PRERELEASE**, which sorts *before* `v1.37.0`. Any
  tooling that orders by version would rank a fork release below the upstream
  release it is built on top of.

When the fork merges a new upstream tag, the baseline moves and the counter
restarts: `bayton-v1.38.0-1`.

## Why annotated tags are required

The deploy script orders candidates by `taggerdate` and **refuses a lightweight
tag outright**, with an alert.

A lightweight tag is just a pointer to a commit and carries no date of its own,
so `creatordate` falls back to the *commit* date. That has two failure modes,
the first of which was hit while testing this kit:

- Two lightweight tags on the same commit tie, and the order between them is
  arbitrary. A newer tag lost to an older one.
- A lightweight tag placed on an older commit sorts by that old commit's date,
  so it is never selected at all. A hotfix tagged onto an earlier commit would
  silently never deploy.

Rather than guess an order, the script refuses and tells you to re-tag.

## What the container does

`pokecollector-update.timer` runs every five minutes. Polling rather than a
webhook because the host is LAN only and GitHub cannot reach it, so nothing new
is exposed to the internet.

Each run:

1. Fetches tags. If the newest `bayton-v*` tag is already deployed, exits 0
   having done nothing.
2. Refuses the tag if it is not annotated.
3. Adds a **git worktree** at the tagged commit and runs `npm ci` and the build
   *there*. A failed install or build cannot touch the serving checkout, and
   the running release carries on untouched.
4. Copies the previous `dist/` aside, checks out the tag in the serving
   checkout, and installs the newly built `dist/`.
5. Restarts the service and polls `/api/health` (20 attempts, 3s apart).
6. On success: records the tag, verifies `HEAD` really is the tagged commit,
   and cleans up.
7. On failure: restores the previous commit and the previous `dist/`, restarts,
   and alerts. If health does not come back even then, it alerts that the
   service is down and needs a person.

A failed release leaves the bad tag as the newest, so the timer will retry it
until it is fixed or superseded. That is deliberate: the alternative is a
silent stall.

## Monitoring

Liveness was already covered before this kit existed.
`pokecollector-health.timer` polls the health endpoint, restarts the service
after two consecutive failures and alerts through `pokecollector-alert`. That
is better than a single check, so this kit does not duplicate it: two checkers
would race each other's restarts and send two emails per incident.

What nothing covered is **drift**, so `pokecollector-drift.timer` runs every
ten minutes and alerts when the serving checkout is not the tag the deploy
recorded. That is what catches a fix applied by hand on the box, which the next
release would otherwise silently overwrite, or a checkout left somewhere
unexpected by an interrupted deploy.

Alerts go through the host's existing `pokecollector-alert`, which holds its
Outpost credentials in `/etc/pokecollector-alerts.env`. One alert path, one
place to configure.

## Installing the kit

```bash
install -m 755 ops/pokecollector-deploy ops/pokecollector-drift /usr/local/bin/
install -m 644 ops/pokecollector-update.service ops/pokecollector-update.timer \
               ops/pokecollector-drift.service ops/pokecollector-drift.timer /etc/systemd/system/

# The live container names its git remote "fork", not "origin".
printf 'POKECOLLECTOR_REMOTE=fork\n' > /etc/pokecollector-deploy.env

systemctl daemon-reload
systemctl enable --now pokecollector-update.timer pokecollector-drift.timer
```

Everything is overridable by environment variable (`POKECOLLECTOR_REPO`,
`POKECOLLECTOR_HEALTH_URL`, `POKECOLLECTOR_TAG_GLOB` and so on) so the scripts
can be exercised somewhere that is not the live container.

## How this was tested

Proven end to end in a throwaway LXD container (`pc-deploy-lab`) against a
local bare repo standing in for GitHub, with a stand-in service so that health
could be made to fail on demand. Eight scenarios, each observed rather than
reasoned about:

| Scenario | Expected | Observed |
|---|---|---|
| New tag, good build | deploys, health passes | exit 0, tag recorded, worktree cleaned |
| Re-run with no new tag | no-op | exit 0, "already on ..." |
| Release that comes up unhealthy | rolls back, alerts | rolled back, marker gone, healthy |
| Release that does not build | running release untouched | exit 1, still on old tag, healthy |
| Good tag after a failed one | recovers | deployed, health passes |
| Lightweight tag | refused with an alert | exit 1, nothing deployed |
| Checkout drifted by hand | drift check alerts | exit 1, drift reported |
| Deploy re-run after rework | still deploys and drift stays clean | exit 0, drift exit 0 |

The lightweight-tag rule exists *because* of that testing: the first version
ordered by `creatordate` and a newer tag lost to an older one.
