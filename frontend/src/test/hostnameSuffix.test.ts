/** The zone is a suffix, not a retype — and there is still only one
 *  module that decides it.
 *
 * ## Why this file exists, and why it is not new work
 *
 * This behaviour was **reported by hand** in the first operator session
 * with the deployed UI (`docs/ops/ui-design.md` §8, row 3: *"a name must
 * be typed as a full FQDN although its zone was just selected"*), fixed
 * by #90, and covered by a table of ten cases plus a
 * no-second-validator sweep in `frontend/src/test/HostnamesPage.test.tsx`.
 *
 * **#127 deleted that file.** It deleted the *page*, correctly — the
 * board is the only tenant surface now — and the test file went with it.
 * The behaviour did not go with it: `composeHostname` and
 * `decomposeHostname` were extracted into `tenant/hostnameName.ts` and
 * are called by `NameModal` on the way in and on the way out. So the
 * suffix rule survived and every guard on it was removed in the same
 * commit, which is the single largest gap the #133 sweep found.
 *
 * It is worse than a plain gap, because `hostnameName.ts`'s own
 * docstring still says the pair is *"the thing the suite sweeps for"*
 * and names `HostnamesPage.test.tsx` twice as the sweeper. A comment
 * asserting a guard that no longer exists is the probe-that-cannot-fail
 * family aimed at a reader instead of at a metric: anyone checking
 * whether the rule is protected finds a sentence saying it is.
 *
 * ## What changed from the deleted version, and what did not
 *
 * The **table is the same ten cases**, kept verbatim including their
 * reasoning, because they were argued once and re-deriving them would be
 * re-litigating a settled decision from a worse position. The vacuity
 * guard is the same.
 *
 * What changed:
 *
 *  - It runs against the **exported composer** rather than through a
 *    rendered `HostnamesPage`, because that page does not exist. The
 *    rendered half — *what is previewed is what is posted* — moved to
 *    `handReported.test.tsx`, which drives `NameModal` on the board.
 *  - The sweep's expected answer moved from `../tenant/HostnameList.tsx`
 *    to `../tenant/hostnameName.ts`, which is where the rule lives now.
 *  - `decomposeHostname` is covered too. It did not exist when #90 was
 *    written; it was extracted by #106 **because a second copy of the
 *    suffix decision had already grown inside `NameModal` and the two
 *    spellings disagreed at the zone apex**. That is the exact drift the
 *    sweep exists to prevent, it happened once, and the round-trip
 *    property below is what would have caught it as an assertion rather
 *    than as a reading.
 */
import { describe, expect, test } from 'vitest';

import {
  composeHostname,
  decomposeHostname,
} from '../tenant/hostnameName';

const ZONE = 'example.net';

/** what is typed → what leaves the browser, with the zone `example.net`
 *  selected. `null` means the preview is not rendered at all, which is a
 *  different state from an empty string. */
const TABLE: [label: string, typed: string, composed: string | null][] = [
  ['a bare label gets the zone appended', 'home', `home.${ZONE}`],
  ['a pasted FQDN is not suffixed twice', `home.${ZONE}`, `home.${ZONE}`],
  [
    'a pasted FQDN in a different case is still recognised',
    `home.${ZONE.toUpperCase()}`,
    // Sent as typed. The server lower-cases on the way in; the browser
    // does not, because "what you typed" is what the preview has to be
    // able to show.
    `home.${ZONE.toUpperCase()}`,
  ],
  [
    'a trailing dot is not special-cased, and the preview says so',
    `home.${ZONE}.`,
    // Deliberate, and pinned so a later "fix" is a decision. A trailing
    // dot marks the root — a fact about the label rule, which lives on
    // the server. `zone_contains` answers False for `foo.example.com.`,
    // so a browser that quietly dropped the dot would be accepting bytes
    // the server refuses.
    `home.${ZONE}..${ZONE}`,
  ],
  ['trailing whitespace is trimmed, then composed', '  home  ', `home.${ZONE}`],
  [
    'a paste with trailing whitespace is trimmed, then recognised',
    `  home.${ZONE}  `,
    `home.${ZONE}`,
  ],
  [
    'the zone in the middle is not the zone at the end',
    // The case the naive `includes()` gets wrong. This name contains
    // `example.net` and does not end with it, so the suffix is appended —
    // anything else would send a name outside the zone.
    `${ZONE}.staging`,
    `${ZONE}.staging.${ZONE}`,
  ],
  ['the apex is left alone', ZONE, ZONE],
  ['an empty field composes to nothing, not to the zone', '', null],
  ['whitespace only is also nothing', '   ', null],
];

