#!/usr/bin/env bash
#
# Applies the repository rulesets in .github/rulesets/ to GitHub.
#
# Rulesets are the API-side half of this repository's rules: the workflows in .github/workflows/
# decide whether a change is good, and these decide that nobody can land one without asking. They
# cannot live in a file the way a workflow does, so this script is how the JSON in this repo becomes
# the configuration on GitHub — run it after changing a ruleset, and re-run it to reconcile drift.
#
# What .github/rulesets/main.json enforces on the default branch:
#   - no direct pushes: every change arrives as a pull request
#   - no force-pushes, no branch deletion, linear history only (squash merges)
#   - one approving review, dismissed when new commits land, and the last pusher cannot self-approve
#   - every review thread resolved
#   - every required CI job green, on a branch that is up to date with main
#
# With one exception, and it is deliberate: the `github-actions` app bypasses the ruleset, because
# cd.yml's release job pushes the `chore(release):` changelog commit and its tag straight onto main.
# A release cannot open a pull request for itself — the pull request would need a release to review.
#
# Usage:
#   ./scripts/branch-protection.sh                 # apply, strict defaults
#   APPROVALS=0 ./scripts/branch-protection.sh     # solo repo: CI still gates, no reviewer needed
#   CODEOWNER_REVIEW=1 ./scripts/branch-protection.sh
#   DRY_RUN=1 ./scripts/branch-protection.sh       # print the payloads, change nothing
#
# Needs the `gh` CLI, authenticated with admin rights on the repository.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v gh >/dev/null || { echo "gh CLI is required: https://cli.github.com" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh is not authenticated; run 'gh auth login'" >&2; exit 1; }

REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
APPROVALS="${APPROVALS:-1}"
CODEOWNER_REVIEW="${CODEOWNER_REVIEW:-0}"
DRY_RUN="${DRY_RUN:-0}"

echo "repository: $REPO"

# Rulesets are a paid feature on a PRIVATE repository: a free account or organisation gets 403
# here, and the POST below would otherwise fail with a bare API error that says nothing about why.
# Checking first turns that into an answer.
if ! probe="$(gh api "repos/$REPO/rulesets" 2>&1)" && grep -q "Upgrade to GitHub" <<<"$probe"; then
  cat >&2 <<MSG
GitHub will not apply rulesets to this repository.

  $REPO is private, and rulesets (and classic branch protection) need GitHub Pro or Team on a
  private repository. The API answers:

    403 Upgrade to GitHub Pro or make this repository public

  Either move the organisation to GitHub Team, or make the repository public, and re-run this
  script — the rules in .github/rulesets/ apply unchanged once the plan allows them.

  Until then, 'make hooks' installs the local pre-push guard that refuses a direct push to main.
  It is per-clone and bypassable with --no-verify; CONTRIBUTING.md says what it is worth.
MSG
  exit 1
fi

# An app bypass is addressed by numeric app id, which is not something anyone should have to trust a
# literal for. Resolve it from the app itself; the id in the JSON is only the fallback.
ACTIONS_APP_ID="$(gh api /apps/github-actions --jq .id 2>/dev/null || true)"

# An Integration bypass actor is only legal if that app is installed on the owner organisation.
# It is not enough for the app to exist: GitHub answers
#
#   422 Actor GitHub Actions integration must be part of the ruleset source or owner organization
#
# and the whole POST fails, which is a confusing way to learn that an org never installed it.
# kwami-labs is such an org. So the installed app ids are resolved here and render() drops any
# Integration actor that is not among them, rather than letting the API reject the payload.
OWNER="${REPO%%/*}"
INSTALLED_APP_IDS="$(gh api "orgs/$OWNER/installations" --jq '.installations[].app_id' 2>/dev/null | tr '\n' ',' || true)"

# The two knobs a repository actually differs on. A solo repository with APPROVALS=1 and no second
# maintainer is a deadlock — nobody can approve their own pull request — so it is settable rather
# than baked into the JSON.
render() {
  APPROVALS="$APPROVALS" CODEOWNER_REVIEW="$CODEOWNER_REVIEW" ACTIONS_APP_ID="$ACTIONS_APP_ID" \
  INSTALLED_APP_IDS="$INSTALLED_APP_IDS" \
    python3 - "$1" <<'PY'
import json, os, sys

ruleset = json.load(open(sys.argv[1]))

for rule in ruleset["rules"]:
    if rule["type"] == "pull_request":
        rule["parameters"]["required_approving_review_count"] = int(os.environ["APPROVALS"])
        rule["parameters"]["require_code_owner_review"] = os.environ["CODEOWNER_REVIEW"] == "1"

app_id = os.environ.get("ACTIONS_APP_ID")
installed = {int(x) for x in os.environ.get("INSTALLED_APP_IDS", "").split(",") if x.strip()}

kept = []
for actor in ruleset.get("bypass_actors", []):
    if actor.get("actor_type") != "Integration":
        kept.append(actor)
        continue
    if app_id:
        actor["actor_id"] = int(app_id)
    # An empty install list means the lookup failed (a user-owned repository has no org
    # installations endpoint); trust the JSON rather than silently stripping the actor.
    if installed and actor["actor_id"] not in installed:
        print(
            f"  warn     dropping bypass actor: app {actor['actor_id']} is not installed on "
            "this organisation, and GitHub rejects the whole ruleset if it is named",
            file=sys.stderr,
        )
        continue
    kept.append(actor)
ruleset["bypass_actors"] = kept

json.dump(ruleset, sys.stdout)
PY
}

# Rulesets are addressed by numeric id, not by name, so an existing one with the same name has to be
# found first — otherwise every run would stack another copy of the same rules on the branch.
existing_id() {
  gh api "repos/$REPO/rulesets" --jq ".[] | select(.name == \"$1\") | .id" 2>/dev/null | head -1
}

apply() {
  local file="$1" name payload id
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$file")"
  payload="$(render "$file")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "--- $name (dry run)"
    printf '%s' "$payload" | python3 -m json.tool
    return
  fi

  id="$(existing_id "$name")"
  if [[ -n "$id" ]]; then
    printf '%s' "$payload" | gh api --silent -X PUT "repos/$REPO/rulesets/$id" --input -
    echo "  updated  $name (id $id)"
  else
    printf '%s' "$payload" | gh api --silent -X POST "repos/$REPO/rulesets" --input -
    echo "  created  $name"
  fi
}

for file in .github/rulesets/*.json; do
  apply "$file"
done

if [[ "$DRY_RUN" != "1" ]]; then
  # Repository-level settings that are not expressible as a ruleset.
  gh api --silent -X PATCH "repos/$REPO" \
    -F allow_squash_merge=true \
    -F allow_merge_commit=false \
    -F allow_rebase_merge=false \
    -F delete_branch_on_merge=true \
    -F allow_auto_merge=true \
    -f squash_merge_commit_title=PR_TITLE \
    -f squash_merge_commit_message=PR_BODY
  echo "  updated  merge settings (squash only, delete branch on merge, auto-merge enabled)"

  echo
  echo "main is protected: pull requests only, $APPROVALS approval(s), all required CI checks green."
  echo "Repository admins bypass it, so cd.yml can push the release commit and tag — but only when"
  echo "it pushes AS an admin. That is what the RELEASE_TOKEN secret is for: GITHUB_TOKEN acts as"
  echo "github-actions[bot], which holds no repository role and is NOT covered by that bypass."
  echo "See CONTRIBUTING.md, section Releases."
  [[ "$APPROVALS" == "0" ]] && echo "note: APPROVALS=0 — CI still gates every merge, but no human review is required."
fi
