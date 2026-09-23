# Demo — 5 minutes

The loop being demonstrated: **edit policy in one repo, run the applier, watch
the target repo change.** Everything else is supporting detail.

```bash
export GH_TOKEN=$(gh auth token)
git clone https://github.com/gnh374/policy-poc.git && cd policy-poc
```

---

## Act 1 — add and remove a policy

### Before

```bash
gh api repos/gnh374/policy-poc-hybrid/rulesets --jq '.[].name'
```

```
main-protection
my-manual-rule
policy/checks-common
policy/checks-teamcity
```

### Edit the policy — one block in one file

```bash
grep -B1 -A3 policy-poc-hybrid registry.json
```

Delete the `add` block so the entry reads:

```json
{ "name": "policy-poc-hybrid" }
```

### Run the applier

```bash
python3 apply_policy.py --owner gnh374 --repo policy-poc-hybrid
```

```
  = policy-poc-hybrid: main-protection already in state
  = policy-poc-hybrid: checks-common already in state
  - policy-poc-hybrid: policy/checks-teamcity deleted (orphaned)
  = policy-poc-hybrid: settings already in state
```

### After

```bash
gh api repos/gnh374/policy-poc-hybrid/rulesets --jq '.[].name'
```

`policy/checks-teamcity` is gone. Put the `add` block back, run again, and it
returns. Two directions, one file, nobody touched GitHub settings.

Note what did **not** change: `my-manual-rule` was created by hand and survives
every run. The applier only manages names it owns.

> In production this edit arrives as a merged pull request and the applier runs
> from CI. Wiring that up was deliberately left out of the POC — here it is run
> by hand so the mechanism is visible.

---

## Act 2 — what the policy actually does

With `checks-teamcity` back in place, open a PR on the target repo:

```bash
git clone https://github.com/gnh374/policy-poc-hybrid.git /tmp/demo && cd /tmp/demo
git checkout -b demo/$(date +%s)
date > probe.txt && git add -A && git commit -m "Demo commit" && git push -u origin HEAD
gh pr create --fill
gh pr view --json mergeStateStatus,statusCheckRollup \
  | jq '{mergeStateStatus, checks:[.statusCheckRollup[]?|{n:(.name//.context),s:(.conclusion//.state)}]}'
```

`gate/merge` is **SUCCESS** and the PR is still **BLOCKED** — a second,
independent ruleset requires `Build Validation`, which TeamCity has not posted.
Both rulesets are enforced; that is the layering.

Post it, exactly as the TeamCity meta-runner does:

```bash
SHA=$(gh pr view --json headRefOid --jq .headRefOid)
gh api repos/gnh374/policy-poc-hybrid/statuses/$SHA -f state=success -f context="Build Validation"
```

Now **CLEAN**. Send `failure` instead and it blocks again. A failing build
really does stop the merge.

---

## Act 3 — a forgotten build cannot lock a repo

```bash
cd -; python3 apply_policy.py --owner gnh374 --repo policy-poc-public
```

```
~ policy-poc-public: checks-common REFUSED -- never observed ['gate/merge']
```

That repo has never produced `gate/merge`, so the requirement is refused rather
than applied. Without this, one typo in a context name would leave every PR in
a repo waiting forever — GitHub reports no error, it simply never merges.

Run it twice: the second run reports zero changes.

---

## Act 4 — what this replaces

```bash
cat reference/README.md
```

Two scripts write repository protection today and neither knows about the
other. One adds the required check to classic branch protection; the other
deletes classic protection outright. The four repos that have a gate are one
sync run away from losing it, with nothing logged.

**Consider opening with this act.** The mechanism above only feels necessary
once it is clear the current gate can disappear on its own.
