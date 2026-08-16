/** An address, wrapped only where it is safe to wrap.
 *
 * Two jobs, both from `docs/ops/ui-design.md`:
 *
 * **§3.5 — break opportunities.** They exist *only* immediately after a
 * `:` in an IPv6 literal and after a `.` in an IPv4 literal, so this
 * emits `<wbr>` at those points and the CSS sets `overflow-wrap: normal`.
 * Never `anywhere` or `break-all`: those split a hextet, and `…:0db8:`
 * and `…:0d` `b8:` look alike. A layout whose overflow behaviour is
 * "silently show a different address" is disqualified, and this is the
 * same rule applied one level down.
 *
 * **§3.2 — the differing groups get underlined.** The third redundant
 * channel. In the dark scheme the accent and the ink differ in luminance
 * by 1.42:1, so colour alone does not survive a greyscale render; an
 * underline does, and it also says *which part* moved, which the colour
 * never could.
 *
 * ## Why the group comparison can refuse
 *
 * The two addresses are compared group by group only when they split
 * into the same number of groups on the same separator. `ipaddress`'s
 * normal form may `::`-compress an IPv6 address, so two spellings of the
 * same prefix can have different group counts — and lining up group *i*
 * against group *i* across a compression is how you underline the wrong
 * hextet with total confidence. When the counts disagree the whole
 * address is marked instead.
 *
 * In this estate that path is close to dead: M2 measured **7 colons on
 * 232 of 232 rows** — eight groups, never compressed. It is written this
 * way because "close to dead" is not "unreachable", and the failure it
 * avoids is a confidently wrong highlight rather than a missing one.
 */
import { Fragment } from 'react';

import type { Tone } from './format';

/** Split an address into groups *including* the trailing separator, so
 *  the separator stays at the end of the line it broke after and the
 *  reader can see the join (§3.5). */
export function splitAddress(value: string): string[] {
  const separator = value.includes(':') ? ':' : '.';
  const raw = value.split(separator);
  return raw.map((part, index) =>
    index === raw.length - 1 ? part : part + separator,
  );
}

/** Per-group "does this differ", or `null` when the two are not
 *  comparable group-wise. Exported for its own test — the refusal is the
 *  interesting half and it is invisible from the rendered DOM. */
export function differingGroups(
  value: string,
  compareTo: string | null | undefined,
): boolean[] | null {
  if (!compareTo) return null;
  const mine = splitAddress(value);
  const theirs = splitAddress(compareTo);
  if (mine.length !== theirs.length) return null;
  return mine.map((group, index) => group !== theirs[index]);
}

export interface AddressTextProps {
  value: string | null;
  /** The other side of the joint. When supplied and group-comparable,
   *  the groups that differ are underlined. */
  compareTo?: string | null;
  tone?: Tone;
  'data-testid'?: string;
}

export function AddressText({
  value,
  compareTo,
  tone = 'ink',
  'data-testid': testId,
}: AddressTextProps) {
  if (value === null) return null;

  const groups = splitAddress(value);
  const differs = differingGroups(value, compareTo);
  // Counts disagreed (or there was nothing to compare against): mark the
  // whole address rather than guessing which group moved.
  const wholeDiffers = compareTo != null && differs === null && value !== compareTo;

  return (
    <span
      className="ddns-data ddns-address"
      data-tone={tone}
      data-testid={testId}
      title={value}
    >
      {groups.map((group, index) => (
        <Fragment key={index}>
          <span
            className="ddns-group"
            data-differs={
              (differs ? differs[index] : wholeDiffers) ? 'true' : 'false'
            }
          >
            {group}
          </span>
          {index < groups.length - 1 ? <wbr /> : null}
        </Fragment>
      ))}
    </span>
  );
}
