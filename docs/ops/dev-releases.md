# Development releases

Code you want running on a staging host **before** it is ready for
`master`, cut and deployed without a CI run per iteration.

---

## 0. The rule, in one line

> **CI fires only for `master`. Anything else is invisible to it — and
> that is a property of `.github/workflows/ci.yml`, not a convention you
> can rely on by habit.**

```yaml
on:
  pull_request:
    branches: [master]
  push:
    branches: [master]
```

Two consequences worth stating separately, because people get the second
one wrong:

1. **A push to any other branch runs nothing.** No `dev/**` exclusion is
   needed; the trigger is an allowlist, not a denylist.
2. **A pull request *into* `master` runs CI the moment it is opened** —
   before anybody merges it. So a dev branch stays invisible right up
   until you open that PR, and opening one "just to look at the diff"
   starts a full build.

There is **no tag trigger**, so tagging is free.

## 1. Naming

| what | form | example |
|---|---|---|
| branch | `dev/<topic>` or `staging/<topic>` | `dev/zone-modal` |
| tag | `v<version>-dev.<slug>.<n>` | `v0.1.0-dev.zone-modal.3` |

`scripts/dev-release.sh` **refuses** any other branch name. Not because
the name is magic — CI would ignore `wip-thing` just as thoroughly — but
because a name that states its own status is the thing a second person
reads six weeks later, and "is this branch safe to push?" should be
answerable without opening a workflow file.

The tag is per-branch numbered, so two dev branches do not renumber each
other, and it sorts under the release it precedes.

### Never name a branch `master`

Obvious, and it is the only naming mistake that actually costs anything:
a local branch called `master` pushed to the remote **is** the release
boundary, and CI runs against it as the last check before a release
without any of the gate having run.

## 2. Cutting one

```bash
git switch -c dev/zone-modal
# … work, commit …
scripts/dev-release.sh --dry-run     # shows the tag it would make
scripts/dev-release.sh
```

It refuses, in this order, and each refusal is a thing that has cost
somebody an afternoon somewhere:

1. **On `master`.** A `-dev` tag on the release boundary says the
   opposite of what is true.
2. **The workflow no longer proves CI is master-only.** The script reads
   `ci.yml` and checks for a tag trigger and for anything other than
   `branches: [master]`. It does *not* trust a remembered rule: the rule
   lives in that file, and a guard carrying its own copy of a policy
   stops matching the day the policy moves.
3. **Branch is not `dev/*` or `staging/*`.** See above.
4. **Dirty working tree.** A tag that names a commit which is not what
   got deployed is worse than no tag: it is a wrong answer to "what is
   running?", and it looks authoritative.

Tags are **GPG-signed**, like every commit in this repo. `git tag -s`
fails loudly without the hardware token, which is correct — an unsigned
dev tag is indistinguishable from one anybody could have made.

## 3. Deploying one

```bash
scripts/deploy-dev.sh <ssh-host> dist/v0.1.0-dev.zone-modal.3.bundle
```

### Why a bundle and not a push

**The deploy hosts have no credentials for the remote.** `git fetch`
there fails with *"could not read from remote repository"*. A bundle is
one file that `git fetch` accepts as if it were a remote, so the whole
transfer needs nothing but `ssh` and `scp` — no deploy key, no token on
the host, nothing to rotate.

The tag is read **out of the bundle**, not parsed from the filename, so
a renamed file cannot check out the wrong commit and report success.

### What it verifies, and why that specific check

It compares the **served** bundle against the **image it was built
from**, by digest.

Reporting the git revision proves the checkout moved. It does not prove
the containers did — `up -d --build` has been observed here building a
new image, tagging it, and leaving the containers on the previous one.
That failure is silent, and it presents as "my change did not deploy"
after you have already convinced yourself it did.

## 4. Getting back to `master`

A dev release is not a release. When the work is ready:

```bash
git switch -c <milestone-or-feature-branch> dev/zone-modal
# open the PR into master — this is the point CI runs
```

Dev tags are **not** deleted. They are the record of what was on staging
when, which is the question you will actually be asked ("was the TSIG
change on staging on Tuesday?"). They are cheap and they are evidence.

## 5. Two things that are easy to get wrong here

**`.env` shadows the compose default.** `compose.yaml` pins
`ATRIUM_IMAGE` with a `${VAR:-default}`, so a stale value in a host's
`.env` silently overrides a bump in the repo. At the time of writing the
repo default is `0.29.0` and a local `.env` still pinned `0.28` — the
stack was running the old base image while the file said otherwise.
**Check the host's `.env` after any atrium bump**, not just the compose
file.

**The local gate, not CI, is the quality bar for a dev release.** That
is the same rule `docs/ops/overnight-template.md` states for per-issue
work, and it is not a loophole: the gate is a superset of this workflow
— same typecheck, same vitest, same backend suite, plus a smoke test
against a stack it raises itself. Skipping CI is not skipping the
checks. Run `make check-fresh` and the suites before you cut.
