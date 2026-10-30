#!/usr/bin/env bash
# shellcheck disable=SC2016  # '$ANSIBLE_VAULT;' strings are literals, not expansions
# End-to-end tests for push-to-github.sh against synthetic repos.
#
# Each case builds a local "Gitea" repo carrying every published branch, runs
# the script with SOURCE_URL/TARGET_URL pointed at local paths, and inspects
# what actually arrived in the local bare "GitHub" repo.
#
# Needs git-filter-repo on PATH, or FILTER_REPO set (see push-to-github.sh).
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/push-to-github.sh"
BRANCHES=(master production cis-benchmark docs/provisioning-diagram image-mode
          maintenance-bootc-upgrade test-clusters)
# Built at runtime so the dummy values never appear literally in this file,
# which the clean case publishes along with the script.
UUID="$(printf '%s-%s-%s-%s-%s' 11111111 2222 3333 4444 555555555555)"
PW="hunter2$(printf pass)"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0

ok()   { echo "ok   - $*"; }
fail() { echo "FAIL - $*"; fails=$((fails + 1)); }
gitc() { git -c user.name=test -c user.email=test@example.com -c init.defaultBranch=master "$@"; }

# new_case <name>: fresh source repo at $src and empty bare target at $dst.
new_case() {
  src="$tmp/$1/src"; dst="$tmp/$1/dst.git"
  gitc init -q "$src"
  gitc init -q --bare "$dst"
}
commit_all() {
  gitc -C "$src" add -A
  gitc -C "$src" commit -qm "$1"
}
publish_branches() {
  for b in "${BRANCHES[@]}"; do
    [[ "$b" == master ]] || gitc -C "$src" branch -f "$b" master
  done
}
run_script() {   # run_script [--dry-run]; sets $rc and $out
  set +e
  out="$(SOURCE_URL="$src" TARGET_URL="$dst" bash "$SCRIPT" "$@" 2>&1)"
  rc=$?
  set -e
}
# Concatenated content of every blob reachable in the target. %(rest) matters:
# without it cat-file takes the whole "<sha> <path>" line as the object name.
target_blobs() {
  git -C "$dst" rev-list --objects --all \
    | git -C "$dst" cat-file --batch-check='%(objecttype) %(objectname) %(rest)' \
    | awk '$1=="blob"{print $2}' \
    | while read -r sha; do git -C "$dst" cat-file -p "$sha"; echo; done
}
target_paths() { git -C "$dst" log --all --name-only --format= | sort -u; }

# --- case 1: clean repo — scrub, redact, prune, push -------------------------
new_case clean
mkdir -p "$src/vars"
printf '$ANSIBLE_VAULT;1.1;AES256\n3132\n' > "$src/vars/vault.yml"
printf '$ANSIBLE_VAULT;1.1;AES256\n3334\n' > "$src/vault copy.yml"
printf 'proxmox_api_token: "%s"  # leaked\n' "$UUID" > "$src/my vars.yml"
printf 'foreman_password: REDACTED   # store in vault\n' "$PW" > "$src/vars/foreman.yml"
printf 'hello\n' > "$src/README.md"
# The real repo publishes this script and its tests, which mention the vault
# header and the secret patterns; they must not trip the gate themselves.
cp "$SCRIPT" "$src/push-to-github.sh"
mkdir -p "$src/tests" && cp "$0" "$src/tests/test-push-to-github.sh"
commit_all "seed with secrets"
# Later commits replace the literals, as in the real history.
printf 'proxmox_api_token: "{{ vault_proxmox_api_token }}"\n' > "$src/my vars.yml"
printf 'foreman_password: "{{ vault_foreman_password }}"\n' > "$src/vars/foreman.yml"
commit_all "use vault references"
publish_branches
gitc -C "$src" checkout -q -b feature/unpublished
printf 'api_secret: "%s"\n' "$UUID" > "$src/leak.yml"
commit_all "secret on a branch that is never published"
gitc -C "$src" checkout -q master

