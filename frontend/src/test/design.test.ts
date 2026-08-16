/** Guards on the design's own values, read out of the shipped files.
 *
 * `docs/ops/ui-design.md` §7 lists seven things the implementation
 * issues inherit. Four of them are checkable without a browser, and
 * they are checked here — against the CSS this bundle actually ships
 * and against the enums the API actually declares, never against a list
 * retyped into the test.
 *
 * The rule this file is written to: **derive, don't hardcode.** A
 * hardcoded list of six token names is the identical defect one commit
 * later, and it would keep passing against a stylesheet that had grown
 * a seventh.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

import { answeredCell, jointGlyph, jointWord } from '../board/format';
import type { DnsCheckStatus, JointVerdict } from '../api/board';

// Read off disk rather than imported.
//
// The obvious spelling — `import CSS from '../ddns.css?raw'` — reads as
// the *better* one, because it goes through the same resolver
// `DdnsRoot`'s side-effect import uses. **Measured, and it does not
// work:** vitest defaults to `css: false` and stubs CSS modules, so the
// `?raw` specifier resolves to the empty string. Every regex below then
// matches nothing and every assertion passes against `[]` — six guards
// that could not fail, in the file whose whole job is to make guards
// fail. The length assertion underneath is the vacuity check that would
// have caught it.
// `process.cwd()` and not `import.meta.url`: under vitest the module URL
// is not a `file:` URL and `fileURLToPath` throws *"The URL must be of
// scheme file"*. vitest's cwd is the package root, which is where
// `vitest.config.ts` and `package.json` live.
const CSS = readFileSync(resolve(process.cwd(), 'src/ddns.css'), 'utf8');

const LIGHT_BLOCK = /\[data-ddns-root\]\s*\{([^}]*)\}/;
const DARK_BLOCK =
  /\[data-mantine-color-scheme='dark'\]\s*\[data-ddns-root\]\s*\{([^}]*)\}/;

function declared(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of block.split(';')) {
    const match = line.match(/(--ddns-[a-z0-9-]+)\s*:\s*([^;]+)/);
    if (match) out[match[1]] = match[2].trim();
  }
  return out;
}

const light = declared(CSS.match(LIGHT_BLOCK)?.[1] ?? '');
const dark = declared(CSS.match(DARK_BLOCK)?.[1] ?? '');

describe('the instrument itself', () => {
  test('the stylesheet was actually read, and both blocks were found', () => {
    // Every other test in this file is a regex over `CSS`. An empty
    // `CSS` makes all of them pass, which is the shape this whole
    // repository keeps cataloguing. Ask first what the file would print
    // if the thing it measures were absent.
    expect(CSS.length).toBeGreaterThan(2000);
    expect(Object.keys(light).length).toBeGreaterThan(0);
    expect(Object.keys(dark).length).toBeGreaterThan(0);
  });
});

describe('the palette', () => {
  test('is six values, and there is no seventh', () => {
    // §7.1: "Six palette values, §1, with their measured ratios. Do not
    // add a seventh." Counted off the stylesheet, so adding one fails
    // here rather than in a review.
    expect(Object.keys(light).sort()).toEqual([
      '--ddns-diverge',
      '--ddns-diverge-wash',
      '--ddns-edge',
      '--ddns-ink',
      '--ddns-quiet',
      '--ddns-rule',
    ]);
  });

  test('every colour that has a dark value has one, and it is different', () => {
    // Derived: the dark block must cover every light token except the
    // one that is *computed from* another (`--ddns-diverge-wash` is a
    // `color-mix` off `--ddns-diverge`, so it follows automatically and
    // restating it would be a second source of truth).
    const derived = Object.entries(light)
      .filter(([, value]) => value.includes('color-mix'))
      .map(([name]) => name);
    expect(derived).toEqual(['--ddns-diverge-wash']);

    const expected = Object.keys(light).filter((n) => !derived.includes(n));
    expect(Object.keys(dark).sort()).toEqual(expected.sort());
    for (const name of expected) {
      expect(dark[name], `${name} is the same in both schemes`).not.toBe(
        light[name],
      );
    }
  });

  test('the accent is a host-owned literal, not a Mantine step', () => {
    // §1.3, and the measurement behind it: `--mantine-color-orange-9`
    // (#d9480f) is the darkest orange Mantine ships and is 4.30:1 on
    // white — short of the 4.5:1 that 14px accent text needs. And a
    // status colour must not move when someone rebrands, because a
    // learned signal that is re-themable is not a signal.
    expect(light['--ddns-diverge']).toBe('#b4500a');
    expect(dark['--ddns-diverge']).toBe('#f59042');
    expect(light['--ddns-diverge']).not.toMatch(/var\(--mantine/);
    expect(dark['--ddns-diverge']).not.toMatch(/var\(--mantine/);
  });

  test('the rail is gray-6, not gray-3 — WCAG 1.4.11', () => {
    // The instinct is a very light hairline. But the rail *carries
    // state*, so it is a graphical object under 1.4.11 and needs 3:1.
    // gray-3 is 1.30:1: a rule that means something, drawn so faintly
    // that the standard treats it as absent.
    expect(light['--ddns-rule']).toBe('var(--mantine-color-gray-6)');
    expect(light['--ddns-edge']).toBe('var(--mantine-color-gray-3)');
    expect(light['--ddns-rule']).not.toBe(light['--ddns-edge']);
  });

  test('nothing writes into atrium’s token namespace', () => {
    // `docs/theme.md` forbids a host shipping raw-CSS overrides for
    // atrium's tokens. Reading them is the contract; writing one is the
    // violation, and the two look identical at a glance.
    const assignments: string[] = CSS.match(/^\s*--[a-z0-9-]+\s*:/gm) ?? [];
    const foreign = assignments.filter((line) => !line.includes('--ddns-'));
    expect(foreign).toEqual([]);
  });

  test('the accent never lands on a control — §1.2 Rule 2', () => {
    // > atrium's primary colour means *you can do this*.
    // > `--ddns-diverge` means *this is true and it is wrong*.
    // > Nothing is ever both.
    //
    // The design states the rule as a list of places the accent may not
    // appear — "not on a button, not on a heading, not on a nav item,
    // not on a focus ring, not on a link" — and a list is not
    // checkable. `cursor: pointer` is: it is what every control in this
    // stylesheet declares and what nothing else does, so "no rule block
    // carries both" is the rule expressed as a property.
    //
    // Added by #46, which introduced the first accented rows that sit
    // beside clickable filter controls. Before that the two never met
    // in one file and the rule held by accident.
    const blocks = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
      .split('}')
      .map((chunk: string) => {
        const [selector, body = ''] = chunk.split('{');
        return { selector: selector.trim(), body };
      })
      .filter((block) => block.body.length > 0);

    const both = blocks.filter(
      (block) =>
        /cursor:\s*pointer/.test(block.body) &&
        /--ddns-diverge/.test(block.body),
    );
    expect(both.map((block) => block.selector)).toEqual([]);

    // Vacuity, twice: the sweep must have found blocks of each kind, or
    // "no block has both" is true because no block has either.
    expect(
      blocks.filter((block) => /cursor:\s*pointer/.test(block.body)).length,
    ).toBeGreaterThan(0);
    expect(
      blocks.filter((block) => /--ddns-diverge/.test(block.body)).length,
    ).toBeGreaterThan(0);
  });

  test('every selector is scoped to the host subtree', () => {
    // The bundle's CSS reaches the page as a runtime <style> tag, so an
    // unscoped selector applies to atrium's shell.
    const selectors: string[] = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
      .split('}')
      .map((chunk: string) => chunk.split('{')[0].trim())
      .filter((chunk: string) => chunk.length > 0 && !chunk.startsWith('@'));
    const unscoped = selectors.filter(
      (selector: string) => !selector.includes('[data-ddns-root]'),
    );
    expect(unscoped).toEqual([]);
  });
});

/** Every rule block in the shipped stylesheet, comments stripped.
 *
 * The accent sweep below already built this inline; Part III needs it
 * three more times, so it is one function rather than four copies of a
 * split that has to agree with itself.
 */
