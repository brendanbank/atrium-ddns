/** Rendering decisions for the log, and only rendering decisions.
 *
 * Same rule as `board/format.ts`: nothing here derives a *state*. Every
 * function takes something the server already decided — a response
 * code, the set of codes the server calls successful, a filter echo —
 * and returns the string or tone the design says goes with it.
 *
 * `docs/ops/ui-design.md` §1.2 Rule 2 is the one that constrains this
 * file hardest:
 *
 * > atrium's primary colour means *you can do this*. `--ddns-diverge`
 * > means *this is true and it is wrong*. Nothing is ever both.
 *
 * On this surface the measured disagreement is a **non-success response
 * code**. Nothing else is accented: not a filter chip, not the clear
 * button, not a column head, not the deleted-device marker. A deleted
 * device is a configuration fact, not a fault, and §4.1 already ruled
 * that an unassigned hostname "is a configuration state, not a fault,
 * and must not be marked".
 */

/** How a cell is coloured. `ink` is the plain, healthy rendering —
 *  §1.2 Rule 1: agreement has no colour. */
export type Tone = 'ink' | 'quiet' | 'diverge';

/** The tone of a response code, decided against the **server's** set.
 *
 * `successCodes` is `worker_jobs.SUCCESS_RESPONSE_CODES`, shipped in
 * the payload. Passing it in rather than closing over a literal is what
 * stops this becoming a second implementation of the health check's own
 * classification: reclassify `dnserr` on the server and this function
 * follows, with nothing to remember.
 *
 * `null` is `quiet`, not `diverge`. A row with no response code did not
 * answer on the wire — a health check does not — and painting *absence
 * of an answer* the same colour as *a bad answer* is the same mistake
 * §4.2 refuses when it keeps `error` grey: a fact about our instrument
 * rendered as a fact about the tenant's DNS.
 */
export function responseTone(
  code: string | null,
  successCodes: readonly string[],
): Tone {
  if (code === null) return 'quiet';
  return successCodes.includes(code) ? 'ink' : 'diverge';
}

/** The redundant non-colour channel for a failed line — §1.2 Rule 3.
 *
 * In the dark scheme the accent and the ink differ in luminance by
 * 1.42:1, so a greyscale render shows an accented code and a plain one
 * as the same tone. `≠` is the glyph the strip already uses for *this
 * is true and it is wrong*, and reusing it is the point: one learned
 * mark, two surfaces.
 *
 * `null` for the two tones that mean "nothing to see", exactly as
 * `board/format.ts::jointGlyph` does.
 */
export function responseGlyph(tone: Tone): string | null {
  switch (tone) {
    case 'ink':
      return null;
    case 'quiet':
      return null;
    case 'diverge':
      return '≠';
  }
}

/** The word channel. Never a substitute for the glyph — the third of
 *  the three, and the one a screen reader gets. */
export function responseWord(tone: Tone): string {
  switch (tone) {
    case 'ink':
      return 'accepted';
    case 'quiet':
      return 'no wire answer';
    case 'diverge':
      return 'refused';
  }
}

/** How the `; via` cell reads.
 *
 * `null` in `backend_type` is a **meaning**, not a missing value:
 * `models.DnsEvent` records it as "decided before any backend was
 * contacted" — every `badauth`, `abuse`, `911`, `notfqdn` and `nohost`
 * row, and a hostname whose domain has zero backends. So it renders as
 * a word with an explanation available, never as `—` alone, which
 * §4.2's first prohibition rules out for exactly this ambiguity.
 */
export function backendCell(
  backendType: string | null,
): { text: string; tone: Tone; title: string } {
  if (backendType === null) {
    return {
      text: 'no backend',
      tone: 'quiet',
      title:
        'decided before any provider was contacted — an authentication ' +
        'refusal, a rate limit, an unknown hostname, or a domain with no ' +
        'backend configured',
    };
  }
  return { text: backendType, tone: 'ink', title: backendType };
}

/** A denormalised name and whether it can still be filtered on.
 *
 * The pair is the whole point of carrying both halves. `name` is
 * captured at write time and survives the row it describes being
 * deleted; `id` is `ON DELETE SET NULL` and is `null` exactly when it
 * is gone.
 *
 * Three outcomes, and they are three:
 *
 * - a name and an id — render it, and offer the filter
 * - a name and no id — render it, say **deleted**, offer no filter.
 *   A link that filters on nothing is worse than no link: it returns an
 *   empty log that reads as "this device did nothing".
 * - no name at all — the row predates the denormalised columns, or the
 *   writer had nothing to capture. `unknown`, quiet. Not the empty
 *   string, which renders as a blank cell and reads as a layout bug.
 */
export function nameCell(
  name: string | null,
  id: number | null,
): { text: string; deleted: boolean; filterable: boolean; tone: Tone } {
  if (name === null) {
    return { text: 'unknown', deleted: false, filterable: false, tone: 'quiet' };
  }
  return {
    text: name,
    deleted: id === null,
    filterable: id !== null,
    tone: 'ink',
  };
}

/** Human label for a filter key, for the applied-filter chips.
 *
 * Body face, no `;` prefix. §2.4 confines the zone-file-comment
 * convention to machine-data column heads and station labels — "not on
 * form fields, not on section titles, not on empty states". A chip is a
 * control, so it speaks.
 */
export function filterLabel(key: string): string {
  switch (key) {
    case 'user_id':
      return 'user';
    case 'device_id':
      return 'device';
    case 'domain_id':
      return 'domain';
    case 'hostname_id':
      return 'hostname';
    case 'event_type':
      return 'event';
    case 'response_code':
      return 'result';
    case 'backend_type':
      return 'backend';
    case 'client_ip':
      return 'called from';
    case 'since':
      return 'since';
    case 'until':
      return 'until';
    default:
      return key;
  }
}