run_script --dry-run
if [[ $rc -eq 0 ]] && grep -q 'secret scan clean' <<<"$out" \
   && [[ -z "$(git -C "$dst" for-each-ref)" ]]; then
  ok "dry run passes the scan and pushes nothing"
else
  fail "dry run (rc=$rc): $out"
fi

run_script
if [[ $rc -eq 0 ]]; then ok "real push to local target succeeds"; else fail "push (rc=$rc): $out"; fi

pushed="$(git -C "$dst" for-each-ref --format='%(refname:short)' refs/heads | sort)"
want="$(printf '%s\n' "${BRANCHES[@]}" | sort)"
if [[ "$pushed" == "$want" ]]; then ok "exactly the 7 BRANCHES are published"; else fail "published branches: $pushed"; fi

blobs="$(target_blobs)"
paths="$(target_paths)"
# Guard: the "absent" checks below would pass vacuously on empty output.
if grep -qxF hello <<<"$blobs" && grep -qxF README.md <<<"$paths"; then
  ok "target history is readable (guard against vacuous passes)"
else
  fail "could not read the target's blobs/paths"
fi
if ! grep -qF -- "$UUID" <<<"$blobs"; then ok "no literal token UUID anywhere in published history"; else fail "UUID reached the target"; fi
if ! grep -qE -- '^[[:space:]]*[$]ANSIBLE_VAULT;[0-9.]+;' <<<"$blobs"; then ok "no vault-encrypted blob in published history"; else fail "vault blob reached the target"; fi
if ! grep -qxF 'vars/vault.yml' <<<"$paths"; then ok "vars/vault.yml absent from history"; else fail "vars/vault.yml reached the target"; fi
if ! grep -qxF 'leak.yml' <<<"$paths"; then ok "non-published branch content pruned"; else fail "leak.yml reached the target"; fi
if ! grep -qF -- "$PW" <<<"$blobs"; then ok "plaintext foreman_password redacted"; else fail "foreman_password reached the target"; fi
if grep -qF 'proxmox_api_token: "REDACTED"' <<<"$blobs" \
   && grep -qF 'proxmox_api_token: "{{ vault_proxmox_api_token }}"' <<<"$blobs" \
   && grep -qxF 'my vars.yml' <<<"$paths"; then
  ok "token redacted in the spaced path, vault reference left intact"
else
  fail "redaction/reference not as expected"
fi

# --- case 2: unredactable secret in a path with spaces aborts ----------------
new_case dirty
printf 'db_password: "%s"\n' "$UUID" > "$src/secret file.yml"
commit_all "a secret no replace rule covers"
publish_branches
run_script
if [[ $rc -ne 0 ]] && grep -q 'HIT .*secret file.yml' <<<"$out" \
   && grep -q 'nothing was pushed' <<<"$out" \
   && [[ -z "$(git -C "$dst" for-each-ref)" ]]; then
  ok "secret in 'secret file.yml' aborts the push and the target stays empty"
else
  fail "dirty repo (rc=$rc): $out"
fi

# --- case 3: an inline !vault value in a YAML file aborts ---------------------
new_case inline
printf 'db_password: !vault |\n  $ANSIBLE_VAULT;1.1;AES256\n  3132\n' > "$src/group vars.yml"
commit_all "inline vault value"
publish_branches
run_script
if [[ $rc -ne 0 ]] && grep -q 'HIT .*group vars.yml' <<<"$out" \
   && [[ -z "$(git -C "$dst" for-each-ref)" ]]; then
  ok "inline !vault value aborts the push and the target stays empty"
else
  fail "inline vault (rc=$rc): $out"
fi

# --- case 4: a missing published branch is a hard error ----------------------
new_case missing
printf 'hello\n' > "$src/README.md"
commit_all init
run_script --dry-run
if [[ $rc -ne 0 ]] && grep -q "branch 'production' not found" <<<"$out"; then
  ok "missing BRANCHES entry fails loudly"
else
  fail "missing branch (rc=$rc): $out"
fi

echo
if ((fails)); then echo "$fails failure(s)"; exit 1; fi
echo "all tests passed"
