#!/usr/bin/env python3
"""
configure_branch_protection.py

One-time script to standardize repository protection across hybrid Octopus
components. For a given repo it:
  1. Sets the required *commit status* checks on the protected branch,
     WITHOUT touching the existing approval-review rule.
  2. Enables the repo "Allow auto-merge" setting.
  3. Enables "Automatically delete head branches" after merge.

Auth: reads a token from $GITHUB_TOKEN (or $GH_TOKEN). No `gh` login needed.
      Token needs `repo` / `administration:write` scope on the target repo.

Usage:
  export GITHUB_TOKEN=ghp_xxx
  python3 configure_branch_protection.py <owner/repo> <check-context> [check-context ...]

Options:
  --branch NAME     Protect this branch (default: repo's default branch)
  --dry-run         Print what would happen; make no changes

Env:
  GITHUB_API_URL    API base (default https://api.github.com;
                    for GHE use https://<host>/api/v3)
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def api(method, path, body=None):
    """Call the GitHub REST API. Returns (status_code, parsed_json_or_None)."""
    url = f"{API_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"message": raw}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def build_put_body(prot, scc_body):
    """Turn a GET-protection response into a PUT body, preserving every existing
    rule (reviews, restrictions, toggles) and injecting the new status checks."""
    body = {"required_status_checks": scc_body}

    ea = prot.get("enforce_admins")
    body["enforce_admins"] = ea.get("enabled", False) if isinstance(ea, dict) else bool(ea)

    rpr = prot.get("required_pull_request_reviews")
    if rpr:
        new = {
            "dismiss_stale_reviews": rpr.get("dismiss_stale_reviews", False),
            "require_code_owner_reviews": rpr.get("require_code_owner_reviews", False),
            "required_approving_review_count": rpr.get("required_approving_review_count", 0),
        }
        if "require_last_push_approval" in rpr:
            new["require_last_push_approval"] = rpr["require_last_push_approval"]
        dr = rpr.get("dismissal_restrictions")
        if dr:
            new["dismissal_restrictions"] = {
                "users": [u["login"] for u in dr.get("users", [])],
                "teams": [t["slug"] for t in dr.get("teams", [])],
            }
        bpa = rpr.get("bypass_pull_request_allowances")
        if bpa:
            new["bypass_pull_request_allowances"] = {
                "users": [u["login"] for u in bpa.get("users", [])],
                "teams": [t["slug"] for t in bpa.get("teams", [])],
                "apps": [a["slug"] for a in bpa.get("apps", [])],
            }
        body["required_pull_request_reviews"] = new
    else:
        body["required_pull_request_reviews"] = None

    r = prot.get("restrictions")
    if r:
        body["restrictions"] = {
            "users": [u["login"] for u in r.get("users", [])],
            "teams": [t["slug"] for t in r.get("teams", [])],
            "apps": [a["slug"] for a in r.get("apps", [])],
        }
    else:
        body["restrictions"] = None

    for key in ("required_linear_history", "allow_force_pushes", "allow_deletions",
                "block_creations", "required_conversation_resolution",
                "lock_branch", "allow_fork_syncing"):
        v = prot.get(key)
        if isinstance(v, dict) and "enabled" in v:
            body[key] = v["enabled"]
    return body


def main():
    p = argparse.ArgumentParser(description="Configure branch protection + auto-merge for a repo.")
    p.add_argument("repo", help="owner/repo, e.g. openwaygroup/octopus-dms-service")
    p.add_argument("checks", nargs="+", help="required status-check context name(s)")
    p.add_argument("--branch", help="branch to protect (default: repo default branch)")
    p.add_argument("--strict", action="store_true",
                   help="require branches up to date before merge (default: off; "
                        "leaving it off avoids Renovate/Dependabot re-run churn)")
    p.add_argument("--dry-run", action="store_true", help="print actions, make no changes")
    args = p.parse_args()

    if not TOKEN:
        die("no token found. Set GITHUB_TOKEN (or GH_TOKEN).")
    if "/" not in args.repo:
        die("repo must be in 'owner/repo' form.")

    repo = args.repo
    contexts = args.checks

    # --- resolve branch ---
    status, info = api("GET", f"/repos/{repo}")
    if status == 401:
        die("401 Unauthorized — check the token value/scope.")
    if status == 404:
        die(f"repo '{repo}' not found (or token lacks access).")
    if status >= 400:
        die(f"GET /repos/{repo} failed ({status}): {info.get('message')}")
    branch = args.branch or info.get("default_branch") or "main"

    print(f"==> Repo:    {repo}")
    print(f"==> Branch:  {branch}")
    print(f"==> Checks:  {contexts}")
    if args.dry_run:
        print("==> DRY-RUN: no changes will be made")
    print()

    # --- 1. required status checks (preserve existing review rule) ---
    # We always GET the full protection then PUT it back with status checks
    # injected. This works whether status checks were previously enabled or not,
    # and preserves the approval-review rule and every other setting.
    prot_status, prot = api("GET", f"/repos/{repo}/branches/{branch}/protection")
    scc_body = {"strict": args.strict, "contexts": contexts}

    if prot_status == 200:
        print("==> Protection exists -> PUT protection with status checks (review rule preserved)")
        put_body = build_put_body(prot, scc_body)
    elif prot_status == 404:
        print("==> No protection yet -> creating protection with status checks only")
        print(f"    WARNING: no existing approval-review rule found on '{branch}'.")
        put_body = {
            "required_status_checks": scc_body,
            "enforce_admins": None,
            "required_pull_request_reviews": None,
            "restrictions": None,
        }
    else:
        die(f"GET protection failed ({prot_status}): "
            f"{prot.get('message') if isinstance(prot, dict) else prot}")

    if args.dry_run:
        print(f"    [dry-run] PUT .../protection {put_body}")
    else:
        st, resp = api("PUT", f"/repos/{repo}/branches/{branch}/protection", put_body)
        if st >= 400:
            die(f"setting protection failed ({st}): {resp.get('message')}")
    print("   done: required status checks set.\n")

    # --- 2 & 3. repo settings: auto-merge + delete head branch ---
    print(f"==> Enabling auto-merge + auto-delete head branch on {repo}")
    repo_body = {"allow_auto_merge": True, "delete_branch_on_merge": True}
    if args.dry_run:
        print(f"    [dry-run] PATCH /repos/{repo} {repo_body}")
    else:
        st, resp = api("PATCH", f"/repos/{repo}", repo_body)
        if st >= 400:
            die(f"updating repo settings failed ({st}): {resp.get('message')}")
    print("   done.\n")

    # --- verification ---
    if not args.dry_run:
        print("==> Verification:")
        _, scc = api("GET", f"/repos/{repo}/branches/{branch}/protection/required_status_checks")
        if isinstance(scc, dict):
            print(f"    strict={scc.get('strict')}  contexts={scc.get('contexts')}")
        _, r = api("GET", f"/repos/{repo}")
        if isinstance(r, dict):
            print(f"    allow_auto_merge={r.get('allow_auto_merge')}  "
                  f"delete_branch_on_merge={r.get('delete_branch_on_merge')}")
        print()

    print(f"All done for {repo}.")


if __name__ == "__main__":
    main()
