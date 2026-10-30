#!/usr/bin/env bash
#
# push-to-github.sh — publish the Gitea repo to the public GitHub copy with
# secrets scrubbed from history.
#
# Gitea (git.garaventaville.com) is the source of truth. GitHub is a public
# copy that must NOT contain vars/vault.yml (even though it's ansible-vault
# encrypted) or any plaintext credential that was ever committed. Because we
# rewrite history, GitHub's commit SHAs diverge from Gitea's, so this always
# force-pushes: only ever push TO GitHub via this script, never pull from it.
#
# Each run is a fresh mirror clone of Gitea in a temp dir; your working copy is
# never touched, and nothing is pushed unless the secret scan comes back clean.
#
# Requires: git-filter-repo   (pip install git-filter-repo==2.47.0), or point
#                             FILTER_REPO at it, e.g.
#                             FILTER_REPO="python /path/to/git-filter-repo"
#           gitleaks          optional; run as an extra scan when installed.
#                             REQUIRE_GITLEAKS=1 makes it mandatory.
#
# Usage:
#   ./push-to-github.sh            # scrub, scan, force-push BRANCHES
#   ./push-to-github.sh --dry-run  # everything except the push

set -euo pipefail

# --- config -----------------------------------------------------------------
SOURCE_URL="${SOURCE_URL:-git@git.garaventaville.com:Garaventaville/kubernetes-cluster-provisioning.git}"
TARGET_URL="${TARGET_URL:-git@github.com:delgado23/kubernetes-cluster-provisioning.git}"
DEFAULT_BRANCH="master"

# Branches to publish. Anything else on Gitea (feature branches, refs/pull/*)
# is dropped before filtering and never leaves this machine.
BRANCHES=(master production cis-benchmark docs/provisioning-diagram image-mode
          maintenance-bootc-upgrade test-clusters)

# Paths removed from ALL history before pushing.
EXCLUDE_PATHS=(vars/vault.yml)

# Literal values rewritten in ALL history (git filter-repo --replace-text
# syntax). Patterns only — never put a real secret in this file.
REPLACE_RULES=(
  # Proxmox API token secret (a UUID) assigned to proxmox_api_token / proxmox_password
  'regex:(proxmox_(?:api_token|password)["'"'"']?\s*[:=]\s*["'"'"']?)[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}==>\1REDACTED'
  # Plaintext foreman_password (anything that isn't a Jinja/vault reference)
  'regex:(foreman_password:REDACTED#]+==>\1REDACTED'
)

# High-signal secret patterns; any hit in the rewritten history aborts the push.
# shellcheck disable=SC2016  # '\$ANSIBLE_VAULT;' is a literal $, not an expansion
SCAN_PATTERNS=(
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'AKIA[0-9A-Z]{16}'
  'gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{60,}'
  'glpat-[A-Za-z0-9_-]{20}'
  'xox[baprs]-[A-Za-z0-9-]{10,}'
  '\bhvs\.[A-Za-z0-9]{24,}'
  '\$ANSIBLE_VAULT;'
  'PVEAPIToken=[^ =]+=[0-9a-fA-F]{8}-'
  '(token|password|secret)[A-Za-z_]*["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
)
# ----------------------------------------------------------------------------

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

die() { echo "!! $*" >&2; exit 1; }

# Every blob reachable from the remaining refs, one "<sha> <path>" per line.
# The path is everything after the first space, so paths containing spaces
# are kept (filtering rev-list output on field count would silently drop them).
list_blobs() {
  git rev-list --objects --all \
    | git cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
    | sed -n 's/^blob //p'
}

if [[ -n "${FILTER_REPO:-}" ]]; then
  read -r -a filter_repo <<<"$FILTER_REPO"
elif git filter-repo --version >/dev/null 2>&1; then
  filter_repo=(git filter-repo)
else
  die "git-filter-repo not found. Install it:  pip install git-filter-repo==2.47.0  (or set FILTER_REPO)"
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/kcp-github.XXXXXX")"
trap 'rm -rf "$work"' EXIT

echo ">> cloning $SOURCE_URL"
git clone --mirror "$SOURCE_URL" "$work/repo.git" >/dev/null 2>&1
cd "$work/repo.git"

for b in "${BRANCHES[@]}"; do
  git rev-parse --verify -q "refs/heads/$b" >/dev/null || die "branch '$b' not found on Gitea; update BRANCHES"
