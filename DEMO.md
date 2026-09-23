# Demo — 5 minutes

Four acts, in this order. The point is not the tooling; it is that a failing
build can block a merge, and that changing who is gated is one line in one file.

Prep: `export GH_TOKEN=$(gh auth token)`

## Reset to a clean start

The demo needs a PR whose head has no `Build Validation` yet.

```bash
git clone https://github.com/gnh374/policy-poc-hybrid.git /tmp/demo && cd /tmp/demo
git checkout -b demo/$(date +%s)
date > probe.txt && git add -A && git commit -m "Demo commit"
git push -u origin HEAD
gh pr create --fill
```

## Act 1 — the build result actually gates the merge

```bash
gh pr view --json mergeStateStatus,statusCheckRollup \
  | jq '{mergeStateStatus, checks:[.statusCheckRollup[]?|{n:(.name//.context),s:(.conclusion//.state)}]}'
```

`gate/merge` is **SUCCESS** and the PR is still **BLOCKED** — because a second,
independent ruleset requires `Build Validation`, which TeamCity has not posted.
This is the layering: two rulesets, both enforced.

Now post it, as the TeamCity meta-runner does:

```bash
SHA=$(gh pr view --json headRefOid --jq .headRefOid)
gh api repos/gnh374/policy-poc-hybrid/statuses/$SHA \
  -f state=success -f context="Build Validation"
```

Re-run the first command: **CLEAN**. Say `failure` instead of `success` and it
goes back to blocked.

## Act 2 — changing who is gated is one line

```bash
grep -A2 policy-poc-hybrid registry.json
```

Remove the `add: ["checks-teamcity"]` block, then:

```bash
python3 apply_policy.py --owner gnh374 --repo policy-poc-hybrid
```

The report shows `policy/checks-teamcity deleted (orphaned)` and the PR above
unblocks without anyone touching GitHub settings. In production that edit
arrives as a merged pull request, reviewed like any other change.

Put the block back and re-run to restore the gate.

## Act 3 — a forgotten build cannot lock a repo

```bash
python3 apply_policy.py --owner gnh374 --repo policy-poc-public
```

```
~ policy-poc-public: checks-common REFUSED -- never observed ['gate/merge']
```

That repo has never produced `gate/merge`, so the requirement is refused rather
than applied. Without this, one typo or one missing build config would leave
every PR in a repo waiting forever, with no error anywhere.

Run it twice — the second run reports zero changes.

## Act 4 — what this replaces

```bash
cat reference/README.md
gh api repos/gnh374/policy-poc-hybrid/rulesets --jq '.[].name'
```

Two scripts write repository protection today and neither knows about the
other: one adds the required check to classic protection, the other deletes
classic protection outright. The four repos that have a gate are one sync run
away from losing it.

Note `my-manual-rule` in that ruleset list — created by hand, and left
untouched by every run above. The applier only manages names it owns.
