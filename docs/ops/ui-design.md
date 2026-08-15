# atrium-ddns — the design pass

The written plan the V1M3 implementation issues build against, so that four
issues do not invent look-and-feel four different ways. **No components live
here.** Every colour and type decision below is a value; every layout decision
that could have gone two ways names the measurement that decided it.

Written for issue #43. Scope: `docs/ops/ui-design.md` (this file) and the
reconciliation block appended to `docs/ops/refactor-plan.md` §4.

---

## 0. The subject, in one sentence

Self-hosted infrastructure for people who run their own DNS, whose single job
is to answer *which of my names no longer points where I think it does, and
which of my devices stopped talking.*

Everything below follows from one consequence of that sentence: **the product
exists for the exceptional row.** A surface that renders 500 healthy hostnames
and 3 broken ones with equal weight has spent its whole budget on the 500.

### The four measurements this design is built on

Design decisions that could have been taste are decided here by data instead.
Each is quoted with its instrument, because a number without one is an opinion
with a decimal point.

| # | Fact | Instrument A | Instrument B |
|---|---|---|---|
| M1 | Production is IPv6-first | plan §3.1.1/§3.3.1, live legacy DB, 2026-08-15: **305 of 448** update events carried a v6 address (68.1%) | `dyndns-route53/instance/events.db`, a March-2026 snapshot of the same estate: **232 of 238** (97.5%) |
| M2 | Real IPv6 addresses are the *widest* form IPv6 has | same March snapshot, length histogram: 36 ch × 8, 37 × 16, 38 × 50, **39 × 158** | colon count on the same rows: **7 colons in 232 of 232** — i.e. eight groups, never `::`-compressed |
| M3 | Nothing-changed is the overwhelming normal | same snapshot, `response`: **225 `nochg` / 13 `good`** (94.5% no-op) | plan §3.3.1: 448 events in 24 h from **3 of 6** device rows — half the fleet produced nothing at all |
| M4 | Mantine's own tokens cannot express the accent this design needs | computed WCAG ratio, `--mantine-color-orange-9` `#d9480f` on white: **4.30:1** — the darkest step in the scale, and still short of 4.5 | the same computation on `--mantine-color-gray-6` `#868e96`, Mantine's `dimmed`: **3.32:1** |

Two of these disagree with each other and are reported anyway.

**M1's two instruments disagree by 29 points** — 68.1% against 97.5%. They are
five months apart, each a single day's window on the same estate, and neither
is a population. They agree on the only thing this design needs them to agree
on (v6 is the majority case, not the edge case) and they should not be averaged.
The design is sized against the *higher* figure, because a layout that is right
at 97.5% v6 is also right at 68%, and the reverse is not true.

**M2's two instruments agree exactly** — 36/8, 37/16, 38/50, 39/158 in both
readings — and the agreement is worth almost nothing on its own, because both
queries read the same file. The reading that carries weight is the second one:
*seven colons on every single row*. That is a different question asked of the
same data, and it is the one that settles the width budget. `2001:db8::1` is
eleven characters and is what every test fixture in this repo uses; the real
data is thirty-nine and never compressed.

**Retracted, do not reuse.** "10 of 11 hostnames track an IPv6 address" was
read from `hostnames.last_ip_v6`, a column the legacy service seeds from a
boot-time zone lookup — it measures AAAA records in the zone, not client
traffic. Superseded by M1 in commit `058a0a0`. It survives in two docstrings
(`tests/compat/legacy_behaviour/test_legacy_behaviour.py:341`,
`model_cases.yaml:887`) that were never updated; the rebuttal is at
`model_cases.yaml:722`.

---

## 1. Palette

**Six named values.** Four are aliases onto atrium's stable `--mantine-*`
tokens so that a brand change moves them; two are host-owned literals, and
§1.3 is the argument for why those two must not be tokens.

The switch is `[data-mantine-color-scheme]` on `<html>` — never a media query,
because atrium can force a scheme per preset (`colorSchemeForPreset` returns
`'dark'` for the `dark-glass` preset and `'auto'` for the rest, and `'auto'`
is resolved by Mantine, not by CSS).

```css
/* Host-namespaced. theme.md permits a host to invent its own contract
   under its own prefix; it forbids a host shipping raw-CSS overrides
   for atrium's tokens. Nothing below writes a --mantine-* name. */
[data-ddns-root] {
  --ddns-ink:           light-dark(var(--mantine-color-gray-9),  var(--mantine-color-dark-0));
  --ddns-quiet:         light-dark(var(--mantine-color-gray-7),  var(--mantine-color-dark-1));
  --ddns-rule:          light-dark(var(--mantine-color-gray-6),  var(--mantine-color-dark-2));
  --ddns-edge:          light-dark(var(--mantine-color-gray-3),  var(--mantine-color-dark-4));
  --ddns-diverge:       light-dark(#B4500A, #F59042);
  --ddns-diverge-wash:  color-mix(in srgb, var(--ddns-diverge) 8%, transparent);
}
```

