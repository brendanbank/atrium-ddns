/** §16's table, as three assertions that fail if it is undone.
 *
 * The operator reported *"I still cannot edit the zone"* twice, from two
 * surfaces. `docs/ops/ui-design.md` §16 tabulates **three different
 * causes with one appearance**, which is why fixing one would have left
 * the report standing:
 *
 * | surface | what the name actually was | why it read as inert |
 * |---|---|---|
 * | the board (`DeviceBoard`) | a bare `<span class="ddns-data">` inside an expand toggle | **not a link at all** |
 * | the zones list (`DomainList`) | `<Anchor href class="ddns-data">` | `.ddns-data` sets `color: var(--ddns-ink)` — Mantine's link colour and underline are cancelled |
 * | the devices list (`DeviceList`) | the same | the same |
 *
 * `design.test.ts` guards the *stylesheet* half — that an interactive
 * `.ddns-data` is underlined, at rest, without spending colour. This
 * file guards the *markup* half, which is the one a stylesheet cannot
 * see: an element has to be a control before a rule for controls can
 * reach it. The board's name was styled correctly and was still a
 * `<span>`.
 *
 * Each test names the surface it is about, so a future regression
 * report says which of the three came back rather than "the affordance
 * test failed".
 *
 * ## What it deliberately does not assert
 *
 * Not the rendered colour or the rendered underline. jsdom does not
 * apply the bundle's stylesheet — `ddns.css` reaches a browser as a
 * runtime `<style>` tag through vite-plugin-css-injected-by-js, and
 * `getComputedStyle` here would report the initial value for every
 * property in it. A test that asked jsdom for `textDecorationLine`
 * would read `""` before the fix and `""` after it: a probe that prints
 * the same string whether or not the thing it measures exists.
 * `tests-e2e/card-affordance.spec.ts` takes that reading in chromium,
 * where there is a real cascade to read it from.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { DevicesPage } from '../DevicesPage';
import { DomainsPage } from '../DomainsPage';
import { DDNS_ROOT_ATTRIBUTE } from '../host/DdnsRoot';
import { deviceHrefParam, zoneHrefParam } from '../paths';
import { queryClient } from '../queryClient';
import { board, device as boardDevice } from './fixtures';

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

const DEVICE_ROW = {
  id: 7,
  name: 'home-router',
  username: 'ddns-a1b2c3d4e5f6',
  created_at: '2026-08-15T10:00:00Z',
  last_seen_at: '2026-08-15T13:47:00Z',
  rate_limit_per_minute: null,
  effective_rate_limit_per_minute: 30,
  credential_origin: 'issued',
  hostname_count: 0,
};

const ZONE_ROW = {
  id: 11,
  name: 'example.invalid',
  hostname_count: 0,
  backends: [
    {
      id: 3,
      backend_type: 'route53',
      config: {},
      credentials_set: true,
      known_service: true,
    },
  ],
};

let handles: MockAtriumHandles;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  queryClient.clear();
  handles = mockAtriumRegistry();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) {
        return json(
          board({ devices: [boardDevice({ id: 7, hostnames: [] })] }),
        );
      }
      if (url.endsWith('/atrium_ddns/devices')) return json([DEVICE_ROW]);
      if (url.endsWith('/atrium_ddns/domains')) return json([ZONE_ROW]);
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: [] });
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  vi.unstubAllGlobals();
});

describe('the zones list — §16, row 2', () => {
  test('the zone name is an anchor with a real href, and it keeps the data face', async () => {
    renderWithAtrium(<DomainsPage />);
    const name = await screen.findByTestId(`open-domain-${ZONE_ROW.name}`);

    // A destination, not a `<div onClick>`: §17 keeps §12's two
    // surviving arguments, and both are properties of the URL. A modal
    // that replaced the anchor would take copy-link and Back with it.
    expect(name.tagName).toBe('A');
    expect(name).toHaveAttribute('href', zoneHrefParam(ZONE_ROW.id));

    // …and it is still `.ddns-data`. This is the fix that was
    // explicitly ruled out — dropping the class to get Mantine's link
    // colour back — because it would break §2.3 on the most important
    // string on the page.
    expect(name.className.split(/\s+/)).toContain('ddns-data');
  });
});

describe('the card modal keeps the host’s CSS scope', () => {
  test('the opened card sits inside a data-ddns-root subtree', async () => {
    // Found in chromium, guarded here so it cannot come back silently.
    //
    // Mantine's `Modal` renders through a `<Portal>` into
    // `document.body`, which is *outside* the `data-ddns-root` div
    // `DdnsRoot` mounts. Every selector in `ddns.css` is scoped by that
    // attribute and the six palette values are custom properties on it,
    // so a card in an unscoped portal renders in the body face at the
    // body colour — and a resolution strip renders **with no rail**.
    //
    // jsdom cannot see the *consequence* (there is no cascade here, and
    // that is `card-affordance.spec.ts`'s job), but it can see the
    // *cause*, which is a DOM containment fact and is the thing a later
    // refactor would drop.
    renderWithAtrium(<DomainsPage />);
    fireEvent.click(await screen.findByTestId(`open-domain-${ZONE_ROW.name}`));
    const card = await screen.findByTestId('zone-modal-body');
    expect(
      card.closest(`[${DDNS_ROOT_ATTRIBUTE}]`),
      'the card modal is outside the host bundle’s CSS scope — nothing in ' +
        'ddns.css applies to anything inside it',
    ).not.toBeNull();
  });
});

describe('the devices list — §16, row 3', () => {
  test('the device name is an anchor with a real href, and it keeps the data face', async () => {
    renderWithAtrium(<DevicesPage />);
    const name = await screen.findByTestId(`open-${DEVICE_ROW.name}`);
    expect(name.tagName).toBe('A');
    expect(name).toHaveAttribute('href', deviceHrefParam(DEVICE_ROW.id));
    expect(name.className.split(/\s+/)).toContain('ddns-data');
  });
});

describe('the board — §16, row 1: it was not a link at all', () => {
  test('the device name is a control, not a span', async () => {
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');
    const name = screen.getByTestId(`board-open-${DEVICE_ROW.name}`);

    // The one cause of the three that no stylesheet could have fixed.
    // A `<span>` here passes every CSS guard in `design.test.ts` and is
    // still not reachable by keyboard, by screen reader, or by a click.
    expect(name.tagName).toBe('BUTTON');
    expect(name.className.split(/\s+/)).toContain('ddns-data');
  });

  test('the row has one control, and it is the name', async () => {
    // §18.2. Before this, the whole row was one `<button>` whose job was
    // expand, with the name inert inside it — one tab stop, doing the
    // wrong one of the two things.
    //
    // The disclosure is gone entirely: the board is a flat table with a
    // row per name, so there is nothing left to expand and the name is
    // the row's only control. That is the strongest form of §18.2's
    // finding — the two jobs cannot be confused for one when there is
    // one job.
    renderWithAtrium(<DeviceBoardPage />);
    await screen.findByTestId('board-table');
    const open = screen.getByTestId(`board-open-${DEVICE_ROW.name}`);

    expect(open.tagName).toBe('BUTTON');
    // No disclosure state anywhere on the row: a control announcing
    // "collapsed" to a screen reader user trying to open a device is the
    // defect this test was written for.
    expect(open).not.toHaveAttribute('aria-expanded');
    expect(
      screen.queryByTestId(`device-${DEVICE_ROW.name}-expand`),
    ).not.toBeInTheDocument();
    expect(document.querySelectorAll('[aria-expanded]')).toHaveLength(0);
  });
});
