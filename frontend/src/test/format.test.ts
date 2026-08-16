/** The rendering helpers, driven directly.
 *
 * These are the functions the board and the strip use to turn a decided
 * state into a string. Two of them exist specifically to stop a value
 * being rendered as something it is not — `never` for a null timestamp,
 * and a denominator beside every numerator — and both are asserted here
 * against the shape of the bug rather than only against the happy path.
 */
import { describe, expect, test } from 'vitest';

import {
  absoluteTitle,
  agreementSummary,
  calledFromLabel,
  formatAge,
  formatStationTime,
} from '../board/format';
import { differingGroups, splitAddress } from '../board/AddressText';
import { V6_A, V6_B } from './fixtures';

const NOW = Date.parse('2026-08-15T14:05:00Z');

describe('formatAge', () => {
  test('null is the word never, never a number', () => {
    expect(formatAge(null, NOW)).toBe('never');
    // The bug this excludes: `now - 0` is the epoch, i.e. fifty-six
    // years, and every freshness rule fed that alarms for a full
    // cadence after each deploy.
    expect(formatAge(new Date(0).toISOString(), NOW)).toMatch(/\d+ d/);
    expect(formatAge(null, NOW)).not.toMatch(/\d/);
  });

  test.each([
    ['2026-08-15T14:04:40Z', '< 1 min ago'],
    ['2026-08-15T14:03:00Z', '2 min ago'],
    ['2026-08-15T13:24:00Z', '41 min ago'],
    ['2026-08-15T11:05:00Z', '3 h 00 min ago'],
    ['2026-08-12T10:05:00Z', '3 d 04 h ago'],
  ])('%s renders as %s', (iso, expected) => {
    expect(formatAge(iso, NOW)).toBe(expected);
  });

  test('an unparseable timestamp says so instead of guessing', () => {
    // "unknown" and "never" are different facts. Returning `never` for
    // a value we could not read would be the collapse this whole
    // surface is written against, one type down.
    expect(formatAge('not-a-date', NOW)).toBe('unknown');
  });
});

describe('formatStationTime', () => {
  test('a timestamp inside today is clock time; beyond it, an age', () => {
    const today = new Date(NOW);
    today.setHours(14, 2, 0, 0);
    expect(formatStationTime(today.toISOString(), NOW)).toBe('14:02');
    expect(formatStationTime('2026-08-12T10:05:00Z', NOW)).toBe('3 d 04 h ago');
    expect(formatStationTime(null, NOW)).toBe('never');
  });

  test('the absolute form is the API’s own string, unmodified', () => {
    // A relative age alone cannot be correlated with a log line, and
    // this product's other primary surface is a log search. Re-deriving
    // the absolute form from a parsed Date would introduce a second
    // spelling of one instant.
    expect(absoluteTitle('2026-08-15T14:02:00.123456Z')).toBe(
      '2026-08-15T14:02:00.123456Z',
    );
    expect(absoluteTitle(null)).toBe('never');
  });
});

describe('agreementSummary', () => {
  test('names its denominator, and the denominator moves', () => {
    expect(
      agreementSummary({
        joints_agreed: 2,
        joints_compared: 2,
        joints_not_applicable: 0,
        joints_unmeasured: 0,
      }),
    ).toBe('2 of 2 agree');
    // The NAT'd case: one joint is not applicable, so the divisor is 1
    // rather than 2 and the line says so.
    expect(
      agreementSummary({
        joints_agreed: 1,
        joints_compared: 1,
        joints_not_applicable: 1,
        joints_unmeasured: 0,
      }),
    ).toBe('1 of 1 agree, 1 n/a');
    expect(
      agreementSummary({
        joints_agreed: 1,
        joints_compared: 1,
        joints_not_applicable: 0,
        joints_unmeasured: 1,
      }),
    ).toBe('1 of 1 agree, 1 unmeasured');
  });
});

describe('calledFromLabel', () => {
  test('each refusal reads differently', () => {
    const labels = (
      ['evaluated', 'no_device', 'no_update_on_record', 'declared_myip', 'not_comparable'] as const
    ).map(calledFromLabel);
    expect(new Set(labels).size).toBe(labels.length);
    expect(labels[0]).toBe('called from');
    // Every refusal explains itself in the label, because a reader who
    // sees an absent rail and no explanation assumes a bug.
    for (const label of labels.slice(1)) {
      expect(label.length).toBeGreaterThan('called from'.length);
    }
  });
});

describe('address splitting', () => {
  test('IPv6 splits on colons and keeps the separator on the line it broke after', () => {
    const groups = splitAddress(V6_A);
    expect(groups).toHaveLength(8);
    expect(groups[0]).toBe('2001:');
    expect(groups[7]).toBe('0001');
    expect(groups.join('')).toBe(V6_A);
  });

  test('IPv4 splits on dots', () => {
    expect(splitAddress('203.0.113.7')).toEqual(['203.', '0.', '113.', '7']);
  });

  test('only the group that moved is marked', () => {
    const marks = differingGroups(V6_B, V6_A);
    expect(marks).toEqual([false, false, false, false, false, false, false, true]);
  });

  test('a group-count mismatch refuses rather than lining up the wrong hextets', () => {
    // `2001:db8::1` and the expanded form are the same address in
    // different spellings. Comparing group *i* to group *i* across a
    // `::` compression underlines the wrong hextet with total
    // confidence, so the comparison refuses and the caller marks the
    // whole address instead.
    expect(differingGroups('2001:db8::1', V6_A)).toBeNull();
    expect(differingGroups(V6_A, null)).toBeNull();
  });
});
