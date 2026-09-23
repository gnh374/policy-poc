# Demo — 5 minutes

The loop being demonstrated: **edit policy in one repo, run the applier, watch
the target repo change.** Everything else is supporting detail.

```bash
export GH_TOKEN=$(gh auth token)
git clone https://github.com/gnh374/policy-poc.git && cd policy-poc
```

The applier is fenced to throwaway accounts — `ALLOWED_OWNERS` in the script
refuses any other owner, read-only included. Nothing here can reach octopusden.

---

## Act 1 — edit the policy, watch the repo follow

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

### Edit the policy

`registry.json` maps a repo *class* to a set of rulesets:

```bash
cat registry.json
```

Take `checks-teamcity` out of the `hybrid` class:

```json
"hybrid": ["main-protection", "checks-common"]
```

### Run the applier

```bash
python3 apply_policy.py --owner gnh374 --repo policy-poc-hybrid
```

```
  = policy-poc-hybrid: main-protection already in state
  = policy-poc-hybrid: checks-common already in state
  - policy-poc-hybrid: policy/checks-teamcity deleted (orphaned)
```

### After

```bash
gh api repos/gnh374/policy-poc-hybrid/rulesets --jq '.[].name'
```

`policy/checks-teamcity` is gone. Put it back in `registry.json`, run again,
and it returns. Two directions, one file, nobody touched GitHub settings.

`my-manual-rule` was created by hand and survives every run — the applier only
manages names it owns.

> In production this edit arrives as a merged pull request and the applier runs
> from CI. Wiring that up was deliberately left out of the POC; here it is run
> by hand so the mechanism stays visible.

---

## Act 2 — the class comes from the repo itself

A repo declares its class through a GitHub topic, so a new repo is classified
the moment it is created and the class is visible in the GitHub UI:

```bash
gh api repos/gnh374/policy-poc-hybrid/topics --jq '.names'   # octopus-hybrid
gh api repos/gnh374/policy-poc-public/topics --jq '.names'   # octopus-public
```

That means `Build Validation` is required for every hybrid component by
default. Nobody has to remember to register it.

Now try to get out of it by relabelling:

```bash
gh api repos/gnh374/policy-poc-hybrid/topics --method PUT -f 'names[]=octopus-public'
python3 apply_policy.py --owner gnh374 --repo policy-poc-hybrid
```

```
! policy-poc-hybrid: posts 'Build Validation' but class is public -- treating as hybrid, fix the topic
= policy-poc-hybrid: checks-teamcity already in state
```

**The gate did not move.** Topics are editable by anyone with push access, so
the label alone cannot hold a gate in place. This repo has posted
`Build Validation`, so observed fact outranks the topic. Removing the topic
entirely behaves the same way; carrying both class topics is a hard error and
nothing is applied at all.

Dropping a gate on purpose takes an `exceptions` entry with a `waiver` in
`registry.json` — a reviewed pull request, not a one-word edit.

```bash
gh api repos/gnh374/policy-poc-hybrid/topics --method PUT -f 'names[]=octopus-hybrid'
```

---

## Act 3 — a forgotten build cannot lock a repo

```bash
python3 apply_policy.py --owner gnh374 --repo policy-poc-public
```

```
~ policy-poc-public: checks-common REFUSED -- never observed ['gate/merge']
```

That repo has never produced `gate/merge`, so the requirement is refused rather
than applied. Without this, one typo in a context name would leave every PR in
a repo waiting forever — GitHub reports no error, it simply never merges.

Run it twice: the second run reports zero changes.

---

## Act 4 — what a policy actually does to a PR

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
Both rulesets are enforced; that is the layering the whole design rests on.

Post it, exactly as the TeamCity meta-runner does:

```bash
SHA=$(gh pr view --json headRefOid --jq .headRefOid)
gh api repos/gnh374/policy-poc-hybrid/statuses/$SHA -f state=success -f context="Build Validation"
```

Now **CLEAN**. Send `failure` instead and it blocks again. A failing build
really does stop the merge.

---

## Act 5 — what this replaces

```bash
cd -; cat reference/README.md
```

Two scripts write repository protection today and neither knows about the
other. One adds the required check to classic branch protection; the other
deletes classic protection outright. The four repos that have a gate are one
sync run away from losing it, with nothing logged.

**Consider opening with this act.** Everything above only feels necessary once
it is clear the gate we have today can disappear on its own.
