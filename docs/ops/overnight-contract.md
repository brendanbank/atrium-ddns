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

## Testing

**Write the unit test.** They are fast, they are the guard, and they are
not what makes this expensive. A test for a moved control or a new
`disabled` prop is a few minutes and it is worth it.

**The budget rule: proving the test should not cost more than writing the
change.** If it does, you are doing ceremony, not testing.

What is expensive is the apparatus that grew around the tests, and by
default you owe **none** of it:

| not owed by default | what it costs |
|---|---|
| measuring suite totals before and after | a full run each side, and "210 → 216" tells nobody anything |
| a second instrument on those totals | another full run |
| mutation proof — revert the fix, watch it redden, restore | several runs, plus a rebuild if the backend is involved |
| a negative-result sweep of related code | an open-ended read of the tree |
| `merge-tip-check.sh` on a frontend-only diff | raises a stack to run tests that cannot see the change |
| a docblock table of the pass/fail split | prose |
| a PR body of more than a screen | prose |

**Assert on the new behaviour, not on the suite's total.** `247 passed` is
not evidence about your change; the three tests you added are.

Escalate to the apparatus above when the change is **guard-tier** — see the
table below. That is where a test that cannot fail is genuinely dangerous,
because it will be believed.

## Evidence, scaled to risk

The orchestrator sets a tier on the issue. An agent may argue it **up**,
never down.

| tier | example | owed |
|---|---|---|
| **cosmetic** | move a control, copy, layout | the diff + a unit test + `make gate` |
| **behavioural** | refusals, auth, wire responses | + the test shown failing once |
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