### 1.1 Resolved values and measured contrast

| token | light value | on `#ffffff` | dark value | on `#242424` | minimum it must clear |
|---|---|---|---|---|---|
| `--ddns-ink` | `#212529` gray-9 | **15.43:1** | `#C9C9C9` dark-0 | **9.37:1** | 4.5:1 (body text) |
| `--ddns-quiet` | `#495057` gray-7 | **8.18:1** | `#b8b8b8` dark-1 | **7.83:1** | 4.5:1 (label text) |
| `--ddns-rule` | `#868e96` gray-6 | **3.32:1** | `#828282` dark-2 | **4.04:1** | 3:1 (WCAG 1.4.11 — see §1.2) |
| `--ddns-edge` | `#dee2e6` gray-3 | 1.30:1 | `#424242` dark-4 | 1.54:1 | none — decorative only |
| `--ddns-diverge` | `#B4500A` | **5.13:1** | `#F59042` | **6.62:1** | 4.5:1 |
| `--ddns-diverge-wash` | `#f9f1eb` composite | ink on it **13.82:1**, accent on it **4.60:1** | `#352d26` composite | ink **8.16:1**, accent **5.76:1** | 4.5:1 for both |

Worst-case surfaces, not just the body background: `--ddns-diverge` holds
4.61:1 on `--mantine-color-gray-1` (`#f1f3f5`, the lightest surface a Paper
lands on in light) and 4.77:1 on `--mantine-color-dark-5` (`#3b3b3b`, the
lightest in dark). Every reading in this section is computed, not eyeballed;
the arithmetic is WCAG 2.x relative luminance.

### 1.2 Three rules, and each one is load-bearing

**Rule 1 — agreement has no colour.** There is no green in this design. When
the authoritative nameserver answers what we published, the strip is set in
`--ddns-ink` on the page background and its rail is a plain hairline. Nothing
about a healthy row is tinted, badged or filled.

The reason is M3. If the normal state is painted, 94.5% of the surface is
painted, and the 5.5% that matters is competing with it. This is the design's
named risk; §6 argues it properly and states what it costs.

**Rule 2 — `--ddns-diverge` appears nowhere except on a measured
disagreement.** Not on a button, not on a heading, not on a nav item, not on a
focus ring, not on a link. Interactive chrome belongs to
`--mantine-primary-color-*`, which the operator owns and can change. The
boundary is the whole idea:

> **atrium's primary colour means *you can do this*. `--ddns-diverge` means
> *this is true and it is wrong*. Nothing is ever both.**

A user learns one rule — *orange is a fact about DNS, not a control* — and the
rule stays true across every screen in the bundle.

**Rule 3 — colour is never the only channel, and the dark scheme proves why.**
In light, the accent and the ink differ in luminance by 3.01:1, so a greyscale
render still separates them. In dark they differ by **1.42:1** — computed —
which means a dark-scheme screenshot converted to greyscale shows a diverged
address and an agreeing address as *the same tone*. So every state carries a
redundant non-colour channel: the rail's stroke style, a text glyph, and a word.
Colour is the fastest channel, never the only one.

That measurement is also why `--ddns-rule` is gray-6 and not gray-3. The
instinct is a very light hairline. But the rail **carries state** — it is the
difference between "these two agree" and "these two do not — and it is
therefore a graphical object under WCAG 1.4.11 and needs 3:1 against its
background. gray-3 is 1.30:1: a rule that means something, drawn so faintly
that the standard treats it as absent. `--ddns-edge` is kept at gray-3 for
separators that mean nothing, and using it for a rail is a defect.

### 1.3 Why the accent is a literal and not a token

This is a deliberate deviation from "express it against `--mantine-*`", and it
was arrived at by measurement rather than preference. Both halves of the
argument are needed:

1. **The scale cannot reach the ratio (M4).** `--mantine-color-orange-9`
   `#d9480f` is the darkest orange Mantine ships and is **4.30:1** on white.
   Accent text at 14px needs 4.5:1. There is no token in the scale that
   qualifies, so "use the token" is not on the table for text — only for the
   rail, which needs 3:1. Splitting the accent across a token for the rail and
   a literal for the text would produce two oranges, which is worse.
2. **A status colour must not move when someone rebrands.** `--mantine-primary-color-*`
   is exactly the token that changes with `BrandConfig`, and a colour whose
   meaning is "this name is wrong" cannot be the same colour that changed
   because an operator preferred cyan. A learned signal that is re-themable is
   not a signal.

`docs/theme.md` permits this precisely: *"If a host bundle depends on a
hand-rolled `--atrium-…` variable it has either invented its own naming
convention (fine, but it's the host's contract) or imported a value that
doesn't actually exist."* We invent `--ddns-*`, own it, and write nothing to
atrium's namespace.

