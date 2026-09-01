# Every behaviour reported broken by hand, and whether a test would catch it again

**The negative result, up front.**

> We looked, and there are **exactly 21** behaviours in this repository's
> record that a human reported broken while using the product — **17** on the
> strict reading, and the difference between the two numbers is itself a
> finding (§2). Of the 21, **10** had a regression test before this sweep (one
> of them only by accident, §5), **3** are `n/a` at unit level with the reason
> named against each (§4), and the remaining **8 now do**. On the strict
> reading of 17: **7** were covered, **3** are `n/a`, and **7** now are.

Owned by **#133**, of milestone **V2M8 — the polish pass**, whose first exit
clause reads *"**every** behaviour a user has already reported broken once is
covered by a test that fails when it regresses, shown failing."* #128 owned one
instance of that clause. Nothing owned the word *every*; this file is the
enumeration that closes it.

Written 2026-09-01 against `v2m8-the-polish-pass` at `09ecafc`.

---

## 1. What counts, and why the boundary is drawn here

A behaviour is in the population if **a person reported it broken while using
the product** — not if a suite found it, not if a static reading found it, not
if it was designed better on second thought. That is the clause's own wording
(*"a user has already reported broken"*), and it is the property that makes
these defects worth a regression test in the first place: every one of them was
invisible to a green suite, and every one was found by clicking.

Three things are therefore **out**, and are named here so the next reader does
not have to re-decide them:

* **#69** — *a tenant cannot create a hostname* — was found by #47, the issue
  whose only job was to demonstrate a milestone exit criterion. That is a
  demonstration finding a gap, which is the demonstration working. Nobody
  reported it.
