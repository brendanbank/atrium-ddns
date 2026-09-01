/** Where the board row's add-a-name `+` lives, and why the cell matters.
 *
 * ## The defect this is written against
 *
 * The `+` sat in the **name** cell, in a `wrap="nowrap"` group of
 * `[anchor, +]`. Two rules in `ddns.css` combined against it:
 *
 * | rule | value |
 * |---|---|
 * | `.ddns-boardtable` column 3 (the name) | `minmax(12rem, 1fr)` |
 * | `.ddns-boardtable__row > *` | `overflow: hidden` |
 *
 * A name longer than the column consumed it, and because the group did
 * not wrap, the thing pushed past the edge and clipped away was the
 * **control**, not the text. It vanished silently, and it vanished
 * precisely on the longest names — the rows an operator is most likely
 * to want to act on.
 *
 * ## Why `toBeInTheDocument()` is the wrong assertion here
 *
 * `overflow: hidden` clips at paint. It removes nothing from the DOM, it
 * sets no attribute, and jsdom does not lay anything out — so a clipped
 * `+` and a visible one are **byte-identical** to every query
 * `@testing-library` offers. A test asserting the control is *present*
 * would have passed, unchanged, against the exact tree this issue exists
 * to fix. That is the probe-that-cannot-fail shape, and it is worth
 * naming because it is the obvious way to write this file.
 *
 * So the assertion is not *present*. It is **which cell it is in**, and
 * that is read two ways that do not share an author:
 *
 *  1. **From the DOM** — the `+` is a descendant of the grid cell that
 *     holds the device control, and is not a descendant of the one that
 *     holds the name anchor. Rendered on a row whose hostname is 96
 *     characters, five times the `12rem` the name column is guaranteed.
 *  2. **From the stylesheet on disk** — the track that cell occupies,
 *     counted by the cell's own index among the row's children, is
 *     `max-content`: a column sized to what it holds, which cannot run
 *     out of room for it. The name column, at the index the name cell
 *     actually occupies, is the `minmax(12rem, 1fr)` that could.
 *
 * Neither reading stands in for the other. (1) alone would keep passing
 * if the device column were later given a `minmax()` bound; (2) alone
 * says nothing about where the control was rendered. Together they say
 * the control is in a column that cannot clip it — which is what
 * "hittable at any hostname length" means when there is no layout
 * engine to ask.
 *
 * ## What is deliberately *not* re-asserted here
 *
 * That the `+` presets the device end to end through the address bar.
 * `boardAffordance.test.tsx` (#128) drives that across all four files
 * and must stay green — moving a control and losing its meaning is not
 * a fix. What this file adds on that front is the *long-name* row: the
 * href is read off the DOM and asserted to carry `for=<device id>`
 * there too, because the row under test is a different row from #128's.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, screen, within } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { queryClient } from '../queryClient';
import { board, device as boardDevice, hostname } from './fixtures';

const TENANT: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [
    'atrium_ddns.device.manage',
    'atrium_ddns.domain.manage',
    'atrium_ddns.hostname.manage',
  ],
  impersonating_from: null,
};

const DEVICE = { id: 7, name: 'home-router' };

/** 96 characters, five labels. The name column is guaranteed `12rem`
 *  — 192px, roughly 27 characters at the table's size — and takes the
 *  grid's slack beyond that, so this is a name that overflows any
 *  plausible viewport. The old markup put the `+` *after* this string
 *  inside a single `overflow: hidden` cell. */
const LONG_NAME =
  'a-very-long-hostname-that-runs-past-the-column.' +
  'and-keeps-going.for-a-while.longer.example.net';

/** The short row, kept beside it. Two rows, so "the `+` is in the device
 *  cell" is not an accident of the only row on the board, and so the
 *  placement is asserted to be the same at both lengths rather than
 *  special-cased for long ones. */
const SHORT_NAME = 'host-a.example.net';

const BOARD = board({
  devices: [
    boardDevice({
      id: DEVICE.id,
      name: DEVICE.name,
      hostnames: [
        hostname({ id: 1, name: LONG_NAME, device_id: DEVICE.id }),
        hostname({ id: 2, name: SHORT_NAME, device_id: DEVICE.id }),
      ],
    }),
  ],
});