**The residual collision, named.** No shipped preset uses orange as its primary
(`default` → blue, `classic` → teal, `dark-glass` → cyan), but the Branding
admin exposes `primaryColor` freely and an operator may pick `orange`. In that
installation the divergence accent and the button fill are the same hue. The
mitigation is Rule 3 rather than a hue shift: the accent's *form* — a 2px rail
segment, a `≠` glyph, and an underlined address group — is not something a
button ever does. A design that changed its status colour to dodge a brand
collision would have made the status colour un-learnable to avoid making it
briefly ambiguous, which is the worse trade.

### 1.4 Where the hue comes from

Orange is pair 2 of T568A/B, the second strand of the TIA-598 fibre order, and
the colour every person in this audience has stripped with a knife. It is the
one palette this subject actually has. It also happens to be the safe half of
the blue/orange axis under both deuteranopia and protanopia — so if a second
chromatic is ever genuinely needed, it is already chosen and it is blue
(`--mantine-color-blue-8` / `-4`, 5.02:1 / 6.27:1). Nothing in this design
needs it yet, and adding it would break Rule 2.

---

## 2. Type

### 2.1 Three roles, stated as values

```css
[data-ddns-root] {
  --ddns-font-data:  ui-monospace, "SF Mono", SFMono-Regular, "Cascadia Mono",
                     Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono",
                     monospace;
  --ddns-font-body:  var(--mantine-font-family);
  --ddns-font-head:  var(--mantine-font-family-headings);
}
[data-ddns-root] .ddns-data {
  font-family: var(--ddns-font-data);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "zero" 1, "ss01" 1;   /* slashed zero where offered */
  font-size: var(--mantine-font-size-sm);      /* 14px */
  line-height: var(--mantine-line-height-sm);  /* 1.45 */
}
[data-ddns-root] .ddns-label {
  font-family: var(--ddns-font-data);
  font-size: var(--mantine-font-size-xs);      /* 12px */
  line-height: var(--mantine-line-height-xs);  /* 1.4 */
  color: var(--ddns-quiet);
}
```

**Data face — `--ddns-font-data`, and it leads.** This is
`--mantine-font-family-monospace`'s stack with `Courier New` removed and four
better candidates inserted ahead of the generic fallback. Courier New is
removed on purpose: it is markedly lighter in stroke than everything else in
the stack, so a column of addresses set in it reads as *disabled text* — which
is the one thing a status board must never accidentally say. On a machine where
Courier New is the only hit, the design silently claims every value is inactive.

**Body face — `var(--mantine-font-family)`.** Atrium's brand body font,
inherited, unmodified. Prose, empty states, help text, control labels. The
operator's branding still shows through the host's fragments, which is the
point of inheriting it.

**Heading face — `var(--mantine-font-family-headings)`.** Likewise inherited.
A host fragment owns at most one heading, at h3/h4 scale, inside a shell that
already has a header and a sidebar.

### 2.2 The boundary rule, because "mono everywhere" is a cliché

Setting an entire developer-facing product in a monospace face is a genre
default, and it makes prose hard to read for the sake of an atmosphere. The
rule that keeps this honest:

> **If you would paste it into a `dig` command, it is `--ddns-font-data`. If
> you would say it out loud, it is `--ddns-font-body`.**

Addresses, hostnames, response codes, timestamps, record types, error strings
from a resolver: data face. Sentences, buttons, headings, tooltips explaining
what a thing means: body face. There is no third case, and a paragraph set in
mono is a review comment.

### 2.3 The type idea: data outranks its label

The largest type in the bundle is a value, not a heading. Addresses are 14px;
the words naming them are 12px. That inversion is the whole typographic
statement and it needs no third typeface to make it. There is no display face
in this design — §6 argues that against the issue's own wording.

### 2.4 Labels are zone-file comments

The strip's station labels and the board's column heads are lowercase, prefixed
with `;`, in the data face at 12px in `--ddns-quiet`:

```
; answered
; published
; called from
```

`;` begins a comment in DNS master-file format (RFC 1035 §5.1). To this
audience it reads instantly and unambiguously as *annotation, not data*, which
is exactly the job — and it does that without uppercase, without letter-spacing,
and without a rule underneath, all of which are the generic ways to make a label
recede.

**It appears in exactly two places** — the strip's three station labels and the
device board's column heads. Not on form fields, not on section titles, not on
empty states. One borrowed convention used twice is a motif; used everywhere it
is a costume.

### 2.5 The width budget, computed

Monospace advance width is ~0.6em across the whole stack (SF Mono 0.6, Menlo
0.6023, DejaVu Sans Mono 0.602, Consolas 0.55). At 14px that is **8.4px per
character**.

| what | characters | width |
|---|---|---|
| widest real IPv6 seen (M2) | 39 | 328px |
| `IPV6_LEN` column ceiling — v4-mapped + zone index (`models.py:70`) | 45 | **378px** |
| widest IPv4 | 15 | 126px |
| `; called from` label | 13 | 110px |

**Budget 380px for one address cell.** Not 200. This one number decides §3.

---

## 3. Layout