done

# Drop every ref that isn't a branch we publish, so filter-repo only rewrites
# (and we only scan and push) what actually goes to GitHub.
echo ">> pruning non-published refs"
keep_re="$(printf 'refs/heads/%s\n' "${BRANCHES[@]}" | paste -sd'|' -)"
git for-each-ref --format='%(refname)' \
  | { grep -Ev "^(${keep_re})$" || true; } \
  | sed 's/^/delete /' \
  | git update-ref --stdin

# Any ansible-vault encrypted file version under any path (vars/foreman.yml was
# whole-file encrypted for a while) is dropped as well, by blob id.
: > "$work/vault-blobs.txt"
while read -r sha _; do
  [[ "$(git cat-file -p "$sha" | head -c 15)" == "\$ANSIBLE_VAULT;"* ]] && echo "$sha" >> "$work/vault-blobs.txt"
done < <(list_blobs)

echo ">> stripping ${EXCLUDE_PATHS[*]} and $(wc -l < "$work/vault-blobs.txt") vault-encrypted blob(s); redacting known credential patterns"
path_args=()
for p in "${EXCLUDE_PATHS[@]}"; do path_args+=(--path "$p"); done
printf '%s\n' "${REPLACE_RULES[@]}" > "$work/replace.txt"
"${filter_repo[@]}" --force --invert-paths "${path_args[@]}" --replace-text "$work/replace.txt" \
  --strip-blobs-with-ids "$work/vault-blobs.txt" > "$work/filter-repo.log" 2>&1 \
  || { cat "$work/filter-repo.log" >&2; die "git filter-repo failed"; }

for p in "${EXCLUDE_PATHS[@]}"; do
  if git log --oneline --all -- "$p" | grep -q .; then
    die "ABORT: '$p' still present in history after filtering"
  fi
done
echo ">> verified: excluded paths absent from all published history"

# Scan every blob reachable from the published branches. Only pattern names
# and file paths are printed, never the matched text.
echo ">> scanning rewritten history for secrets"
hits=0
any="$(printf '(%s)|' "${SCAN_PATTERNS[@]}")"; any="${any%|}"
while read -r sha path; do
  body="$(git cat-file -p "$sha" | tr -d '\0')"
  grep -qE -- "$any" <<<"$body" || continue
  for pat in "${SCAN_PATTERNS[@]}"; do
    if grep -qE -- "$pat" <<<"$body"; then
      echo "   HIT  [$pat]  $path ($sha)"
      hits=$((hits + 1))
    fi
  done
done < <(list_blobs)
if command -v gitleaks >/dev/null 2>&1; then
  gitleaks git --no-banner --redact --log-level warn . || hits=$((hits + 1))
elif [[ "${REQUIRE_GITLEAKS:-0}" == 1 ]]; then
  die "REQUIRE_GITLEAKS=1 but gitleaks is not installed"
else
  echo "   (gitleaks not installed; built-in patterns only)"
fi
((hits == 0)) || die "ABORT: $hits secret-scan finding(s); nothing was pushed"
echo ">> verified: secret scan clean"

git remote add github "$TARGET_URL"

if [[ "$DRY_RUN" == 1 ]]; then
  echo ">> [dry-run] would force-push to $TARGET_URL:"
  git for-each-ref --format='   %(refname:short)  %(objectname:short)' refs/heads
  exit 0
fi

# Default branch first (keeps GitHub's default branch right on a fresh repo).
echo ">> pushing $DEFAULT_BRANCH"
git push --force github "refs/heads/${DEFAULT_BRANCH}:refs/heads/${DEFAULT_BRANCH}"

rest=()
for b in "${BRANCHES[@]}"; do
  [[ "$b" == "$DEFAULT_BRANCH" ]] && continue
  rest+=("refs/heads/$b:refs/heads/$b")
done
if ((${#rest[@]})); then
  echo ">> pushing: ${BRANCHES[*]/$DEFAULT_BRANCH}"
  git push --force github "${rest[@]}"
fi

stale="$(git ls-remote --heads github | awk '{print $2}' | sed 's#^refs/heads/##' \
  | grep -vxF -f <(printf '%s\n' "${BRANCHES[@]}") || true)"
[[ -n "$stale" ]] && printf '>> note: GitHub branches not in BRANCHES (left as-is):\n%s\n' "$stale"

echo ">> done. GitHub copy updated: $TARGET_URL"