function ruleBlocks(): Array<{ selector: string; body: string }> {
  return CSS.replace(/\/\*[\s\S]*?\*\//g, '')
    .split('}')
    .map((chunk: string) => {
      const [selector, body = ''] = chunk.split('{');
      return { selector: selector.trim(), body };
    })
    .filter((block) => block.body.length > 0);
}

describe('an interactive .ddns-data — Part III §16.1', () => {
  // The operator said *"I still cannot edit the zone"* about a zone
  // name that **was** an `<Anchor href>` and **did** navigate.
  // `.ddns-data` sets `color: var(--ddns-ink)`, which cancels Mantine's
  // link colour and underline, and nothing else on the element said it
  // was a destination.
  //
  // These four guards are the "visual rule with a test" the issue asks
  // for. Each is written so that undoing the fix the obvious way —
  // deleting the block, moving it behind `:hover`, colouring it instead,
  // or dropping `.ddns-data` from the anchors — fails here rather than
  // in a review six weeks later.

  test('carries a text-decoration affordance, and it is not colour', () => {
    const interactive = ruleBlocks().filter(
      (block) =>
        /\ba\.ddns-data\b/.test(block.selector) ||
        /\bbutton\.ddns-data\b/.test(block.selector),
    );
    // Vacuity: the sweep must have found the rule at all. Every
    // assertion below is over this list, and an empty list passes them
    // all — the exact shape the file's own header warns about.
    expect(
      interactive.length,
      'no rule in ddns.css selects an interactive .ddns-data. §16.1 requires ' +
        'one: the anchors are the destination and nothing else on them says so.',
    ).toBeGreaterThan(0);

    const underlining = interactive.filter((block) =>
      /text-decoration:\s*underline/.test(block.body),
    );
    expect(
      underlining.map((block) => block.selector),
      'an interactive .ddns-data must carry an affordance that is not colour',
    ).not.toEqual([]);

    // "not colour" is the half that is easy to regress by *adding*
    // something rather than by removing it. §1.2 Rule 2 gives
    // interactive chrome to atrium's primary colour, which the operator
    // owns and can rebrand to the ink; an affordance that leaned on it
    // would be one theme change from invisible again.
    for (const block of interactive) {
      expect(
        /(^|[;{\s])color:/.test(block.body),
        `${block.selector} sets a colour. Colour is spoken for (§2.3 gives it ` +
          'to the ink, §1.2 Rule 2 gives the accent to nothing clickable), so ' +
          'the affordance has to be something else.',
      ).toBe(false);
    }
  });

  test('the affordance is at rest, not behind :hover', () => {
    // Hover-only "fails the same operator on the same page, and fails
    // entirely on touch". Stated as a property of the stylesheet: no
    // rule in this file may make a `.ddns-data` decoration conditional
    // on the pointer being over it.
    const hoverGated = ruleBlocks().filter(
      (block) =>
        /\.ddns-data/.test(block.selector) &&
        /:hover|:focus-visible|:focus\b/.test(block.selector),
    );
    expect(
      hoverGated.map((block) => block.selector),
      'a .ddns-data affordance behind a pointer state is not an affordance for ' +
        'the reader who has not moved the pointer, and is none at all on touch.',
    ).toEqual([]);
  });

  test('§2.3 is not retracted — .ddns-data still sets the ink', () => {
    // The fix that was explicitly ruled out: dropping `.ddns-data` from
    // the anchors so Mantine's link colour comes back. That would break
    // the type idea on the most important string on the page. So the
    // base rule must still declare the data face and the ink, and this
    // fails if a later change "fixes" the affordance by deleting them.
    const base = ruleBlocks().find(
      (block) => block.selector === '[data-ddns-root] .ddns-data',
    );
    expect(base, 'the base .ddns-data rule is gone').toBeDefined();
    expect((base as { body: string }).body).toMatch(
      /color:\s*var\(--ddns-ink\)/,
    );
    expect((base as { body: string }).body).toMatch(
      /font-family:\s*var\(--ddns-font-data\)/,
    );
  });

  test('the link underline and the differing-hextet underline are told apart', () => {
    // Two underlines with two meanings now live in one stylesheet: a
    // destination, and the groups of an address that differ from the
    // one above it (§1.2 Rule 3's third channel). On a greyscale
    // screenshot the only thing separating them is the stroke, so they
    // may not share a thickness.
    const thickness = (predicate: (selector: string) => boolean) =>
      ruleBlocks()
        .filter((block) => predicate(block.selector))
        .map((block) => block.body.match(/text-decoration-thickness:\s*([^;]+)/))
        .filter((match): match is RegExpMatchArray => match !== null)
        .map((match) => match[1].trim());

    const link = thickness((selector) => /\.ddns-data\b/.test(selector));
    const differs = thickness((selector) =>
      /\.ddns-group\[data-differs='true'\]/.test(selector),
    );
    expect(link, 'the interactive .ddns-data underline has no thickness').not.toEqual(
      [],
    );
    expect(differs, 'the differing-group underline has no thickness').not.toEqual(
      [],
    );
    expect(new Set([...link, ...differs]).size).toBe(2);
  });
});

describe('the redundant channels', () => {
  test('every non-agreed verdict carries a glyph and a word', () => {
    // §1.2 Rule 3: colour is never the only channel. In dark the accent
    // and the ink differ in luminance by 1.42:1, so a greyscale render
    // shows a diverged address and an agreeing one as the same tone.
    const verdicts: JointVerdict[] = [
      'agreed',
      'diverged',
      'not_measured_never',
      'not_measured_failed',
      'not_applicable',
    ];
    for (const verdict of verdicts) {
      expect(jointWord(verdict), verdict).toBeTruthy();
    }
    // The two that draw nothing are the two that mean "nothing to see":
    // an agreement, and a comparison that was never made.
    expect(verdicts.filter((v) => jointGlyph(v) === null)).toEqual([
      'agreed',
      'not_applicable',
    ]);
    expect(jointGlyph('diverged')).toBe('≠');
  });

  test('one stroke style per verdict, and no two share one', () => {
    // Read off the stylesheet rather than restated: the stroke style is
    // the first of the three channels and the one a screenshot keeps.
    const strokes = new Map<string, string>();
    for (const match of CSS.matchAll(
      /\.ddns-rail__joint\[data-verdict='([a-z_]+)'\][\s\S]*?border-left:\s*([^;]+);/g,
    )) {
      strokes.set(match[1], match[2].trim());
    }
    expect([...strokes.keys()].sort()).toEqual([
      'agreed',
      'diverged',
      'not_measured_failed',
      'not_measured_never',
    ]);
    expect(new Set(strokes.values()).size).toBe(strokes.size);
    // `not_applicable` deliberately has no rule of its own: absence of a
    // segment is the statement.
    expect(strokes.has('not_applicable')).toBe(false);
  });
});

describe('the five-state table', () => {
  test('every status maps to its own words, and three of them share a null address', () => {
    const statuses: DnsCheckStatus[] = [
      'never_checked',
      'ok',
      'mismatch',
      'missing',
      'error',
    ];
    // Driven with a null address so the three that *must* say something
    // other than the address are forced to show their own string.
    const nulls = statuses
      .map((status) => [status, answeredCell(status, null)] as const)
      .filter(([, cell]) => cell.text !== null);
    expect(nulls.map(([status]) => status)).toEqual([
      'never_checked',
      'missing',
      'error',
    ]);
    expect(new Set(nulls.map(([, cell]) => cell.text)).size).toBe(3);
    // The quiet/loud split. `error` is our resolver's problem, not the
    // tenant's, and must not be painted like an outage.
    expect(answeredCell('error', null).tone).toBe('quiet');
    expect(answeredCell('never_checked', null).tone).toBe('quiet');
    expect(answeredCell('missing', null).tone).toBe('diverge');
  });
});

describe('motion', () => {
  test('there is exactly one transition, and it is on the rail', () => {
    // §3.7. An operations board that animates is a board people stop
    // trusting, and the restraint is the point of spending the boldness
    // on the rail.
    const transitions = [...CSS.matchAll(/^\s*transition:\s*([^;]+);/gm)].map(
      (m) => m[1].trim(),
    );
    expect(transitions).toEqual(['border-color 120ms ease', 'none']);
    expect(CSS).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    // No shimmer anywhere: the loading state is a flat block on purpose,
    // and it has no rail so it cannot be mistaken for an agreed strip.
    expect(CSS).not.toMatch(/@keyframes/);
  });
});
