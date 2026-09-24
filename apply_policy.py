#!/usr/bin/env python3
"""
apply_policy.py -- reconcile GitHub repository rulesets and repo settings
against a policy directory.

Written to be lifted into releng/gh-permissions-granting/create_repo.py, so it
deliberately borrows that file's shape: `gh` CLI for transport via run_gh(),
a RunState carrying step failures, log() for progress, and the same report
markers. Nothing here needs a dependency `gh` does not already bring.

Model
  A repo declares its class through a GitHub topic -- `octopus-hybrid` or
  `octopus-public` -- and the registry maps that class to a set of rulesets.
  Hybrid components therefore require `Build Validation` by default; it is not
  something anyone has to remember to register.

  An `exceptions` entry can tighten with `add` or loosen with `remove`, and
  `remove` requires a `waiver`, so dropping a gate always costs a reviewed
  pull request.

  Topics are editable by anyone with push access, so observed fact outranks the
  label: a repo that has posted `Build Validation` is treated as hybrid no
  matter what its topic claims. Relabelling cannot remove a gate.

  `main-protection` keeps the name create_repo.py already uses in production,
  so adopting this needs no rename across the fleet. Rulesets we introduce are
  named `policy/<name>`.

Enforcement
  Policy is applied as written. A required check is set even on a repo that has
  never produced it, which blocks every PR there until the check starts
  arriving -- deliberately, so a missing build is a loud problem rather than a
  quiet exemption. The run reports each repo it has just put in that state.

  Contexts are still read, both to report those repos and to keep the class
  cross-check honest. They come from recent PR head commits. A merge-gate workflow
  triggered only by `pull_request` leaves no trace on the default branch, so
  looking there reports false absences. Commit statuses (TeamCity) and check
  runs (Actions, apps) are separate APIs; both count.

Usage
  python3 apply_policy.py --owner gnh374 --all
  python3 apply_policy.py --owner gnh374 --repo policy-poc-hybrid --dry-run

Only the owners in ALLOWED_OWNERS can be targeted. This is the POC copy and it
stays off octopusden entirely -- read-only included.

Auth: $GH_TOKEN or $GITHUB_TOKEN, needing `administration:write` on the targets.

Known gap
  A token without admin access gets an empty ruleset list rather than an
  error, which is indistinguishable from "this repo has no rulesets" -- so the
  script would try to create ones that already exist. Before this runs
  anywhere real it needs a preflight that proves admin access first;
  create_repo.py already has a preflight() step shaped for exactly that.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""

ALLOWED_OWNERS = {"gnh374"}        # POC guard -- see main()
BASELINE = "main-protection"          # the name create_repo.py already uses
OVERLAY_PREFIX = "policy/"            # everything else we manage
MANAGED_FIELDS = ("target", "enforcement", "conditions", "bypass_actors", "rules")


def _read(path):
    def reader(full_name):
        ok, body, _ = gh_json("api", path.format(repo=full_name))
        return body if ok and isinstance(body, dict) else None
    return reader


# One entry per settings group in settings.json. `read: None` means the repo
# object already fetched carries the current values, so no extra call is needed.
SETTINGS_GROUPS = {
    "repo": {
        "read": None,
        "write": lambda r: ("api", "-X", "PATCH", f"repos/{r}"),
    },
    "actions_workflow": {
        "read": _read("repos/{repo}/actions/permissions/workflow"),
        "write": lambda r: ("api", "-X", "PUT",
                            f"repos/{r}/actions/permissions/workflow"),
    },
    "code_scanning_default_setup": {
        "read": _read("repos/{repo}/code-scanning/default-setup"),
        "write": lambda r: ("api", "-X", "PATCH",
                            f"repos/{r}/code-scanning/default-setup"),
    },
}


# --------------------------------------------------------------------------
# gh plumbing -- same shape as create_repo.py's helpers
# --------------------------------------------------------------------------

def run_gh(*args, stdin_data=None):
    env = {**os.environ, "GH_TOKEN": TOKEN}
    return subprocess.run(["gh", *args], input=stdin_data, capture_output=True,
                          text=(stdin_data is None), env=env)


def gh_err(r):
    s = r.stderr if isinstance(r.stderr, str) else (r.stderr or b"").decode()
    return " ".join(s.split())[:200]


def gh_json(*args, stdin_data=None):
    """(ok, parsed, error_text)."""
    r = run_gh(*args, stdin_data=stdin_data)
    if r.returncode != 0:
        return False, None, gh_err(r)
    out = r.stdout if isinstance(r.stdout, str) else (r.stdout or b"").decode()
    if not out.strip():
        return True, None, ""
    try:
        return True, json.loads(out), ""
    except json.JSONDecodeError:
        return False, None, "unparseable response"


def gh_paged(path):
    """Every page of a list endpoint. octopusden already has more repos than a
    single default page returns, so this is not optional."""
    ok, data, err = gh_json("api", "--paginate", "--slurp", path)
    if not ok:
        return False, [], err
    flat = []
    for page in (data or []):
        flat.extend(page if isinstance(page, list) else [page])
    return True, flat, ""


def log(msg):
    print(msg, flush=True)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(2)


class RunState:
    def __init__(self):
        self.lines = []          # (marker, text)

    def add(self, marker, text):
        self.lines.append((marker, text))
        log(f"  {marker} {text}")

    def count(self, marker):
        return sum(1 for m, _ in self.lines if m == marker)


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

def _load(directory, stem):
    for suffix, loader in ((".json", json.load), (".yaml", None)):
        p = Path(directory) / f"{stem}{suffix}"
        if not p.exists():
            continue
        if suffix == ".yaml":
            try:
                import yaml
            except ImportError:
                die(f"{p} needs PyYAML; add it to requirements or use {stem}.json")
            with p.open() as fh:
                return yaml.safe_load(fh)
        with p.open() as fh:
            return loader(fh)
    die(f"neither {stem}.json nor {stem}.yaml in {directory}")


def load_policy(directory):
    pol_dir = Path(directory) / "policies"
    if not pol_dir.is_dir():
        die(f"no policies/ directory in {directory}")
    overlays = {}
    for path in sorted(pol_dir.glob("*.json")):
        spec = json.loads(path.read_text())
        stem = path.stem
        expected = stem if stem == BASELINE else f"{OVERLAY_PREFIX}{stem}"
        if spec.get("name") != expected:
            die(f"{path.name}: ruleset name must be '{expected}', "
                f"found '{spec.get('name')}'")
        overlays[stem] = spec
    if BASELINE not in overlays:
        die(f"policies/{BASELINE}.json is required")
    return {"registry": _load(directory, "registry"),
            "settings": _load(directory, "settings"),
            "overlays": overlays}


def classify(repo, policy):
    """The repo's class, taken from its own GitHub topics.

    Keeping the classification on the repo rather than in the registry means a
    new repo is classified the moment it is created, the class is visible in
    the GitHub UI, and it costs no extra API call -- topics come back with the
    repo listing. The trade-off is that anyone with push access can edit a
    topic, which is what the Build Validation cross-check below is for.
    """
    prefix = policy["registry"].get("class_topic_prefix", "octopus-")
    classes = policy["registry"].get("classes", {})
    found = [t[len(prefix):] for t in repo.get("topics", [])
             if t.startswith(prefix) and t[len(prefix):] in classes]
    if len(found) > 1:
        return None, f"carries more than one class topic: {sorted(found)}"
    return (found[0] if found else None), ""


def resolve_rulesets(repo, cls, policy):
    """Ruleset names for a repo of class `cls`.

    Two separate lists rather than one list of "exceptions" with a direction
    field: tightening and loosening are not the same act. An `additions` entry
    needs no justification -- nobody is harmed by a stricter repo. A `waivers`
    entry drops a ruleset the class asked for, and the list it lives in carries
    `reason` and `review_by` as fields, so the justification is structural
    rather than a rule the applier has to remember to enforce.
    """
    reg = policy["registry"]
    names = list(reg["classes"][cls] if cls else reg.get("unclassified", []))
    repo_name = repo["name"]

    for entry in reg.get("additions", []):
        if entry.get("name") != repo_name:
            continue
        for n in entry.get("rulesets", []):
            if n not in names:
                names.append(n)

    for entry in reg.get("waivers", []):
        if entry.get("name") != repo_name:
            continue
        if not entry.get("reason"):
            die(f"{repo_name}: waiver needs a 'reason'")
        for n in entry.get("drop", []):
            if n in names:
                names.remove(n)

    unknown = [n for n in names if n not in policy["overlays"]]
    if unknown:
        die(f"{repo_name}: unknown ruleset(s) {unknown}")
    return names


def expired_waivers(policy, today=None):
    """Waivers past their review date. A loosening with no expiry quietly
    becomes permanent, so the run surfaces the ones that are overdue."""
    import datetime
    today = today or datetime.date.today().isoformat()
    return [(e["name"], e.get("review_by"), e.get("reason", ""))
            for e in policy["registry"].get("waivers", [])
            if e.get("review_by") and e["review_by"] < today]


def required_contexts(spec):
    out = []
    for rule in spec.get("rules", []):
        if rule.get("type") == "required_status_checks":
            for c in rule.get("parameters", {}).get("required_status_checks", []):
                if c.get("context"):
                    out.append(c["context"])
    return out


# --------------------------------------------------------------------------
# actual state
# --------------------------------------------------------------------------

def list_repos(owner):
    """Every repo the owner owns -- including the private ones.

    `/users/{owner}/repos` lists only what is publicly visible, so a private
    repo would be skipped without ever being reported as ungated: the script
    would not know it exists. When the token authenticates as the owner
    (create_repo.py's preflight already requires that) `/user/repos` sees
    everything they own.
    """
    ok, who, err = gh_json("api", f"users/{owner}")
    if not ok:
        die(f"cannot read owner '{owner}': {err}")

    if who.get("type") == "Organization":
        path = f"orgs/{owner}/repos?type=all"
    else:
        ok, me, _ = gh_json("api", "user")
        if ok and me.get("login") == owner:
            path = "user/repos?affiliation=owner"
        else:
            log(f"WARNING: token does not authenticate as {owner}; listing "
                f"public repos only -- private repos will be missed entirely")
            path = f"users/{owner}/repos"

    ok, repos, err = gh_paged(path)
    if not ok:
        die(f"listing repos for '{owner}': {err}")
    return repos


def observed_contexts(full_name, pr_sample=10):
    shas = []
    ok, prs, _ = gh_json("api", f"repos/{full_name}/pulls?state=all&per_page={pr_sample}")
    if ok and isinstance(prs, list):
        shas += [p["head"]["sha"] for p in prs if p.get("head", {}).get("sha")]
    ok, head, _ = gh_json("api", f"repos/{full_name}/commits?per_page=1")
    if ok and isinstance(head, list) and head:
        shas.append(head[0]["sha"])

    seen = set()
    for sha in dict.fromkeys(shas):
        ok, statuses, _ = gh_json("api", f"repos/{full_name}/commits/{sha}/statuses")
        if ok and isinstance(statuses, list):
            seen.update(s["context"] for s in statuses if s.get("context"))
        ok, runs, _ = gh_json("api", f"repos/{full_name}/commits/{sha}/check-runs")
        if ok and isinstance(runs, dict):
            seen.update(r["name"] for r in runs.get("check_runs", []) if r.get("name"))
    return seen


def managed_rulesets(full_name, known_names):
    """name -> full body, for rulesets we are responsible for. Anything created
    by hand keeps a name we do not recognise and is never touched."""
    ok, summaries, err = gh_paged(f"repos/{full_name}/rulesets")
    if not ok:
        return None, err
    out = {}
    for s in summaries:
        name = s.get("name", "")
        if name != BASELINE and not name.startswith(OVERLAY_PREFIX) \
                and name not in known_names:
            continue
        ok, full, _ = gh_json("api", f"repos/{full_name}/rulesets/{s['id']}")
        out[name] = full if ok and full else s
    return out, ""


def _subset(desired, actual):
    """Everything we assert is present and equal in `actual`.

    Strict equality does not work: GitHub echoes defaults we never sent (a
    rule's own do_not_enforce_on_create, for one), so every run would report
    drift and the report would stop being worth reading. Lists must match in
    length -- a dropped required context is real drift.
    """
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in desired.items())
    if isinstance(desired, list):
        if not isinstance(actual, list) or len(desired) != len(actual):
            return False
        remaining = list(actual)
        for d in desired:
            for i, a in enumerate(remaining):
                if _subset(d, a):
                    remaining.pop(i)
                    break
            else:
                return False
        return True
    return desired == actual


def in_desired_state(spec, current):
    return _subset({f: spec[f] for f in MANAGED_FIELDS if f in spec}, current)


# --------------------------------------------------------------------------
# reconcile
# --------------------------------------------------------------------------

TEAMCITY_CONTEXT = "Build Validation"


def reconcile_rulesets(repo, policy, state, dry_run):
    full_name, name = repo["full_name"], repo["name"]

    cls, problem = classify(repo, policy)
    if problem:
        state.add("x", f"{name}: {problem}")
        return

    actual, err = managed_rulesets(full_name, set(policy["overlays"]))
    if actual is None:
        state.add("x", f"{name}: cannot read rulesets ({err})")
        return

    seen = observed_contexts(full_name)

    # Topics are editable by anyone with push access, so the label on its own
    # cannot be trusted to keep a gate in place -- relabelling a repo would
    # otherwise drop its build gate with no review. Observed fact outranks the
    # label: a repo that has posted Build Validation is treated as hybrid
    # whatever its topic says. Dropping the gate then requires an exception
    # entry with a waiver, which means a reviewed pull request.
    if TEAMCITY_CONTEXT in seen and cls != "hybrid":
        state.add("!", f"{name}: posts {TEAMCITY_CONTEXT!r} but class is "
                       f"{cls or 'unset'} -- treating as hybrid, fix the topic")
        cls = "hybrid"
    elif cls is None:
        state.add("!", f"{name}: no class topic -- treated as unclassified")

    wanted = resolve_rulesets(repo, cls, policy)
    desired_names = []

    for overlay in wanted:
        spec = policy["overlays"][overlay]
        # Policy is enforced as written: a required check is applied whether or
        # not the repo has ever produced it. GitHub waits forever for a context
        # that never arrives -- no error, no timeout, just BLOCKED -- so the gap
        # is reported loudly here instead of being discovered on someone's PR.
        missing = [c for c in required_contexts(spec) if c not in seen]
        if missing:
            state.add("!", f"{name}: {overlay} applied but {missing} has never "
                           f"been produced here -- PRs will block until it is")

        desired_names.append(spec["name"])
        current = actual.get(spec["name"])
        payload = json.dumps(spec).encode()

        if current is None:
            if dry_run:
                state.add("+", f"{name}: {overlay} would be created")
                continue
            ok, _, err = gh_json("api", "-X", "POST",
                                 f"repos/{full_name}/rulesets",
                                 "--input", "-", stdin_data=payload)
            state.add("+" if ok else "x",
                      f"{name}: {overlay} created" if ok
                      else f"{name}: {overlay} create failed -- {err}")
        elif not in_desired_state(spec, current):
            if dry_run:
                state.add("+", f"{name}: {overlay} would be updated")
                continue
            ok, _, err = gh_json("api", "-X", "PUT",
                                 f"repos/{full_name}/rulesets/{current['id']}",
                                 "--input", "-", stdin_data=payload)
            state.add("+" if ok else "x",
                      f"{name}: {overlay} updated" if ok
                      else f"{name}: {overlay} update failed -- {err}")
        else:
            state.add("=", f"{name}: {overlay} already in state")

    # Absence is part of desired state. Without this, dropping an overlay from
    # the registry merges a PR, reports "no change", and changes nothing.
    for rs_name, current in actual.items():
        if rs_name in desired_names:
            continue
        if dry_run:
            state.add("-", f"{name}: {rs_name} would be deleted (orphaned)")
            continue
        ok, _, err = gh_json("api", "-X", "DELETE",
                             f"repos/{full_name}/rulesets/{current['id']}")
        state.add("-" if ok else "x",
                  f"{name}: {rs_name} deleted (orphaned)" if ok
                  else f"{name}: {rs_name} delete failed -- {err}")


def _scopes(repo):
    """Settings blocks that apply to this repo, weakest key first so that a
    visibility-specific value wins over `all`."""
    return ("all", "private" if repo.get("private") else "public")


def _desired_settings(group, repo):
    merged = {}
    for scope in _scopes(repo):
        merged.update(group.get(scope, {}))
    return merged


def reconcile_settings(repo, settings, state, dry_run):
    """Repo settings are not expressible as ruleset rules, and they do not all
    live behind one endpoint -- create_repo.py has four separate steps, two of
    which skip private repos entirely. The policy file carries values and
    visibility; the endpoints stay here because they are implementation.
    """
    full_name, name = repo["full_name"], repo["name"]

    for group_name, group in settings.items():
        cfg = SETTINGS_GROUPS.get(group_name)
        if cfg is None:
            state.add("x", f"{name}: unknown settings group '{group_name}'")
            continue

        desired = _desired_settings(group, repo)
        if not desired:
            continue  # nothing declared for this repo's visibility

        current = repo if cfg["read"] is None else cfg["read"](full_name)
        if current is None:
            state.add("~", f"{name}: {group_name} not readable, skipped")
            continue

        drift = {k: v for k, v in desired.items()
                 if not _subset(v, current.get(k))}
        if not drift:
            state.add("=", f"{name}: {group_name} already in state")
            continue
        if dry_run:
            state.add("+", f"{name}: {group_name} would set {sorted(drift)}")
            continue

        ok, _, err = gh_json(*cfg["write"](full_name), "--input", "-",
                             stdin_data=json.dumps(drift).encode())
        state.add("+" if ok else "x",
                  f"{name}: {group_name} set {sorted(drift)}" if ok
                  else f"{name}: {group_name} failed -- {err}")


def reconcile_repo(repo, policy, state, dry_run):
    if repo.get("archived"):
        state.add("~", f"{repo['name']}: archived, skipped")
        return
    reconcile_rulesets(repo, policy, state, dry_run)
    reconcile_settings(repo, policy["settings"], state, dry_run)


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# repo creation -- the slice of create_repo.py that policy depends on
# --------------------------------------------------------------------------

MERGE_GATE_WORKFLOW = """name: Merge Gate

on:
  pull_request:
  workflow_dispatch:

jobs:
  gate:
    # Job name is the check-run name branch protection points at.
    name: gate/merge
    runs-on: ubuntu-latest
    steps:
      - run: echo "merge gate ok"
"""


def _put_file(full_name, path, text, message):
    import base64
    body = {"message": message,
            "content": base64.b64encode(text.encode()).decode()}
    return gh_json("api", "-X", "PUT", f"repos/{full_name}/contents/{path}",
                   "--input", "-", stdin_data=json.dumps(body).encode())


def create_repo(owner, name, cls, with_ci, state):
    """The part of create_repo.py that policy depends on: the repo exists, it
    is not empty, and it carries its class topic before any ruleset is applied.

    Deliberately partial. The real script also handles contributors, the Prod
    environment, secrets and Sonar provisioning -- none of which change what
    policy does, and all of which need credentials a demo should not need.
    """
    full_name = f"{owner}/{name}"

    ok, _, err = gh_json("api", f"repos/{full_name}")
    if ok:
        state.add("~", f"{name}: already exists, not recreated")
    else:
        ok, _, err = gh_json("api", "-X", "POST", "user/repos", "--input", "-",
                             stdin_data=json.dumps(
                                 {"name": name, "private": False,
                                  "description": "CD-5795 policy POC (throwaway)",
                                  "auto_init": False}).encode())
        if not ok:
            state.add("x", f"{name}: repo create failed -- {err}")
            return None
        state.add("+", f"{name}: repo created")

    # A repo with no commits has no default branch, and a ruleset targeting
    # ~DEFAULT_BRANCH has nothing to attach to. The first file creates it.
    ok, _, _ = _put_file(full_name, "README.md",
                         f"# {name}\n\nThrowaway repo for the CD-5795 policy POC.\n",
                         "Initial commit")
    state.add("+" if ok else "=", f"{name}: README.md"
              + ("" if ok else " already present"))

    if with_ci:
        ok, _, _ = _put_file(full_name, ".github/workflows/merge-gate.yml",
                             MERGE_GATE_WORKFLOW, "Add the merge gate")
        state.add("+" if ok else "=", f"{name}: merge-gate workflow"
                  + ("" if ok else " already present"))
    else:
        state.add("~", f"{name}: no merge-gate workflow -- overlays stay "
                       f"refused until CI exists")

    topic = f"octopus-{cls}"
    ok, _, err = gh_json("api", "-X", "PUT", f"repos/{full_name}/topics",
                         "--input", "-",
                         stdin_data=json.dumps({"names": [topic]}).encode())
    state.add("+" if ok else "x",
              f"{name}: class topic {topic}" if ok
              else f"{name}: topic failed -- {err}")

    ok, repo, _ = gh_json("api", f"repos/{full_name}")
    return repo if ok else None


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--owner", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true",
                   help="every non-archived repo of the owner")
    g.add_argument("--repo", action="append", help="repo name (repeatable)")
    g.add_argument("--create", metavar="NAME",
                   help="create this repo, stamp its class topic, then apply policy")
    p.add_argument("--class", dest="cls", choices=("hybrid", "public"),
                   help="repo class for --create; becomes the octopus-<class> topic")
    p.add_argument("--with-ci", action="store_true",
                   help="with --create, also seed a merge-gate workflow")
    p.add_argument("--policy-dir", default=".")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not TOKEN:
        die("no token found. Set GH_TOKEN (or GITHUB_TOKEN).")
    if run_gh("--version").returncode != 0:
        die("gh CLI not found on PATH.")
    # This is the POC copy. It is confined to throwaway accounts on purpose --
    # nothing here should reach octopusden, not even read-only, until the logic
    # has been lifted into create_repo.py and reviewed there.
    if args.owner not in ALLOWED_OWNERS:
        die(f"owner '{args.owner}' is not allowed in the POC. "
            f"Allowed: {', '.join(sorted(ALLOWED_OWNERS))}. "
            f"Remove this guard only when lifting the logic into create_repo.py.")

    policy = load_policy(args.policy_dir)

    if args.create:
        if not args.cls:
            die("--create needs --class hybrid|public")
        if args.dry_run:
            die("--create cannot be combined with --dry-run")
        log("==> Creating repository")
        created = create_repo(args.owner, args.create, args.cls,
                              args.with_ci, RunState())
        log("")
        if created is None:
            die("repo creation failed; nothing was applied")
        repos = [created]
    elif args.all:
        repos = list_repos(args.owner)
    else:
        repos = []
        for name in args.repo:
            ok, r, err = gh_json("api", f"repos/{args.owner}/{name}")
            if not ok:
                die(f"repo '{args.owner}/{name}': {err}")
            repos.append(r)

    log(f"==> Owner:      {args.owner}")
    log(f"==> Policy dir: {Path(args.policy_dir).resolve()}")
    log(f"==> Overlays:   {sorted(policy['overlays'])}")
    log(f"==> Repos:      {len(repos)}")
    if args.dry_run:
        log("==> DRY-RUN: no changes will be made")
    log("")

    state = RunState()
    for repo in sorted(repos, key=lambda r: r["name"]):
        reconcile_repo(repo, policy, state, args.dry_run)

    log("")
    log("=== Summary ===")
    log(f"  repos:     {len(repos)}")
    log(f"  changed:   {state.count('+')}")
    log(f"  deleted:   {state.count('-')}")
    log(f"  unchanged: {state.count('=')}")
    log(f"  skipped:   {state.count('~')}")
    log(f"  warnings:  {state.count('!')}")
    log(f"  failed:    {state.count('x')}")

    overdue = expired_waivers(policy)
    if overdue:
        log("")
        log(f"Waivers past review date -- {len(overdue)}:")
        for name, when, reason in overdue:
            log(f"  {name}  (due {when})  {reason}")

    # Every repo is attempted before the run fails -- same contract as the
    # sync-rulesets workflow this replaces.
    sys.exit(1 if state.count("x") else 0)


if __name__ == "__main__":
    main()
