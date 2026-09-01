# Overnight contract

The binding rules. ~90 lines, read in full before starting.

The incidents that produced them are in `overnight-ledger.md` — **not
required reading**. Cite it when a rule looks like fussiness; do not read
it front to back. It is an archive, and it grows every run.

---

## Project card

| | |
|---|---|
| trunk | `master` |
| milestone branch | milestone title, lowercased, dashes |
| board | Projects v2 **2**, Status = Todo / In Progress / Done |
| gate | `make gate` |
| e2e | `make test-e2e` — **64s, 25 specs**, run once at the milestone |
| deploy | `scripts/deploy-verify.sh`, orchestrator only, never an agent |
| migrations | `backend/alembic/versions/`, single head, one author at a time |
| signing | `scripts/overnight-commit.sh`, never bare `git commit` |

## The gate

`make gate` runs **only what the diff can reach**, and touches no docker:

- `frontend/` → typecheck + vitest
- `tests/`, `scripts/*.py` → service-free `pytest tests/`
- `backend/` → **no docker-free suite exists.** It says so rather than
  running an unrelated one.
- nothing else → runs nothing, and says that is the result

`make test-backend` is a **functional** suite: 933 tests, real MySQL, ten
workers sharing one database. Its shared state is what generates this
project's races. It is not the gate. Run it deliberately.

## Testing: the default is NO new test

**Write a test only when one of these is true:**

1. The change is a **guard** — it exists to prevent a defect class.
2. It fixes a bug **no existing test caught** and that would recur
   **silently**.
3. The behaviour cannot be seen by e2e or by looking at the app.

Otherwise: make the change, let typecheck and the existing suite catch
regressions, and let e2e catch it at the milestone. Moving a control,
removing an icon, changing copy, adjusting a layout — **no new test.**

**When you do write one**, it gets a mutation proof. When you do not, you
owe nothing: no baseline measurement, no two-instrument reading, no sweep.

Rationale, measured on V2M9: six agents averaged 16 minutes and 160,000
tokens per issue on changes averaging 15 lines. The e2e suite that would
have caught any of them costs 64 seconds, once.

## Evidence, scaled to risk

The orchestrator sets a tier on the issue. An agent may argue it **up**,
never down.

| tier | example | owed |
|---|---|---|
| **cosmetic** | move a control, copy, layout | the diff + `make gate` |
| **behavioural** | refusals, auth, wire responses | + a test, shown failing |
| **guard** | protects a defect class | + mutation proof, + two instruments on any number that matters |

Only guard-tier work needs the ledger's rigour. Most work is not
guard-tier.

## Rules that always bind

- **One agent, one worktree, one environment.** A shared checkout means an
  agent tests code it did not write and gets a green suite for it.
- **Never `git add -A`.** Stage the files the issue owns.
- **Never touch the deploy host.** Merge and hold.
- **Push before you stop.** Unpushed worktree state is the only thing a
  shutdown cannot recover.
- **Correct the issue if it is wrong.** Overturning the premise is a
  result. This has been the most valuable output of most runs.
- **`n/a` is never `0`.** Not-measured, measured-as-zero, refused and
  never-ran are four states.
- **Do not assert on a report.** Assert on the thing the report describes.
- **Never wait in an unbounded loop.** Bound it and fail loudly.
- **No image builds.** `make up` carries `--no-build`. Building is
  `make build` / `make dev-up` / `make e2e-up`, by someone who meant it.

## Stop and ask

Only these. Everything else: write it down and carry on.

- an AC is ambiguous or contradicts the design
- the work needs a file outside the issue's scope
- a test cannot run without mutating production
- the deploy credential is refused

## Close-out

Demonstrate the exit criterion; do not count closed issues. Run e2e once,
here. Do not tune a demonstration until it passes — an honest "exit 2 with
named numbers" is the better result.
