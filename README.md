# Repository policy POC — CD-5795

Proves the mechanics behind "one policy repo, one applier" before any of it is
built for real. Everything here was run **manually**; wiring it to CI was
deliberately left out of the POC.

## What is here

```
policies/
  main-protection.json     baseline — every repo. Keeps the name create_repo.py
                           already uses in production, so adopting this needs
                           no rename across the fleet.
  checks-common.json       overlay — requires gate/merge
  checks-teamcity.json     overlay — requires Build Validation
registry.json              defaults + per-repo add/remove
settings.json              repo settings (not expressible as ruleset rules)
apply_policy.py            the reconciler
```

## Running it

```bash
GH_TOKEN=$(gh auth token) python3 apply_policy.py \
  --owner gnh374 --repo policy-poc-hybrid --repo policy-poc-public
```

Add `--dry-run` to see the plan without touching anything. `--all` walks every
non-archived repo of the owner — never point that at a personal account by
accident, it will apply policy to unrelated repos.

## What the POC proved

| Question | Answer |
|---|---|
| Do layered rulesets union their required checks? | **Yes** — `gate/merge` green alone left the PR BLOCKED; posting `Build Validation` made it CLEAN |
| Does removing an overlay actually relax the repo? | **Yes** — deleting `policy/checks-teamcity` flipped BLOCKED → CLEAN |
| Are orphaned rulesets pruned? | Yes — renaming baseline left `policy/baseline` behind and the next run deleted it |
| Are hand-made rulesets safe? | Yes — `my-manual-rule` survived every run |
| Does the fail-safe hold? | Yes — `checks-common` refused on a repo that never produced `gate/merge`, both in the lab and in a dry-run against octopusden |
| Is it idempotent? | Yes — second run reports zero changes |
| Is `enforcement: evaluate` usable? | **No** — rejected on a User account (org/Enterprise feature). The rollout cannot rely on shadow mode |

## Defects found in octopus-base

`.github/rulesets/jvm-strict.json` cannot be applied as written. Three defects,
the first two returning 422:

1. `"integration_id": null` — the key must be omitted, not nulled
2. `pull_request` missing `require_last_push_approval` — the field is required
3. `do_not_enforce_on_create` at the top level — it is a *per-rule* parameter
   and is silently ignored where it sits

So `sync-rulesets.yml` would have failed on the first repo even with the token
it was waiting for. Worth its own ticket, independent of this plan.

`create_repo.py` on `master` is unaffected: CD-5916 already moved it to a
ruleset with the correct schema, and it drops the legacy classic protection
after applying. That covers a good part of what this plan set out to do.

## Deltas before any of this reaches production

| Here | Production |
|---|---|
| `required_approving_review_count: 0` | `2` — the POC owner cannot approve their own PR |
| `checks-common` requires only `gate/merge` | add `GitGuardian Security Checks` (confirmed present on hybrid PRs) |
| `enforcement: active` everywhere | unchanged — `evaluate` is unavailable |
| `settings.json` has 5 fields | `patch_settings()` sets 17, plus security and Actions permissions on their own endpoints, and two steps skip when the repo is private |
| urllib → `gh` CLI | done — matches `create_repo.py`'s `run_gh()` so the lift is a copy, not a rewrite |

## Throwaway repos

`gnh374/policy-poc`, `policy-poc-hybrid`, `policy-poc-public` — all public, all
disposable. `policy-poc` was created but never used; the policy files stayed
local. Delete all three when the findings are written up.
