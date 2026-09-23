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
registry.json              repo class -> rulesets, plus exceptions
settings.json              repo settings, grouped by endpoint and scoped by
                           repo visibility (not expressible as ruleset rules)
apply_policy.py            the reconciler
```

## The two non-GitHub files

`policies/*.json` follow **GitHub's** rulesets schema — get them wrong and the
API returns 422, which is how the octopus-base defects below were found.

`registry.json` and `settings.json` are **ours**. Nothing validates them but
this script, so a typo like `ad` instead of `add` is silently ignored. That is
why a PR-time validator is not optional in the real thing.

| | `registry.json` | `settings.json` |
|---|---|---|
| Answers | which rulesets a repo class gets | what value a setting takes |
| Varies by | repo class (from the repo's topic) | repo visibility |
| Drives | `POST/PUT /repos/../rulesets` | `PATCH /repos/..` and two other endpoints |

## How a repo is classified

The repo says so itself, through a GitHub topic:

```
octopus-hybrid  ->  main-protection, policy/checks-common, policy/checks-teamcity
octopus-public  ->  main-protection, policy/checks-common
no topic        ->  main-protection, and a warning in the report
```

So `Build Validation` is required for every hybrid component by default —
nobody has to remember to register it. The class is visible in the GitHub UI,
`create_repo.py` can set it at creation (it already has `set_topics()`), and
reading it costs no extra API call because topics come back with the repo
listing.

Topics are editable by anyone with push access, which would otherwise make the
gate removable without review. So **observed fact outranks the label**: a repo
that has posted `Build Validation` is treated as hybrid whatever its topic
says, and the mismatch is reported.

| Topic on a repo that posts `Build Validation` | Result |
|---|---|
| `octopus-hybrid` | applied, no warning |
| `octopus-public` | still hybrid, warning to fix the topic |
| none | still hybrid, warning to fix the topic |
| both class topics | hard error, nothing applied |

Dropping a gate therefore needs an `exceptions` entry with a `waiver` — a
reviewed pull request, not a one-word edit.

`settings.json` is grouped because `create_repo.py` has four separate settings
steps behind three different endpoints, and two of them skip private repos:

```
repo                         PATCH /repos/{repo}
actions_workflow             PUT   /repos/{repo}/actions/permissions/workflow
code_scanning_default_setup  PATCH /repos/{repo}/code-scanning/default-setup
```

Each group takes `all`, `public` and `private` blocks, merged weakest-first.
A group with nothing declared for a repo's visibility is skipped — which is
exactly what `patch_security()` and `disable_codeql_default_setup()` do today.

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
| `settings.json` covers 11 fields across 3 endpoints | `patch_settings()` sets 17 — the rest are cosmetic repo toggles (`has_issues`, squash commit titles) worth copying across verbatim |
| urllib → `gh` CLI | done — matches `create_repo.py`'s `run_gh()` so the lift is a copy, not a rewrite |

## Throwaway repos

`gnh374/policy-poc`, `policy-poc-hybrid`, `policy-poc-public` — all public, all
disposable. `policy-poc` was created but never used; the policy files stayed
local. Delete all three when the findings are written up.
