# reference/

`configure_branch_protection.py` — the one-time script that has been adding the
`Build Validation` required check and auto-merge by hand, run from a laptop
against four repos. It lived in no repository at all until this copy, which is
itself part of what CD-5795 is meant to fix.

It writes required status checks into *classic* branch protection. Since
CD-5916, `create_repo.py` deletes classic protection after applying its
ruleset — so anything this script configured is removed on the next sync run.
That is the conflict `apply_policy.py` is designed to end.

Kept for reference only. Do not run it.