let handles: MockAtriumHandles;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  queryClient.clear();
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(BOARD);
      if (url.endsWith('/atrium_ddns/devices')) return json([]);
      if (url.endsWith('/atrium_ddns/domains')) return json([]);
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: [] });
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------
// Instrument 2: the stylesheet, read off disk.
//
// Read with `readFileSync` and not `import '../ddns.css?raw'` for
// `design.test.ts`'s measured reason: vitest defaults to `css: false`
// and stubs CSS modules, so the `?raw` specifier resolves to the empty
// string and every regex below matches nothing — six assertions that
// cannot fail, in the file whose job is to make one fail. The vacuity
// test at the bottom of this section is what catches that.
// ---------------------------------------------------------------------
const CSS = readFileSync(resolve(process.cwd(), 'src/ddns.css'), 'utf8');

/** The declared tracks of `.ddns-boardtable`, in order.
 *
 * Split on whitespace *outside* parentheses, so `minmax(12rem, 1fr)`
 * survives as one token. A naive `.split(/\s+/)` yields `minmax(12rem,`
 * and `1fr)` and silently shifts every index after column three — which
 * would make the assertions below compare the wrong tracks and still
 * produce a plausible-looking pass. */
function boardColumns(): string[] {
  const block = CSS.match(
    /\[data-ddns-root\]\s*\.ddns-boardtable\s*\{([\s\S]*?)\}/,
  )?.[1];
  const value = block?.match(/grid-template-columns:\s*([^;]+);/)?.[1];
  if (!value) return [];
  const tokens: string[] = [];
  let depth = 0;
  let current = '';
  for (const ch of value) {
    if (ch === '(') depth += 1;
    if (ch === ')') depth -= 1;
    if (depth === 0 && /\s/.test(ch)) {
      if (current) tokens.push(current);
      current = '';
    } else {
      current += ch;
    }
  }
  if (current) tokens.push(current);
  // The declaration is written across two source lines and `minmax()`
  // carries an inner space, so tokens are normalised rather than
  // compared to whatever whitespace the author happened to type.
  return tokens.map((t) => t.replace(/\s+/g, ' '));
}

/** The rule that does the clipping, as the stylesheet declares it. */
function rowChildRule(): string {
  return (
    CSS.match(
      /\[data-ddns-root\]\s*\.ddns-boardtable__row\s*>\s*\*\s*\{([\s\S]*?)\}/,
    )?.[1] ?? ''
  );
}

/** The grid cell an element sits in: the ancestor that is a *direct*
 *  child of the row, because that is what `grid-template-columns:
 *  subgrid` places and what `.ddns-boardtable__row > *` styles. Returns
 *  the element and its zero-based track index.
 *
 *  Not `element.parentElement` — the control may legitimately be nested
 *  inside a `Group` within the cell, and a test that assumed one level
 *  would fail on a correct implementation for the wrong reason. */
function cellOf(row: HTMLElement, el: HTMLElement) {
  const children = Array.from(row.children) as HTMLElement[];
  const cell = children.find((child) => child === el || child.contains(el));
  if (!cell) throw new Error('element is not inside this row at all');
  return { cell, index: children.indexOf(cell) };
}

describe('the instruments themselves', () => {
  test('the stylesheet was read, and both rules were found', () => {
    // Ask what this file would print if the thing it measures were
    // absent. Without this, an empty `CSS` makes every track lookup
    // return `undefined` and every `expect(...).toBe('max-content')`
    // fails loudly — but the *rule* checks below would pass on ''.
    expect(CSS.length).toBeGreaterThan(2000);
    expect(boardColumns()).toHaveLength(11);
    expect(rowChildRule()).not.toBe('');
  });

  test('the premise: row children are clipped, not wrapped', () => {
    // The mechanism the placement is a response to. If this ever stops
    // being true the argument in this file's header changes, and
    // whoever changed it should read it rather than inherit it.
    const rule = rowChildRule();
    expect(rule).toMatch(/overflow:\s*hidden/);
    expect(rule).toMatch(/white-space:\s*nowrap/);
  });

  test('the long name really does overflow the column it used to share', () => {
    // `12rem` is the name column's guaranteed minimum. A "long" name
    // shorter than that would make the whole file a test of nothing —
    // the same defect one level up from the assertions.
    const name = boardColumns()[2];
    expect(name).toBe('minmax(12rem, 1fr)');
    expect(LONG_NAME.length).toBeGreaterThan(90);
  });
});

