/** What a zone with no provider says, in one place.
 *
 * Part II §10.1 fixes the words, and it fixes them against the obvious
 * alternative:
 *
 * > Not "no backends configured". The operator does not own a backend;
 * > they own a zone that does or does not work.
 *
 * So the sentence is about the zone and about the wire, in the
 * operator's terms: **publishes nowhere**, and *every update for a name
 * in this zone answers 911*. `911` is DynDNS v2 for *the service is
 * broken, stop asking*; a well-behaved router backs off hard on it.
 *
 * It appears on two surfaces — the zone list and the zone detail route —
 * and it lives here rather than in either of them because two copies of
 * a sentence are two sentences the moment one is edited. The e2e spec
 * asserts against `WIRE_CONSEQUENCE` for the same reason: a test that
 * retyped the string would keep passing after the product stopped
 * saying it.
 *
 * ## Not a palette value, and not a new element
 *
 * The treatment is `--ddns-diverge` / `--ddns-diverge-wash`, which
 * already exist (§14.1: the accent "gets a new *use* … and no new
 * value"). The markup is a row with a glyph and a phrase; §14.5 forbids
 * a seventh element and this is not one.
 */
import { Text } from '@mantine/core';

/** The phrase, and the reason the phrase is not "no backends". */
export const PUBLISHES_NOWHERE = 'publishes nowhere';

/** The wire fact, stated as the operator experiences it. Frozen at
 *  `tests/compat/protocol_cases.yaml:211` — `update/no-backends-911`,
 *  *"hostname owned, but zero backends -> 911 {ip}"*. */
export const WIRE_CONSEQUENCE =
  'every update for a name in this zone answers 911';

/** The glyph channel. §1.2 Rule 3: colour is never the only channel,
 *  because in the dark scheme the accent and the ink differ in luminance
 *  by 1.42:1 and a greyscale render shows them as one tone. */
export const NOWHERE_GLYPH = '⚠';

/** The marker that sits beside the zone name. Glyph plus word — the two
 *  channels that survive a greyscale render; the border and wash are the
 *  third and they belong to the row, not to this span. */
export function ZoneNowhereMark({ testId }: { testId?: string }) {
  return (
    <span className="ddns-zone__nowhere" data-testid={testId}>
      <span aria-hidden="true">{NOWHERE_GLYPH} </span>
      {PUBLISHES_NOWHERE}
    </span>
  );
}

/** The consequence line, under the name. `.ddns-label` renders the
 *  leading `;` — §2.4, labels are zone-file comments — so the string
 *  itself does not carry one and cannot drift from the convention. */
export function ZoneWireConsequence({ testId }: { testId?: string }) {
  return (
    <span className="ddns-label" data-testid={testId}>
      {WIRE_CONSEQUENCE}
    </span>
  );
}

/** The prospective form, for the "add a provider later" link.
 *
 * Deliberately **not** accented and deliberately not this file's
 * `--ddns-diverge`: §1.2 Rule 2 bans the accent from anything that means
 * *you can do this*, and this sentence sits next to a control. It is
 * also not a measurement — nothing has happened yet — and the accent is
 * reserved for a state the data is actually in.
 */
export function ZoneLaterConsequence({ testId }: { testId?: string }) {
  return (
    <Text size="xs" c="dimmed" data-testid={testId}>
      The zone is created with no provider. It {PUBLISHES_NOWHERE} until you
      add one — {WIRE_CONSEQUENCE}.
    </Text>
  );
}