### 3.1 The resolution strip — two arrangements, and the one M2 kills

**Arrangement A — three columns.** The obvious reading of plan §4's "three
values side by side", and what anyone would sketch first.

```
  hostname                ; answered              ; published             ; called from
  ────────────────────────────────────────────────────────────────────────────────────────
  host-a.example.net      2001:0db8:0000:0000:…   2001:0db8:0000:0000:…   2001:0db8:0000:0000:…
  host-b.example.net      2001:0db8:0000:0000:…   2001:0db8:0000:0000:…   2001:0db8:0000:0000:…
```

Minimum width: 3 × 380 + 2 × 16 gutter + 200 hostname + 24 rail =
**≈1436px**. Atrium's shell is `<AppShell header={{height:60}} navbar={{width:240}}
padding="md">` (`atrium/frontend/src/components/AppLayout.tsx:126–129`), so a
1440px viewport gives 1440 − 240 − 2 × 16 = **1168px** of content, and a 1280px
laptop gives 1008px. Arrangement A does not fit on the machine the
operator is most likely using, and the failure mode is the worst available: a
wrapped IPv6 address is indistinguishable from a shorter one, because the wrap
falls at an arbitrary character and there is no visual mark for it. **A layout
whose overflow behaviour is "silently show a different address" is
disqualified**, not merely tight.

It also fails on a second count before the width even matters: a hostname can
carry both an A and an AAAA record, so the one-row-per-hostname promise the
column layout is sold on is already broken. In the March snapshot 2 of 10
configured hostnames had both families set.

**Arrangement B — three rows on a chain rail.** Chosen.

```
  host-a.example.net                                              AAAA
┌──────────────────────────────────────────────────────────────────────┐
│ │  ; answered      2001:0db8:0000:0000:0000:0000:0000:0001    14:02  │
│ │                                                                    │
│ │  ; published     2001:0db8:0000:0000:0000:0000:0000:0001    13:47  │
│ │                                                                    │
│ │  ; called from   2001:0db8:0000:0000:0000:0000:0000:0001    13:47  │
└──────────────────────────────────────────────────────────────────────┘
   ^
   the rail
```

Minimum width: 110 label + 380 address + 50 time + 16 rail + 3 × 12 gutter =
**≈592px**. Fits one-up on a phone in landscape, two-up at desktop width, and
does not wrap at any realistic viewport above 640px. Below that, §3.4.

### 3.2 The rail is the signature, and its *segments* are the datum

The three values are not peers. They stand in a causal order that runs upward
against time:

```
   the device called from  →  we published  →  the world answers
```

So the strip is a **chain of custody**, not a timeline — there is no time axis,
the vertical spacing carries no duration, and nothing may be added later that
implies one. What matters is not the three cells but the **two joints between
them**, and each joint is exactly one verdict:

| joint | compares | means, when it diverges |
|---|---|---|
| upper | `Hostname.dns_ip_*` vs `Hostname.last_ip_*` | *the zone does not carry what we wrote* — the provider write failed, is still propagating, or something else edited the zone |
| lower | `Hostname.last_ip_*` vs `Device.last_ip_*` | *the device has moved and the name has not followed* — we have not written its current address yet |

You read the joints. The cells are just addresses.

**Rail segment renderings** — one per joint, and the stroke style is the
redundant channel Rule 3 requires:

| verdict | rail segment | gutter glyph | address treatment |
|---|---|---|---|
| agreed | 1px solid `--ddns-rule` | *(none)* | `--ddns-ink`, plain |
| diverged | 2px solid `--ddns-diverge` | `≠` | lower cell in `--ddns-diverge`; the groups that differ get `text-decoration: underline; text-decoration-thickness: 2px` |
| not measured — never | 1px **dotted** `--ddns-rule` | `·` | see §4 |
| not measured — failed | 1px **dashed** `--ddns-rule` | `!` | see §4 |
| not applicable | **no segment drawn** | *(none)* | see §3.3 |

Absence of a rail segment is a statement: *no comparison was made, and none
should have been.* It is not the same as a comparison that came back equal
(solid hairline) and it is not the same as one we could not make (dotted or
dashed).

### 3.3 The lower joint is not always meaningful, and the plan's wording hides that

This corrects plan §4 rather than implementing it. §4 says the third value is
*"what the device last claimed"*. The schema does not hold that. `Device.last_ip_v4`
and `Device.last_ip_v6` hold **the address the device called *from***, and
`router_nic.py:670` says so in as many words: *"The addresses stored are the
ones the device called from, not the ones it asked to publish. `client_ip` and
`myip` are different facts and the difference is the interesting part of a
NAT'd update."*

For a device that sends no `myip` — the common case, and universal over IPv6
where there is no NAT — those are the same address and the lower joint means
exactly what §4 wanted it to mean. For a device behind IPv4 NAT that declares
`myip=` explicitly, they are *permanently and correctly different*, and a strip
that marks that as divergence shows a false positive on every render, forever.
That is the "probe that could not fail" defect wearing a UI's clothes: an
indicator that is always on cannot indicate.

**The rule, and it is buildable from columns that exist.** `DnsEvent` stores
both facts per event: `client_ip` (came from) and `ip` (was about). For the
most recent `event_type='update'`, `response_code IN ('good','nochg')` row for
this hostname and family:

- `event.client_ip == event.ip` → the device publishes its own call-from
  address. **The lower joint is evaluated.**
- `event.client_ip != event.ip` → the device declares its address explicitly.
  **The lower joint is `not applicable`**: no rail segment, and the
  `; called from` cell is labelled `; called from (declared myip)` so the
  reader knows *why* nothing is being compared rather than assuming a bug.
- no such event → **`not applicable`**, same rendering, label
  `; called from (no update on record)`.

**The lower joint is the most actionable signal in the product** and only this
column can produce it: a device whose call-from address has moved while the
hostname still publishes the old one is a name that is already wrong and has not
been noticed yet. It is worth the care it takes to not fire it spuriously.

### 3.4 One strip per (hostname, family), and collapse is the information architecture

A hostname produces up to **two** strips: one for `A`, one for `AAAA`. Only
families with at least one non-null value across the three stations are
rendered — a v6-only hostname shows one strip and reserves no space for the
other. The family badge sits top-right of the strip in the data face.

A device with four hostnames, both families, fully expanded, is 4 × 2 × 3 = 24
address lines. That is a wall. The collapse rule fixes it and does real work:

> **A strip whose every applicable joint is `agreed` collapses to one line. A
> strip with any other verdict is expanded and is not collapsed by any default.**

```
  host-a.example.net    AAAA   ; 2 of 2 agree  2001:0db8:0000:0000:0000:0000:0000:0001  14:02