describe('the zone is a suffix, not a retype — ui-design §8 row 3, #90', () => {
  test.each(TABLE)('%s', (_label, typed, composed) => {
    expect(composeHostname(typed, ZONE)).toBe(composed ?? '');
  });

  test('the table is not vacuous — composition changed something', () => {
    // Every row above could pass against `composeHostname = (s) => s` if
    // the table happened to contain only already-suffixed names.
    const changed = TABLE.filter(
      ([, typed, composed]) => composed !== null && composed !== typed,
    );
    expect(changed.length).toBeGreaterThan(3);
    // …and the identity rows are real too, or "not suffixed twice" is
    // being asserted by a table with no paste in it.
    const unchanged = TABLE.filter(
      ([, typed, composed]) => composed !== null && composed === typed,
    );
    expect(unchanged.length).toBeGreaterThan(1);
  });

  test('with no zone selected, nothing is appended and nothing is invented', () => {
    // The state the form opens in. A composer that appended `undefined`
    // or `.` here would put a name nobody typed into the preview, and the
    // preview is what the operator reads before pressing the button.
    expect(composeHostname('home', null)).toBe('home');
    expect(composeHostname('home', '')).toBe('home');
  });
});

describe('the inverse, which the modal seeds its box from — #106', () => {
  /** `NameModal` opens on a stored row and puts the **label** in the Name
   *  box, not the FQDN. So it has to answer `composeHostname`'s question
   *  backwards, and a second spelling of it is what #106 found. */
  const CASES: [name: string, expected: string][] = [
    ['the label under its zone', `home.${ZONE}`],
    ['a deeper label keeps its dots', `a.b.${ZONE}`],
    ['the apex is the zone itself, and has no label', ZONE],
    ['a name that merely ends with the zone text is not under it', `not${ZONE}`],
    ['a name outside the zone is left whole', 'home.example.org'],
  ];
  const EXPECTED: Record<string, string> = {
    [`home.${ZONE}`]: 'home',
    [`a.b.${ZONE}`]: 'a.b',
    [ZONE]: ZONE,
    [`not${ZONE}`]: `not${ZONE}`,
    'home.example.org': 'home.example.org',
  };

  test.each(CASES)('%s', (_label, name) => {
    expect(decomposeHostname(name, ZONE)).toBe(EXPECTED[name]);
  });

  test('the apex is where the two spellings disagreed, and it is pinned', () => {
    // `example.net` under zone `example.net` is the zone itself.
    // `NameModal`'s private copy matched on `.${zone}` and stripped
    // nothing here while the composer matched on the bare zone and left
    // it alone — two rules, one apex, different answers. Named because
    // the disagreement was found by reading, and a reading does not fail
    // a build.
    expect(decomposeHostname(ZONE, ZONE)).toBe(ZONE);
    expect(composeHostname(ZONE, ZONE)).toBe(ZONE);
  });

  test('compose(decompose(n)) === n for every name under the zone', () => {
    // The property the pair exists to have, asserted rather than
    // described. It is what makes "open a name, change nothing, Save" a
    // no-op instead of a rename.
    const under = [
      `home.${ZONE}`,
      `a.b.${ZONE}`,
      ZONE,
      `${ZONE}.staging.${ZONE}`,
    ];
    for (const name of under) {
      expect(composeHostname(decomposeHostname(name, ZONE), ZONE)).toBe(name);
    }
  });
});

describe('the bundle does not grow a second validator — #90’s standing guard', () => {
  test('exactly one shipped module decides whether a name ends with its zone', () => {
    // The assertion #90 accepted **in place of** a shared implementation:
    // function identity cannot cross the Python/TypeScript boundary, so
    // the next best property is that the browser's copy of the question
    // has exactly one home. It fails when a second grows *and* when the
    // first is deleted, which is the half that matters here — deleting
    // the sweep is how the previous one stopped protecting anything.
    const sources = import.meta.glob('../**/*.{ts,tsx}', {
      query: '?raw',
      import: 'default',
      eager: true,
    }) as Record<string, string>;
    // Tests are not shipped, and every fetch stub in this directory
    // matches a URL with `url.endsWith(...)`, so an unfiltered sweep
    // names a dozen offenders that are all itself.
    const shipped = Object.entries(sources).filter(
      ([path]) => !/(^|\/)test\//.test(path) && !/\.test\.tsx?$/.test(path),
    );
    // Vacuity: the glob must have read the bundle, not an empty record.
    expect(
      shipped.length,
      'the source glob matched nothing, so the sweep below is asserting ' +
        'over an empty set and would pass however many validators exist',
    ).toBeGreaterThan(10);
    const suffixDeciders = shipped
      .filter(([, text]) => /\.endsWith\(/.test(text))
      .map(([path]) => path)
      .sort();
    expect(
      suffixDeciders,
      'the set of shipped modules that test whether a name already ends ' +
        'with its zone is not exactly [../tenant/hostnameName.ts]. Either ' +
        'a second copy has grown — which is how the apex bug arrived in ' +
        '`NameModal` — or the one home has been moved or deleted, which ' +
        'is how this guard stopped guarding anything in #127.',
    ).toEqual(['../tenant/hostnameName.ts']);
  });
});
