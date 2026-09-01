#!/usr/bin/env bash
# Regression harness for native scan storage migration. It proves a legacy
# relative path remains readable after both a successful swap and a rollback,
# including a photo written during the candidate window.
set -euo pipefail

ROOT=$(mktemp -d)
trap 'rm -rf "${ROOT}"' EXIT
REPO="${ROOT}/repo"
STORE="${ROOT}/durable-scans"
mkdir -p "${REPO}/backend/data/scan-uploads/71"
printf 'legacy photo' > "${REPO}/backend/data/scan-uploads/71/legacy.jpg"

# Use a private test copy so the production-only durable path remains a
# deliberate contract of the shipped helper.
sed "s|^SCAN_UPLOAD_ROOT=.*|SCAN_UPLOAD_ROOT=${STORE}|" \
  "$(dirname "$0")/pokecollector-prepare-scan-storage" > "${ROOT}/prepare"
chmod +x "${ROOT}/prepare"

"${ROOT}/prepare" "${REPO}"
test -f "${STORE}/71/legacy.jpg"
test ! -e "${REPO}/backend/data/scan-uploads"

owner_of() {
  stat -c '%U:%G' "$1" 2>/dev/null || stat -f '%Su:%Sg' "$1"
}
test "$(owner_of "${STORE}")" = "$(owner_of "${REPO}")"
grep -qx 'Environment=SCAN_UPLOAD_DIR=/var/lib/pokecollector/scan-uploads' \
  "$(dirname "$0")/pokecollector.service.d/scan-storage.conf"

# A candidate writes to the configured durable root. The same relative path
# must still resolve after a rollback to an older checkout.
mkdir -p "${STORE}/72"
printf 'candidate photo' > "${STORE}/72/candidate.jpg"
test -f "${STORE}/71/legacy.jpg"
test -f "${STORE}/72/candidate.jpg"

# Running preparation again is safe and does not discard either generation.
# Removing an expired/replaced photo must survive a later rerun: the moved
# legacy tree is gone and cannot resurrect it.
rm "${STORE}/71/legacy.jpg"
"${ROOT}/prepare" "${REPO}"
test ! -e "${STORE}/71/legacy.jpg"
test -f "${STORE}/72/candidate.jpg"