```

The collapsed line **names its denominator**: `; 2 of 2 agree`, or
`; 1 of 2 agree, 1 n/a` when the lower joint is not applicable. `; agrees` on
its own would be a ratio with the divisor hidden, and the divisor here moves —
it is 2 for a device publishing its own address and 1 for one declaring `myip`.

Two consequences worth stating because they are the design working:

- **Page height becomes an instrument.** A tenant with nothing wrong has a
  short page. A tenant with three broken names has a page three strips long.
  You can tell how bad it is from the scrollbar.
- A collapsed strip is expandable by click and by keyboard, and expanding one
  is a local state, never a default. **Nothing may ship a "collapse all" that
  can hide a divergence**, because the collapsed state is defined as "agrees",
  and a control that lets a diverged strip render in the agreed shape makes the
  shape a lie.

### 3.5 Wrapping, for the viewport where 592px is not available

Below 640px the address cell must give. The rule is precise because the wrong
one produces a *readable and wrong* address:

- Break opportunities exist **only** immediately after `:` in an IPv6 literal
  and after `.` in an IPv4 literal — emit `<wbr>` at those points and set
  `overflow-wrap: normal`. Never `anywhere` or `break-all`, which split a
  hextet and make `…:0db8:` and `…:0d` `b8:` look alike.
- A wrapped continuation indents to the address column and the `:` stays at the
  end of the line it broke after, so the reader can see the join.
- Below 480px the labels move above their values instead of beside them; the
  rail stays, because the rail is the content.

### 3.6 The device board — two arrangements

The board answers *which device stopped talking*. §4 is right that this is the
single most useful thing the product can say and that the old UI could not say
it at all.

**Arrangement A — sortable table, one row per device, expand for hostnames.**
Familiar and dense. Rejected as the default not because it is ugly but because
it makes the answer the *user's* job: the board opens in whatever order the
table was last sorted, and "which one is quiet" is a sort the user has to know
to perform.

**Arrangement B — ledger, ordered by liveness, no sort control in the default
view.** Chosen. The ordering is the opinion:

```
;                                    ; last seen      ; updates / 7 d
────────────────────────────────────────────────────────────────────────
 !  garage-nas                         never            —
 !  roof-ap                            3 d 04 h ago     error
    office-router                      41 min ago       0
    home-router                        2 min ago        213
