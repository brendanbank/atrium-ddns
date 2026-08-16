/** The bundle's import-time registrations.
 *
 * `main.tsx` runs its `register*` calls as a side effect of being
 * imported, so a route that is written but not registered is invisible
 * to every component test in this directory — the component renders
 * fine and atrium never mounts it. This is the "a writer nothing calls"
 * shape from `docs/ops/overnight-template.md`, applied to a route.
 *
 * `mockAtriumRegistry` records what the side effect did; the assertions
 * below are on those records, and the path is compared against the
 * module's own exported constant rather than against a literal typed
 * twice.
 */
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

import {
  mockAtriumRegistry,
  type MockAtriumHandles,
} from '@brendanbank/atrium-test-utils';

let handles: MockAtriumHandles;

beforeEach(() => {
  handles = mockAtriumRegistry({ me: null });
  vi.resetModules();
});

afterEach(() => {
  handles?.cleanup();
});

test('the board is registered as a route and a nav item at one path', async () => {
  const main = await import('../main');

  const route = handles.routes.find((entry) => entry.key === 'atrium-ddns-board');
  const nav = handles.navItems.find((entry) => entry.key === 'atrium-ddns-board-nav');

  expect(route, 'the board route was never registered').toBeDefined();
  expect(nav, 'the board nav item was never registered').toBeDefined();
  // One constant, two consumers. A nav item pointing at a path no route
  // serves is a dead link that no component test can see.
  expect(route!.path).toBe(main.BOARD_PATH);
  expect(nav!.to).toBe(main.BOARD_PATH);
});

test('every registered surface still mounts through the wrapper element', async () => {
  await import('../main');

  // Vacuity guard: the sweep has to be over a non-empty population.
  // Ten — the scaffold's four (home widget, demo page, admin tab,
  // profile item), the board's one (#44), #45's two tenant pages, #46's
  // log search, #69's names page, and #75's help page.
  //
  // An exact count rather than a floor, on purpose (#45's argument,
  // kept): a registration added without going through
  // `makeWrapperElement` would slip past `toBeGreaterThan` by simply
  // not being in this list, and the whole point of the sweep is that
  // every surface is covered.
  //
  // #46 arrived wanting to delete the count as "a derived number above
  // a list", on the template's own rule, and was wrong to. The rule is
  // about numbers *nothing else checks*; this one is the only thing
  // holding the sweep's population to the registry's. The cost is a
  // one-line edit per new surface and a failure that reads "expected 9
  // to be 8", so the comment above names what the number is made of —
  // which is what turns that message from a puzzle into an instruction.
  // (#69 was the fourth issue to append here and the message did read as
  // an instruction; #75 was the fifth and read `expected 10 to be 9`,
  // which is the evidence for keeping it.)
  const rendered = [
    ...handles.homeWidgets,
    ...handles.routes,
    ...handles.adminTabs,
    ...handles.profileItems,
  ];
  expect(rendered.length).toBe(10);
  // Keys are the registry's primary key: two registrations sharing one
  // silently replace each other, so the count above would still read 10
  // while one surface never mounts. Added by #46 because three issues
  // have now appended to this file and the fourth will not have read
  // the other three.
  expect(new Set(rendered.map((entry) => entry.key)).size).toBe(
    rendered.length,
  );

  for (const entry of rendered) {
    const render = entry.render;
    expect(typeof render, `${entry.key} has no render()`).toBe('function');
    const element = render!() as { props?: Record<string, unknown> };
    // `makeWrapperElement` produces an atrium-React element whose ref
    // callback mounts the host's own tree. What is checkable from here
    // is that a `ref` is what carries the mount — a registration that
    // returned a host-React element directly would have children
    // instead, and would render under atrium's React copy with none of
    // this bundle's providers.
    expect(typeof element.props?.ref, `${JSON.stringify(entry)}`).toBe(
      'function',
    );
  }
});

test('the nav item for the board is not permission-gated away', async () => {
  await import('../main');
  const nav = handles.navItems.find((entry) => entry.key === 'atrium-ddns-board-nav');
  // Hiding the entry for a user without `atrium_ddns.device.manage`
  // would turn "you may not read this" into "this does not exist". The
  // page renders an explicit refusal instead, which is the same
  // distinction the board makes about a null address.
  expect(nav).not.toHaveProperty('perm');
});