describe('the add-a-name `+` is in the device cell, not the name cell — #154', () => {
  async function openBoard() {
    renderWithAtrium(<DeviceBoardPage />);
    return (await screen.findByTestId('board-table')) as HTMLElement;
  }

  test.each([
    ['a hostname that overflows its column', LONG_NAME],
    ['a hostname that fits', SHORT_NAME],
  ])('on %s', async (_label, name) => {
    await openBoard();
    const row = screen.getByTestId(`board-row-${name}-AAAA`);

    const plus = within(row).getByTestId(`board-add-name-${name}`);
    const deviceControl = within(row).getByTestId(`board-open-${DEVICE.name}`);
    const nameAnchor = within(row).getByText(name);

    const plusCell = cellOf(row, plus);
    const deviceCell = cellOf(row, deviceControl);
    const nameCell = cellOf(row, nameAnchor);

    // --- reading 1: the DOM. Which cell, by containment. ---
    expect(
      plusCell.cell,
      'the add-a-name `+` is not in the same grid cell as the device ' +
        'control. Adding a name is an action on the *device* — it presets ' +
        `?for=${DEVICE.id} — and it is the device column that is sized to ` +
        'its contents. Beside the name it sat in a `minmax(12rem, 1fr)` ' +
        'cell with `overflow: hidden`, where a long name pushed it past ' +
        'the edge and it was clipped away rather than the text truncating.',
    ).toBe(deviceCell.cell);
    expect(
      plusCell.cell,
      'the add-a-name `+` is back in the name cell, which is the cell ' +
        'that runs out of room. See the header of this file.',
    ).not.toBe(nameCell.cell);
    // And the two cells really are different cells, so the assertion
    // above is a discrimination rather than a tautology about a row
    // that renders everything into one box.
    expect(deviceCell.index).not.toBe(nameCell.index);

    // --- reading 2: the stylesheet. Which track that cell occupies. ---
    const columns = boardColumns();
    expect(
      columns[plusCell.index],
      'the add-a-name control sits in grid track ' +
        `${plusCell.index + 1}, declared "${columns[plusCell.index]}". A ` +
        'bounded track can run out of room for it, which is the whole ' +
        'defect. It belongs in a max-content column.',
    ).toBe('max-content');
    expect(columns[nameCell.index]).toBe('minmax(12rem, 1fr)');

    // --- and it still means what it meant: the device is preset. ---
    // Read off the DOM, never recomputed: a component calling
    // `boardNameNewHref()` with no argument keeps every other assertion
    // in this file green and ships the defect #128 was opened for.
    expect(plus.getAttribute('href')).toBe(
      `/atrium-ddns?name=new&for=${DEVICE.id}`,
    );
  });

  test('the name cell holds the name and nothing else', async () => {
    // The other half of the move. The name column is the one with the
    // ellipsis on it, and an ellipsis is only useful if the thing being
    // truncated is the text. A control left behind in there is a
    // control competing with the name for the space.
    await openBoard();
    const row = screen.getByTestId(`board-row-${LONG_NAME}-AAAA`);
    const nameCell = cellOf(row, within(row).getByText(LONG_NAME)).cell;

    expect(
      within(nameCell).queryAllByRole('link', { name: /add a name/i }),
      'an add-a-name control is still inside the name cell',
    ).toHaveLength(0);
    expect(
      within(nameCell).queryAllByRole('button', { name: /add a name/i }),
    ).toHaveLength(0);
    expect(nameCell).toHaveTextContent(LONG_NAME);
  });

  test('the "no names yet" row keeps its own add control', async () => {
    // Explicitly in the issue's "done when", and it is a different
    // branch of `flatten` with its own call site — so it can be broken
    // while everything above stays green.
    const NAMELESS = { id: 12, name: 'garage-pi' };
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (url.endsWith('/users/me/context')) return json(TENANT);
        if (url.includes('/atrium_ddns/board'))
          return json(
            board({
              devices: [
                boardDevice({
                  id: NAMELESS.id,
                  name: NAMELESS.name,
                  hostnames: [],
                }),
              ],
            }),
          );
        return json([]);
      }),
    );

    await openBoard();
    const row = screen.getByTestId(`board-row-device-${NAMELESS.name}`);
    const add = within(row).getByTestId(`board-add-name-for-${NAMELESS.name}`);
    expect(add.getAttribute('href')).toBe(
      `/atrium-ddns?name=new&for=${NAMELESS.id}`,
    );
  });
});