```

Order is fixed: `never_seen` → `last_call_failed` → `idle` → `active`, and
within a bucket, oldest `last_seen_at` first. A device that has gone quiet is
at the top of the page without anyone asking. Sorting by other columns is
available and is never the initial state.

The `!` marker sits in a 3-character gutter in `--ddns-diverge`. **Only
`never_seen` and `last_call_failed` are marked.** `idle` is not, and M3 is why:
half the fleet produced zero events in a 24-hour window, so marking idle would
paint half the board and destroy the marker. Idle is normal. It is rendered as
a measured `0`, which is a statement, not a silence.

Each device expands to its hostnames, each hostname to its strips. Three levels,
and the top two are one line each.

### 3.7 Spacing, radius, motion

All from Mantine's scale, so the operator's `scale` setting still works:

| use | token | value |
|---|---|---|
| gap between rail stations | `--mantine-spacing-xs` | 10px |
| padding inside a strip | `--mantine-spacing-md` | 16px |
| gap between strips | `--mantine-spacing-sm` | 12px |
| gap between device blocks | `--mantine-spacing-lg` | 20px |
| strip container radius | `--mantine-radius-sm` | 4px |
| rail radius | — | none. A rail is a line. |

**Motion: one transition, and nothing else.** `transition: border-color 120ms
ease` on the rail segment, so a state change arriving from a health-check poll
is perceptible rather than a jump cut. No page-load sequence, no scroll reveal,
no hover animation, no skeleton shimmer. An operations board that animates is a
board people stop trusting, and the restraint is the point of spending the
boldness on the rail. Wrapped in `@media (prefers-reduced-motion: reduce) { *
{ transition: none } }`.

---

## 4. The signature element, specified to build

Everything below is normative. Two people building from §4 alone should produce
the same thing.

### 4.1 The three values, by column

| station | value | timestamp shown beside it |
|---|---|---|
| `; answered` | `Hostname.dns_ip_v4` / `dns_ip_v6` | `Hostname.dns_checked_at` |
| `; published` | `Hostname.last_ip_v4` / `last_ip_v6` | `Hostname.last_updated_at` |
| `; called from` | `Device.last_ip_v4` / `last_ip_v6` (via `Hostname.device`) | `Device.last_seen_at` |

`Hostname.device_id` is nullable. With no device, the third station renders
`; called from  — no device assigned` in `--ddns-quiet`, and the lower joint is
`not applicable` (no segment). An unassigned hostname is a configuration state,
not a fault, and must not be marked.

### 4.2 Missing versus zero versus never measured — the table this element exists for

The `; answered` station has **five** states and three of them have a null
address. The backend already distinguishes them and the mapping is
`worker_jobs.stored_dns_status()`; the UI reads that function's states and must
not re-derive them from the columns.

| stored columns | `DnsCheckStatus` | cell text | cell colour | upper rail | glyph |
|---|---|---|---|---|---|
| `dns_checked_at IS NULL` | `NEVER_CHECKED` | `n/a` | `--ddns-quiet` | 1px **dotted** `--ddns-rule` | `·` |
| checked, `dns_check_error IS NOT NULL` | `ERROR` | `unmeasured` + the error string on a second line in `--ddns-quiet` | `--ddns-quiet` | 1px **dashed** `--ddns-rule` | `!` |
| checked, no error, `dns_ip IS NULL` | `MISSING` | `no record` | `--ddns-diverge` | 2px solid `--ddns-diverge` | `≠` |
| checked, no error, `dns_ip == last_ip` | `OK` | the address | `--ddns-ink` | 1px solid `--ddns-rule` | *(none)* |
| checked, no error, `dns_ip != last_ip` | `MISMATCH` | the address, differing groups underlined | `--ddns-diverge` | 2px solid `--ddns-diverge` | `≠` |

Rows 1 and 3 are the pair the issue singles out: **`dns_ip IS NULL` and
`dns_check_error IS NULL` in both, differing only by whether `dns_checked_at`
is set.** They are rendered as far apart as this design can put them — one is
grey text reading `n/a` on a dotted rail, the other is accent text reading
`no record` on a solid accent rail. One means *we have not looked*; the other
means *we looked and the name does not resolve*, which is an outage.

Row 2 is the third: *we tried and could not measure.* It is deliberately **not**
accented, because a resolver timeout is a fact about our instrument, not about
the tenant's DNS, and painting it the same colour as a real divergence tells an
operator their DNS is broken when their resolver is.

**Three prohibitions.**

- **Never render a null address as `0.0.0.0` or `::` or `-` alone.** Those are
  valid addresses and a bare dash is ambiguous across all three null states.
- **Never render a null timestamp as an epoch-derived age.** `last_seen_at IS
  NULL` renders the word `never`, not `56 years ago`. This is not hypothetical:
  `now - 0` is the standard shape of that bug and it makes every freshness rule
  fire for a full cadence after each deploy.
- **Never compute the five states in the frontend.** They arrive from the API
  as the `DnsCheckStatus` string. Two implementations of a five-state rule is
  how five states become three.

### 4.3 Visual weight order is not the aggregation order, and that is deliberate

`worker_jobs._STATUS_RANK` ranks `ERROR` (3) above `MISMATCH` (2) above
`MISSING` (1) above `OK` (0). That is correct **for aggregation** — when
folding an A result and an AAAA result into one hostname verdict, "I could not
measure" must not be hidden behind a known-bad, or the unmeasured half
disappears.

Visual weight runs differently: `MISSING` and `MISMATCH` are loud (accent),
`ERROR` and `NEVER_CHECKED` are quiet (grey, dotted/dashed). Ranking by severity
and ranking by *whose problem it is* are different questions, and this design
answers the second. An implementation that reuses `_STATUS_RANK` to pick a
colour will paint every resolver hiccup as an outage. Use it for `worst()` and
for nothing else.

### 4.4 Timestamps

Relative in the cell (`14:02` within today, `3 d 04 h ago` beyond it, `never`
for null), absolute UTC ISO-8601 with `Z` in the `title` attribute and in
anything copyable. A relative age alone cannot be correlated with a log line,
and this product's other primary surface is a log search.

### 4.5 Empty and loading states

- **Loading**: a static grey block at the strip's height, no shimmer. Critically,
  a loading strip has **no rail** — so it cannot be mistaken for an agreed one.
- **No devices**: `You have no devices yet. Add one to get a DDNS username and
  password.` — the next action, in the body face, in the interface's voice.
- **Device with no hostnames**: `This device has no hostnames. Assign one to
  start tracking it.`
- **Never checked, whole tenant**: `Nothing has been checked yet. The health
  check runs every 15 minutes.` The 15 comes from
  `DdnsConfig.health_check_interval_minutes`, read from the API — not typed
  into the string, so an operator who changes it does not make this sentence
  wrong.

Same rule for the board's `; updates / 7 d` head: the 7 is
`DeviceStatus.window_days`, transported beside the count precisely so a caller
cannot render the numerator without its denominator. Hardcoding it in the
header is the defect the dataclass was shaped to prevent.

### 4.6 The four device liveness states come from the backend, verbatim

`Liveness` is `NEVER_SEEN | LAST_CALL_FAILED | IDLE | ACTIVE` and
`DeviceStatus.render_updates()` already returns the three strings — `—`,
`error`, and the count. The frontend renders what it is given.

| liveness | marker | `; last seen` | `; updates / 7 d` |
|---|---|---|---|
| `NEVER_SEEN` | `!` accent | `never` | `—` |
| `LAST_CALL_FAILED` | `!` accent | relative age | `error` |
| `IDLE` | none | relative age | `0` — a measured zero |
| `ACTIVE` | none | relative age | the count |

`—`, `error` and `0` are three different strings for three different facts, and
a renderer that formats `updates_in_window` directly prints `None` for the first
or, worse, coerces it to `0`.

---

## 5. Two implementation constraints that are not aesthetics

Both were found by reading `@mantine/core@9`'s source in this worktree, and
both will silently corrupt the design if an implementation issue gets them
wrong.

**1. A nested `MantineProvider` writes its CSS variables to `:root` by
default.** `MantineProvider.mjs:34` resolves `cssVariablesSelector ?? ":root"`.
The host bundle mounts its own provider inside atrium's tree, so **any `theme`
prop the host passes lands on `:root` and changes atrium's shell** — the exact
thing `theme.md` forbids. Two rules follow:

- The host's `MantineProvider` passes **no `theme` override for palette, font
  or radius**. It inherits.
- If a future issue genuinely needs one, it must also pass
  `cssVariablesSelector="[data-ddns-root]"`, and the wrapper div must carry that
  attribute. This design does not need one.

**2. `defaultColorScheme` is not a controlled prop.**
`use-provider-color-scheme.mjs` reads `useState(() => manager.get(defaultColorScheme))`
— the initialiser runs once, and `localStorageColorSchemeManager` wins over the
prop whenever a stored value exists. The scaffold's
`<MantineProvider defaultColorScheme={scheme}>` therefore does not follow atrium
after the first mount, and the same file's `useIsomorphicEffect(…, [])` has the
host provider *writing* `data-mantine-color-scheme` onto `<html>` — the shell's
own attribute.

The consequence for this design: **express light/dark in CSS against
`[data-mantine-color-scheme]` on `<html>` (or `light-dark()`, which reads the
same attribute), never through a JS-resolved scheme value.** The attribute is
atrium's and is always right; the host provider's JS state can lag it. Where a
host component genuinely needs the scheme in JS, `forceColorScheme={scheme}`
from `useAtriumColorScheme()` is the correct prop — it tracks the prop instead
of state and never writes to the manager. `defaultColorScheme` is not.

**3. `light-dark()` in §1 is the CSS-native function, not the Mantine PostCSS
mixin.** `postcss-preset-mantine` is not a dependency of this bundle, so the
mixin spelling is unavailable and would compile to nothing. The native function
works here because `@mantine/core/styles.css:3` sets `color-scheme:
var(--mantine-color-scheme)` on `:root`, and lines 348 and 481 bind that
variable to `dark`/`light` off `[data-mantine-color-scheme]`. `color-scheme`
inherits, so the host subtree resolves it correctly without declaring anything.
If a future change stops atrium loading `@mantine/core/styles.css` before the
bundle mounts, every colour in §1 silently resolves to its light value in both
schemes — so the equivalent explicit form
(`[data-mantine-color-scheme="dark"] [data-ddns-root] { … }`) is the fallback,
not a rewrite of the palette.

---

## 6. The critique pass

The instruction is to check whether this plan is what would fall out of any
similar brief. Worked honestly, one item at a time. Two survived unchanged, two
were revised, one was cut.

**Cut — the display face.** The issue asks for "a characterful display face
used with restraint". I decline it, and the argument is not laziness:

1. Atrium owns the shell. A host fragment's largest heading is h3/h4 scale and
   appears once or twice per view. A display face used twice at 22px is not a
   display face; it is a webfont with a rationalisation.
2. `headingsFontFamily` is one of the five keys the Branding admin exposes.
   Overriding it inside the bundle desyncs the host's headings from the shell's
   and takes a control away from the operator to buy two headings.
3. Any bundled face has to travel as base64 inside `dist/main.js` — the vite
   config injects CSS through JS, so a relative `url()` breaks when the bundle
   is served from `system.host_bundle_url` — or be fetched from a CDN. A
   product for people who self-host their DNS specifically so they do not depend
   on someone else's infrastructure should not phone Google on page load, and
   in an air-gapped install it would not resolve.

The character comes from §2.3's inversion (data is larger than its label) and
§2.4's zone-file comment labels, which cost nothing and are specific to this
subject in a way a bought typeface would not be.

**Revised — "mono-as-lead face" was drifting into a genre default.** Setting a
developer-facing product entirely in monospace is the developer-tool cliché, and
the first version of §2 had it. §2.2's boundary rule is the revision: mono is
confined to machine-generated, copy-pasteable values, prose stays in the brand
face, and there is a one-line test for which is which. What changed: the rule,
and the explicit prohibition on mono paragraphs.

**Revised — the strip was a timeline.** The first version drew the rail with
the stations in chronological order and it read as a vertical timeline, which is
a stock component and, worse, implies a time axis that does not exist (the
spacing carries no duration). §3.2 renames it a chain of custody, states that
the *joints* rather than the stations are the datum, and forbids anything that
implies duration. That is a different idea and the joint-verdict table is what
makes it buildable.

**Survives — agreement is achromatic.** Would I reach for "one accent on a
neutral ground" for any brief? Yes, often, and that is AI cluster #2. What is
*not* generic is the specific form it takes here: the accent is banned from all
interactive chrome (Rule 2), and the argument for banning green is a
measurement — 94.5% `nochg` — not a preference. The check I applied: would this
rule survive if the number were 40% `nochg`? No. It would become a normal
red/green board. The rule is derived from M3, so it stays.

**Survives — orange.** Cluster #1's terracotta is a desaturated warm used as a
*background*, around #C56A4E. `#B4500A` is a saturated dark used as *foreground
signal text* at 5.13:1, and it is chosen from the cable colour code rather than
from a mood. Different role, different provenance, and §1.3 shows the value was
forced by a contrast measurement rather than picked.