* **#122 / #125** — *the host tree unmounts mid-interaction* — was found by
  #113's agent counting spec failures (`3 of 6 runs red`), i.e. by the suite.
  The underlying fault is user-visible and the *symptom* was reported once in
  another form (#12 in the table below), which is why the family appears; the
  spec failure itself is not a hand report.
* **#137** — *`make up` reports success while the api crash-loops* — was found
  by #129's agent setting up its own worktree. An agent is not a user, the
  surface is `make`, and it is unfixed and open. It belongs to the milestone's
  **second** clause, not this one.

## 2. Two readings of the population, and why both are reported

The population size is a number that matters, so it is measured twice, by two
differently-shaped instruments.

| reading | rule | N |
|---|---|---|
| **A — strict** | only what a human *reported*: the three operator sessions' own report lists, plus #128's quoted complaint | **17** |
| **B — wide** | #133's own boundary: everything found *during* those sessions, including defects the author hit while fixing the reported ones | **21** |

The two disagree by exactly **4**, and the four are listed as items 17–20 below.
They are not noise: they are the defects a human found *by fixing*, and #133's
own issue body already includes one of them (the empty state, item 16 in PR
#127's telling but listed by #127 itself under *"Two traps found while
collapsing those"* rather than under its symptom table). So the issue's stated
boundary is already the wide one. **Reading B is used for the headline count**
because it is the less flattering of the two and because it is the one the issue
asked for; reading A is reported beside it because it is the one the exit clause
literally says.

Neither reading was authored here. Both are transcriptions of lists written by
someone else, at the time, for another purpose — which is the whole method (§7).

## 3. The enumeration

Provenance is a citation, not a recollection. `§` references are
`docs/ops/ui-design.md`.

### Session 1 — the first operator session with the deployed UI (V1M5)

Five complaints, enumerated by the operator in `§8`'s own table.

| # | behaviour reported | remedy | covered before | covered now |
|---|---|---|---|---|
| 1 | no provider can be chosen when creating a zone; a zone that publishes nowhere answers `911` and looks fine | #88 | **yes** — `DomainsPage.test.tsx`, 6 tests | — |
| 2 | a device's name cannot be edited | #89 | **no** — the tests existed and #127 deleted them (§5) | `deviceCard.test.tsx` |
| 3 | a name must be typed as a full FQDN although its zone was just selected | #90 | **no** — same, and worse (§5) | `hostnameSuffix.test.ts` |
| 4 | the device list does not scale | #89's detail route | **n/a** (§4) | — |
| 5 | the zone list does not scale | #88's detail route | **n/a** (§4) | — |

### Session 2 — the second operator session (V1M6)

*"I still cannot edit the zone"*, twice, from screenshots — `§16`'s three causes
behind one appearance — plus *"also here make sure you create a modal that pops
up"*, which `§17` records as overruling `§12`.

| # | behaviour reported | remedy | covered before | covered now |
|---|---|---|---|---|
| 6 | the board's device name is not a link at all | #97 | **yes** — `affordance.test.tsx`, 2 tests | — |
| 7 | the zones list's name reads as inert — `.ddns-data` cancels the link | #97 | **yes** — `affordance.test.tsx` + `design.test.ts`, 4 tests | — |
| 8 | the devices list's name reads as inert, same cause | #97 | **n/a** (§4) | — |
| 9 | clicking a row must open a modal, not merely navigate | #97 | **yes** — `sharedCard.test.tsx`, 6 tests | — |

### Session 3 — the board refactor (PR #127, and PR #106 before it)

PR #127's own *Symptom → Cause* table is three rows; #133's issue body names six
defects from this session, four of which are not in that table. The union is
seven, plus four more from the *"Two traps"* and *"Also"* sections and from PR
#106's *"Two gaps the tests found"*.

| # | behaviour reported | covered before | covered now |
|---|---|---|---|
| 10 | *"Add a device"* navigated instead of opening a modal | **no** | `handReported.test.tsx` |
| 11 | finishing a create flow stranded you on a page with no nav entry | **yes** — `returnAddress.test.tsx` (4) + `main.test.tsx` | — |
| 12 | modals vanished mid-flight: a second registered route unmounts the host root under a portal | **yes** — `main.test.tsx` (2) + `eventInUpdater.test.ts` (4) | — |
| 13 | rotation printed the credential inline instead of in the once-only modal | **no** | `deviceCard.test.tsx` |
| 14 | the device card had a Save per field | **no** | `deviceCard.test.tsx` |
| 15 | `hostname: 1` shown where a name belonged | **no** | `handReported.test.tsx` |
| 16 | the empty-state message shown for a filter that matched nothing | **no** | `handReported.test.tsx` |
| 17 | an untouched, prefilled rate-limit box would pin a device that was correctly inheriting | **no** | `deviceCard.test.tsx` |
| 18 | a lookup answering `{}` threw inside `.map` during render and took the whole page down | **yes, by accident** (§5) | `handReported.test.tsx` |
| 19 | a device with no names vanished from the board | **yes**, incidentally — `DeviceBoardPage.test.tsx` | — |
| 20 | the board's legend went missing; its numbers must come from the payload | **yes** — `DeviceBoardPage.test.tsx` (2) | — |

**Item 12 is not in #133's list.** It is row 3 of PR #127's own symptom table
and #133 transcribed rows 1 and 2 and stopped. It turns out to be covered
anyway, so nothing was lost — but a list that drops a row it was copying is the
argument for sweeping the record rather than the summary of it.

### Session 4 — the add-name preselect

| # | behaviour reported | covered before | covered now |
|---|---|---|---|
| 21 | *"When I click the add icon after the name it does not autofill the device in the name card."* | **yes** — `boardAffordance.test.tsx` (3), **#128** | — |

## 4. The three `n/a`s, named rather than dropped

`n/a` is never `0`. Each of these is a behaviour that was genuinely reported and
genuinely has no unit-level test, for a reason that is about the behaviour and
not about the effort.

**4 and 5 — *"the device list does not scale"*, *"the zone list does not
scale"*.** *Not testable at unit level as reported, and the surface they were
reported against no longer exists.* Both are judgements about a quantity
(fifteen rows are unreadable, one row is fine) with no threshold anyone has
committed to, so any assertion would be inventing the number it then checks —
the probe-that-cannot-fail family with a magic constant standing in for a
verdict. Their #88/#89 remedy was a detail route per object; **#127 deleted both
routes** and replaced them with the board's Device / Name / Zone filters. Those
filters *are* testable and item 16's tests now exercise them, but that is the
successor behaviour, not the reported one, and it is recorded as such rather
than counted as cover for a complaint about scale.

**8 — *"the devices list's name reads as inert"*.** *The surface was deleted;
the rule that fixes it survives and is tested.* `tenant/DeviceList.tsx` is gone
with `/atrium-ddns/devices` (#127). The cause was `.ddns-data` cancelling
Mantine's link colour, and the remedy was a stylesheet rule — *an interactive
`.ddns-data` carries an affordance that is not colour, at rest* — which
`design.test.ts` asserts in four tests against `ddns.css` directly. So the rule
cannot regress; the third instance of it has no surface to regress on.

## 5. Two findings the sweep produced that the issue did not predict

### 5.1 #127 deleted the regression tests for two hand-reported behaviours along with the pages they lived on

This is the largest single result here, and it is the reason a sweep was worth
running at all.

`git log --diff-filter=D --name-only -- frontend/src/test/` names three files
deleted at `278bb19` (PR #127) and two at `1a75107` (PR #106):

| deleted file | what it was guarding |
|---|---|
| `HostnamesPage.test.tsx` | *the zone is a suffix, not a retype* — item 3's ten-case table, its vacuity guard, the preview-equals-request assertion, **and the no-second-validator sweep** |
| `DeviceDetailPage.test.tsx` | *the name is editable, in place*; *the conflict is surfaced, not avoided*; *the rate limit keeps its third state*; *rotation is its own operation* — items 2, 14 and 17 |
| `DevicesPage.test.tsx`, `HostnamePublishing.test.tsx`, `ZoneDetailPage.test.tsx` | list and publishing surfaces that also went |

The deletions were correct *as deletions* — the pages went, so page tests for
them had nothing left to assert. What did not go is the **behaviour**:
`composeHostname` / `decomposeHostname` were extracted into
`tenant/hostnameName.ts` and are still called by `NameModal`; rename, one Save,
the rate-limit third state and rotation all moved into `tenant/DeviceCard.tsx`.
Every one of those is exercised by a user today and none of them had a test
between `278bb19` and this commit.

**And the strongest form of it.** `hostnameName.ts`'s own docstring says the
compose/decompose pair is *"the thing the suite sweeps for"* and names
`HostnamesPage.test.tsx` twice as the file that does the sweeping. That file has
not existed since `278bb19`. A comment asserting the existence of a guard that
was deleted in the same commit is the probe-that-cannot-fail family aimed at a
reader instead of at a metric: anyone checking whether the single-validator rule
is protected finds a sentence saying it is. The sweep has been restored in
`hostnameSuffix.test.ts`, now pointing at `hostnameName.ts`, and it fails both
when a second copy grows and when the one home is moved or deleted — the second
half being what actually happened.

### 5.2 Item 18 was covered, by borrowing rather than by owning

Removing `Array.isArray` from `logs/LogFilters.tsx` turns **26** tests red — and
**24 of them are pre-existing tests in `LogSearchPage.test.tsx`**, which was not
written for this at all. That file's catch-all `fetch` stub answers `{}` for
every non-events URL, so the whole file has been running against the exact shape
that used to crash the page.

That is real coverage and it is honestly reported as such: the behaviour *would*
be caught if it regressed today, so the exit clause was already met for item 18.
It is also *borrowed* — it holds only while that stub keeps returning `{}`, and a
maintainer tidying it to `[]`, the realistic shape, would delete the coverage
without touching a test name or a line of source. `handReported.test.tsx` now
states the property with a fixture chosen for it, so the guard's subject is
owned.

## 6. A defect the sweep found while writing the tests for item 16

**The board's `clear` control does not clear a zone filter.**
`board/BoardTable.tsx` computes `filtered` from all three filters but its
`clear` handler sets only `deviceFilter` and `nameFilter` to `null`. The zone
filter is seeded from `?zone=`, which is exactly how the zones list links here
(`namesHrefForZone`). So a tenant who clicks through from a zone with no names
lands on *"No row matches that filter. N in total — clear the filter to see
them."*, presses **clear**, and nothing happens. The instruction the empty-state
fix exists to give is the one instruction the surface cannot follow.

Measured, not read: a throwaway spec that opens `/atrium-ddns?zone=example.org`
against a board whose only name is in `example.net`, asserts `board-no-match`,
clicks `board-filter-clear` and asserts it is gone, fails —
`expected <span class="ddns-note"> to be null`. The spec was not committed, because
committing a red test is not a finding, it is a broken gate.

Out of #133's scope (`board/BoardTable.tsx` is a source file and this issue owns
`docs/ops/` and `frontend/src/test/`), so it is filed rather than fixed. The
component's own comment calls them *"Two view filters"* where there are three,
which is probably how the third was missed.

## 7. Method, for the next person who has to do this

Everything above came from lists **someone else wrote, at the time, for another
purpose**: `ui-design.md` §8's five-row table, §16's three-row table, PR #127's
symptom table and its *"Two traps"* section, PR #106's *"Two gaps"*, and #128's
verbatim quotation of the operator. Not one of them was compiled by reading the
code and asking *what looks untested* — a candidate list written by the same
person as the conclusion shares an author with it, and agreement between them
proves little. This is the template's own #76 lesson: sweeping 41 real checkouts
found six paths no synthetic list had suggested.

Two instruments were used on the coverage half of the question, and they were
deliberately differently shaped:

* **By name** — `vitest list`, which enumerates what collection finds, walked
  against the enumeration above.
* **By reachability** — every `data-testid` in `frontend/src/` compared against
  every id any test queries. Before this issue: **180 in the source, 59 queried
  by a test, 121 never queried by anything.** That set is where items 2, 3, 10,
  13, 14, 15 and 16 were all sitting, and it named them without anyone having to
  guess which behaviours were interesting. After: **76 queried, 104 not** — the
  17 that moved are the affordances the new tests drive.

  The remaining 104 are **not** a to-do list and must not be read as one. Plenty
  are reachable through a role or a string instead, plenty belong to behaviours
  nobody has reported broken, and an issue that set out to drive the number to
  zero would be optimising an instrument rather than using it. Its value is
  exactly what it was used for here: turning "which of these behaviours has no
  guard" from a judgement into a set difference.

The second instrument is the one that found things, and it is worth keeping: an
id rendered by shipped code and asked for by no test is a cheap, mechanical,
un-authored candidate list.
