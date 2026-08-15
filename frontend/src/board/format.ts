/** Rendering decisions, and only rendering decisions.
 *
 * Nothing in this module derives a *state*. Every function here takes a
 * state the backend already decided and returns the string or tone the
 * design says goes with it. The distinction matters because
 * `docs/ops/ui-design.md` §4.2's third prohibition is about derivation,
 * not about display: mapping `'missing'` onto the words `no record` is
 * the frontend's job; deciding that a row *is* `missing` is not.
 *
 * The switches are exhaustive over the union types in `../api/board`.
 * A sixth `DnsCheckStatus` added on the server makes `tsc` fail here
 * rather than silently falling through to a default — which is how five
 * states would otherwise become four.
 */
import type {
  DnsCheckStatus,
  JointVerdict,
  LowerJointReason,
} from '../api/board';

/** How an address cell is coloured. `ink` is the plain, healthy
 *  rendering — ui-design.md §1.2 Rule 1: agreement has no colour. */
export type Tone = 'ink' | 'quiet' | 'diverge';

/** What the `; answered` cell says, per §4.2's five-row table.
 *
 * The two rows this element exists for are `never_checked` and
 * `missing`: both have `dns_ip IS NULL` and `dns_check_error IS NULL`,
 * and they differ only by whether `dns_checked_at` is set. They are
 * rendered as far apart as this design can put them — grey `n/a` on a
 * dotted rail against accent `no record` on a solid accent rail. One
 * means *we have not looked*; the other means *we looked and the name
 * does not resolve*, which is an outage.
 *
 * `error` is the third: *we tried and could not measure*. Deliberately
 * not accented, because a resolver timeout is a fact about our
 * instrument and not about the tenant's DNS — painting it the same
 * colour as a real divergence tells an operator their DNS is broken
 * when their resolver is.
 */
export function answeredCell(
  status: DnsCheckStatus,
  address: string | null,
): { text: string | null; tone: Tone } {
  switch (status) {
    case 'never_checked':
      return { text: 'n/a', tone: 'quiet' };
    case 'error':
      return { text: 'unmeasured', tone: 'quiet' };
    case 'missing':
      return { text: 'no record', tone: 'diverge' };
    case 'ok':
      return { text: address, tone: 'ink' };
    case 'mismatch':
      return { text: address, tone: 'diverge' };
  }
}

/** The glyph in the rail's gutter — §3.2's redundant-channel table.
 *  `null` for the two verdicts that draw none. */
export function jointGlyph(verdict: JointVerdict): string | null {
  switch (verdict) {
    case 'agreed':
      return null;
    case 'diverged':
      return '≠'; // ≠
    case 'not_measured_never':
      return '·'; // ·
    case 'not_measured_failed':
      return '!';
    case 'not_applicable':
      return null;
  }
}

/** The word channel. §1.2 Rule 3 requires stroke style, glyph **and** a
 *  word, because in the dark scheme the accent and the ink differ in
 *  luminance by 1.42:1 — a greyscale render shows them as one tone. */
export function jointWord(verdict: JointVerdict): string {
  switch (verdict) {
    case 'agreed':
      return 'agrees';
    case 'diverged':
      return 'diverged';
    case 'not_measured_never':
      return 'never checked';
    case 'not_measured_failed':
      return 'could not measure';
    case 'not_applicable':
      return 'not applicable';
  }
}

/** The `; called from` label, with the reason folded into it — §3.3.
 *
 * The suffix is the whole point. A reader who sees no rail segment and
 * no explanation assumes a bug; a reader who sees
 * `; called from (declared myip)` learns that the two columns differ
 * permanently and correctly, because the device declares its address
 * rather than being read from its call.
 *
 * Returned without the leading `; ` — the CSS generates that from
 * `.ddns-label::before` so the convention cannot drift string by
 * string.
 */
export function calledFromLabel(reason: LowerJointReason): string {
  switch (reason) {
    case 'evaluated':
      return 'called from';
    case 'no_device':
      return 'called from — no device assigned';
    case 'no_update_on_record':
      return 'called from (no update on record)';
    case 'declared_myip':
      return 'called from (declared myip)';
    case 'not_comparable':
      return 'called from (nothing to compare)';
  }
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

/** An age, or the word `never` — §3.6's board column and §4.4.
 *
 * `null` is `never`, never `0` and never an epoch-derived age.
 * `now - 0` is fifty-six years, which is the standard shape of that bug
 * and makes every freshness rule fire for a full cadence after each
 * deploy (§4.2, second prohibition).
 *
 * The two forms the design shows verbatim are `41 min ago` and
 * `3 d 04 h ago`. The hour band between them is filled with the same
 * two-unit shape rather than a third style.
 */
export function formatAge(iso: string | null, now: number = Date.now()): string {
  if (iso === null) return 'never';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return 'unknown';
  const delta = now - then;
  if (delta < MINUTE) return '< 1 min ago';
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)} min ago`;
  if (delta < DAY) {
    const hours = Math.floor(delta / HOUR);
    return `${hours} h ${pad(Math.floor((delta % HOUR) / MINUTE))} min ago`;
  }
  const days = Math.floor(delta / DAY);
  return `${days} d ${pad(Math.floor((delta % DAY) / HOUR))} h ago`;
}

/** The timestamp beside a station's address — §4.4.
 *
 * `14:02` within today, an age beyond it, `never` for null. Local
 * clock-time for the same-day case on purpose: within a working day
 * "14:02" is the thing an operator correlates against their own
 * memory, and the absolute UTC form is one hover away in `title`.
 */
export function formatStationTime(
  iso: string | null,
  now: number = Date.now(),
): string {
  if (iso === null) return 'never';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return 'unknown';
  const today = new Date(now);
  const sameDay =
    then.getFullYear() === today.getFullYear() &&
    then.getMonth() === today.getMonth() &&
    then.getDate() === today.getDate();
  if (sameDay) return `${pad(then.getHours())}:${pad(then.getMinutes())}`;
  return formatAge(iso, now);
}

/** What goes in `title=` and anything copyable — §4.4.
 *
 * The API's own string, unmodified. A relative age alone cannot be
 * correlated with a log line, and this product's other primary surface
 * is a log search. Re-deriving the absolute form from the parsed
 * `Date` would introduce a second spelling of the same instant.
 */
export function absoluteTitle(iso: string | null): string {
  return iso ?? 'never';
}

/** The collapsed strip's summary line — §3.4.
 *
 * It **names its denominator**. `; agrees` on its own would be a ratio
 * with the divisor hidden, and the divisor here moves: it is 2 for a
 * device publishing its own address and 1 for one declaring `myip`.
 */
export function agreementSummary(counts: {
  joints_agreed: number;
  joints_compared: number;
  joints_not_applicable: number;
  joints_unmeasured: number;
}): string {
  const parts = [
    `${counts.joints_agreed} of ${counts.joints_compared} agree`,
  ];
  if (counts.joints_not_applicable > 0) {
    parts.push(`${counts.joints_not_applicable} n/a`);
  }
  if (counts.joints_unmeasured > 0) {
    parts.push(`${counts.joints_unmeasured} unmeasured`);
  }
  return parts.join(', ');
}