**Not tested against the third cluster.** Broadsheet hairlines with zero radius
was never in play — §1.2's Rule 3 pushes the rules *heavier* rather than
lighter, on a WCAG measurement, and §3.7 keeps `--mantine-radius-sm`. That the
measurement pushed away from the default is a coincidence worth noting rather
than a virtue to claim.

### The deliberate risk, named

**Removing green. A fully healthy board has no colour on it at all.**

*What it costs.* Users expect green for good. The failure mode this invites is
real: a page with no colour reads to some people as *not loaded yet* rather than
*everything is fine*.

*Why it is worth taking.* M3. 94.5% of real updates change nothing and half the
fleet is idle in any 24-hour window, so a design that paints the healthy state
paints almost the whole surface, and the accent that carries the entire product
has to compete with it. Removing green is what buys the accent its power. This
product's reason to exist is one row in a hundred; a palette that treats the
other ninety-nine as worth colouring has argued against the product.

*What makes it survivable.* Healthy is not blank. An agreed strip carries a
solid hairline rail, a `; N of N agree` label with its denominator, and a
`dns_checked_at` timestamp — three positive marks that a not-yet-loaded strip
does not have, and the loading state deliberately has **no rail** (§4.5) so the
two cannot be confused.

*What would falsify it.* If a user study or a first operator reads a healthy
board as broken, the fix is a single hairline mark on the agreed rail, not the
reintroduction of a fill. Write that down before shipping, so the fix is a
half-step rather than an argument.

---

## 7. What the implementation issues inherit

1. Six palette values, §1, with their measured ratios. Do not add a seventh.
2. Three type roles, §2, with the mono/prose boundary rule.
3. The chain rail, §3.2, with its five segment renderings.
4. The five-state `; answered` table, §4.2, with its three prohibitions.
5. The collapse rule, §3.4, and the ban on a "collapse all" control.
6. The two Mantine constraints in §5.
7. One accent, one motion, one borrowed convention. Anything a seventh element
   would add, cut instead.

Open, and owned by the implementation issues rather than by this one: the API
shape that carries `DnsCheckStatus`, `Liveness` and `window_days` to the
frontend (this document specifies what must arrive, not the endpoint), and
whether the log-search surface reuses the strip's station labels or gets its own
vocabulary.
