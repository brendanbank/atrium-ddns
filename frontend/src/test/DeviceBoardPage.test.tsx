/** The board and the strip, under `@brendanbank/atrium-test-utils`.
 *
 * What this file is written to prove, in the order the acceptance
 * criteria list it:
 *
 * 1. **Permission gating in both directions.** A holder of
 *    `atrium_ddns.device.manage` sees the board; a user without it sees
 *    a *refusal*, not an empty board — and the refusal does not fetch.
 * 2. **`n/a` is not `0`.** Never-checked, check-failed and no-record are
 *    three renderings with three different words and three different
 *    rail verdicts, asserted as a set of three rather than as three
 *    separate equalities.
 * 3. **The lower joint does not fire on a NAT'd client**, and the label
 *    says why.
 * 4. **Light and dark are both correct**, keyed on
 *    `[data-mantine-color-scheme]` and never on a JS-resolved scheme.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { BOARD_PERMISSION, type Board } from '../api/board';
import { queryClient } from '../queryClient';
import { board, device, hostname, strip, V6_A, V6_B } from './fixtures';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [BOARD_PERMISSION],
  impersonating_from: null,
};

const OUTSIDER: UserContext = {
  id: 2,
  email: 'outsider@example.com',
  full_name: 'Outsider',
  is_active: true,
  roles: [],
  // Deliberately holds *other* atrium_ddns permissions. A gate spelled
  // "holds any atrium_ddns code" passes with an empty list and fails
  // here, which is the point.
  permissions: ['atrium_ddns.domain.manage', 'atrium_ddns.hostname.manage'],
  impersonating_from: null,
};

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;
let boardPayload: Board = board();
let boardFetches = 0;

function stubFetch() {
  boardFetches = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/users/me/context')) {
        if (!currentMe) return new Response(null, { status: 401 });
        return new Response(JSON.stringify(currentMe), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/atrium_ddns/board')) {
        boardFetches += 1;
        return new Response(JSON.stringify(boardPayload), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200 });
    }),
  );
}

beforeEach(() => {
  stubFetch();
  boardPayload = board();
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  queryClient.clear();
  currentMe = null;
  document.documentElement.removeAttribute('data-mantine-color-scheme');
  vi.unstubAllGlobals();
});

function renderBoard(me: UserContext) {
  currentMe = me;
  handles = mockAtriumRegistry({ me });
  return renderWithAtrium(<DeviceBoardPage />);
}

describe('permission gating', () => {
  test('a holder of atrium_ddns.device.manage sees the board', async () => {
    renderBoard(OPERATOR);
    expect(await screen.findByTestId('board')).toBeInTheDocument();
    expect(screen.queryByTestId('board-refused')).not.toBeInTheDocument();
    // Non-vacuous: the positive half has to actually carry a device.
    expect(screen.getByTestId('device-home-router')).toBeInTheDocument();
  });

  test('a user without it is refused, and is not told the board is empty', async () => {
    renderBoard(OUTSIDER);
    expect(await screen.findByTestId('board-refused')).toBeInTheDocument();
    expect(screen.queryByTestId('board')).not.toBeInTheDocument();
    // The distinction the whole surface is built around: a refusal must
    // not render as "You have no devices yet", which is a claim about
    // the account that is not true.
    expect(screen.queryByTestId('board-empty')).not.toBeInTheDocument();
    expect(screen.getByText(/refusal, not an empty board/i)).toBeInTheDocument();
  });

  test('a refused user does not fetch the board at all', async () => {
    renderBoard(OUTSIDER);
    await screen.findByTestId('board-refused');
    await waitFor(() => expect(boardFetches).toBe(0));

    // The control, same assertion the other way: with the permission the
    // fetch does happen. Without this pair, `boardFetches === 0` also
    // passes when the URL never matched the stub.
    cleanup();
    handles.cleanup();
    queryClient.clear();
    renderBoard(OPERATOR);
    await screen.findByTestId('board');
    expect(boardFetches).toBe(1);
  });
});

describe('the three states that share a null address', () => {
  test('never-checked, check-failed and no-record render three ways', async () => {
    boardPayload = board({
      devices: [
        device({
          hostnames: [
            hostname({
              id: 1,
              name: 'never.example.net',
              strips: [
                strip({
                  answered: {
                    address: null,
                    status: 'never_checked',
                    checked_at: null,
                    error: null,
                  },
                  upper_joint: 'not_measured_never',
                  joints_agreed: 1,
                  joints_compared: 1,
                  joints_unmeasured: 1,
                  collapsible: false,
                }),
              ],
            }),
            hostname({
              id: 2,
              name: 'failed.example.net',
              strips: [
                strip({
                  answered: {
                    address: null,
                    status: 'error',
                    checked_at: '2026-08-15T14:02:00Z',
                    error: 'AAAA: resolver timed out',
                  },
                  upper_joint: 'not_measured_failed',
                  joints_agreed: 1,
                  joints_compared: 1,
                  joints_unmeasured: 1,
                  collapsible: false,
                }),
              ],
            }),
            hostname({
              id: 3,
              name: 'norecord.example.net',
              strips: [
                strip({
                  answered: {
                    address: null,
                    status: 'missing',
                    checked_at: '2026-08-15T14:02:00Z',
                    error: null,
                  },
                  upper_joint: 'diverged',
                  joints_agreed: 1,
                  joints_compared: 2,
                  collapsible: false,
                }),
              ],
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    const cells = screen
      .getAllByTestId('answered-address')
      .map((node) => node.textContent);
    // Three states, three strings, and none of them is `0`, `-`,
    // `0.0.0.0` or `::`. A bare dash is ambiguous across all three, and
    // the two zero-addresses are valid addresses.
    expect(new Set(cells)).toEqual(new Set(['n/a', 'unmeasured', 'no record']));
    for (const text of cells) {
      expect(['-', '—', '0', '0.0.0.0', '::']).not.toContain(text);
    }

    // …and three different rails. The pair that has to stay apart is
    // never-checked (dotted, grey) and no-record (solid, accent): both
    // carry a null address and a null error, and differ only by whether
    // the check has ever run.
    expect(screen.getByTestId('rail-joint-not_measured_never')).toBeInTheDocument();
    expect(screen.getByTestId('rail-joint-not_measured_failed')).toBeInTheDocument();
    expect(screen.getByTestId('rail-joint-diverged')).toBeInTheDocument();

    // The tone is the third channel. `error` is deliberately quiet: a
    // resolver timeout is a fact about our instrument, not about the
    // tenant's DNS, and painting it like a real divergence tells an
    // operator their DNS is broken when their resolver is.
    const byText = Object.fromEntries(
      screen
        .getAllByTestId('answered-address')
        .map((node) => [node.textContent, node.getAttribute('data-tone')]),
    );
    expect(byText['n/a']).toBe('quiet');
    expect(byText['unmeasured']).toBe('quiet');
    expect(byText['no record']).toBe('diverge');

    // And the resolver's own words reach the reader.
    expect(screen.getByText('AAAA: resolver timed out')).toBeInTheDocument();
  });

  test('a never-called device shows a dash, not a zero', async () => {
    boardPayload = board({
      devices: [
        device({
          name: 'garage-nas',
          liveness: 'never_seen',
          marked: true,
          last_seen_at: null,
          last_response_code: null,
          updates_in_window: null,
          updates_display: '—',
          hostnames: [],
        }),
        device({
          id: 2,
          name: 'office-router',
          liveness: 'idle',
          marked: false,
          updates_in_window: 0,
          updates_display: '0',
          hostnames: [],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    // Three facts, three strings. A renderer that formatted
    // `updates_in_window` directly prints `null` for the first or, worse,
    // coerces it to `0` — and a device nobody has ever heard from
    // becomes indistinguishable from one that is merely quiet.
    expect(screen.getByTestId('device-garage-nas-updates')).toHaveTextContent('—');
    expect(screen.getByTestId('device-office-router-updates')).toHaveTextContent('0');
    expect(screen.getByTestId('device-garage-nas-last-seen')).toHaveTextContent(
      'never',
    );
    // Not an epoch-derived age. `now - 0` is fifty-six years.
    expect(
      screen.getByTestId('device-garage-nas-last-seen').textContent,
    ).not.toMatch(/1970|56 |20\d\d\d d/);
  });
});

describe('the lower joint', () => {
  test('a declared myip is not applicable, and the label says why', async () => {
    boardPayload = board({
      devices: [
        device({
          hostnames: [
            hostname({
              strips: [
                strip({
                  family: 'A',
                  published: {
                    address: '203.0.113.7',
                    updated_at: '2026-08-15T13:47:00Z',
                  },
                  answered: {
                    address: '203.0.113.7',
                    status: 'ok',
                    checked_at: '2026-08-15T14:02:00Z',
                    error: null,
                  },
                  called_from: {
                    address: '198.51.100.4',
                    seen_at: '2026-08-15T13:47:00Z',
                    reason: 'declared_myip',
                    declared_address: '203.0.113.7',
                  },
                  lower_joint: 'not_applicable',
                  joints_agreed: 1,
                  joints_compared: 1,
                  joints_not_applicable: 1,
                  collapsible: false,
                }),
              ],
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    // No segment drawn — the two addresses differ permanently and
    // correctly, so an indicator here would be on forever.
    expect(screen.getByTestId('rail-joint-not_applicable')).toBeInTheDocument();
    expect(screen.queryByTestId('rail-joint-diverged')).not.toBeInTheDocument();
    // The reader learns *why* nothing is being compared rather than
    // assuming a bug.
    expect(screen.getByText('called from (declared myip)')).toBeInTheDocument();
    expect(screen.getByText(/declares 203\.0\.113\.7/)).toBeInTheDocument();
    // The address shown is where it called from, not what it claimed.
    expect(screen.getByTestId('called-from-address')).toHaveTextContent(
      '198.51.100.4',
    );
    expect(screen.getByTestId('called-from-address')).toHaveAttribute(
      'data-tone',
      'ink',
    );
  });

  test('a device that moved does diverge, with the groups underlined', async () => {
    boardPayload = board({
      devices: [
        device({
          hostnames: [
            hostname({
              strips: [
                strip({
                  called_from: {
                    address: V6_B,
                    seen_at: '2026-08-15T14:00:00Z',
                    reason: 'evaluated',
                    declared_address: null,
                  },
                  lower_joint: 'diverged',
                  joints_agreed: 1,
                  joints_compared: 2,
                  collapsible: false,
                }),
              ],
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    const cell = screen.getByTestId('called-from-address');
    expect(cell).toHaveAttribute('data-tone', 'diverge');
    expect(screen.getByTestId('rail-joint-diverged')).toBeInTheDocument();

    // Exactly one group differs between V6_A and V6_B — the last — and
    // only that one is marked. Colour is not the only channel: in dark
    // the accent and the ink differ by 1.42:1, so a greyscale render
    // shows them as one tone and the underline is what survives.
    const marked = Array.from(cell.querySelectorAll('.ddns-group')).filter(
      (node) => node.getAttribute('data-differs') === 'true',
    );
    expect(marked).toHaveLength(1);
    expect(marked[0].textContent).toBe('0099');
    expect(V6_A.split(':').length).toBe(8);
  });
});

describe('collapse', () => {
  test('a device with nothing wrong is one line — page height is an instrument', async () => {
    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    // §3.4: "A tenant with nothing wrong has a short page. A tenant with
    // three broken names has a page three strips long. You can tell how
    // bad it is from the scrollbar." That only holds if a healthy device
    // does not draw its strips.
    expect(
      screen.queryByTestId('strip-collapsed-host-a.example.net-AAAA'),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId('device-home-router')).toBeInTheDocument();
  });

  test('an agreeing strip collapses and names its denominator', async () => {
    renderBoard(OPERATOR);
    await screen.findByTestId('board');
    fireEvent.click(screen.getByRole('button', { name: /home-router/ }));

    const collapsed = screen.getByTestId('strip-collapsed-host-a.example.net-AAAA');
    // `; agrees` on its own would be a ratio with the divisor hidden,
    // and the divisor moves — 2 for a device publishing its own address,
    // 1 for one declaring myip.
    expect(collapsed).toHaveTextContent('2 of 2 agree');

    // …and it opens. Collapsed is a local state, never a way to lose the
    // detail.
    fireEvent.click(collapsed);
    expect(
      screen.getByTestId('strip-host-a.example.net-AAAA'),
    ).toBeInTheDocument();
  });

  test('the device detail names its rate limit, and which of three states it is', async () => {
    // #73's AC 4 — the stored value is displayed wherever a device is
    // shown. In the detail rather than in the line: the board's four
    // columns are a status grid and §4 spends its boldness on the
    // strip. Named here so that placement is a decision on the record
    // and not something a later reader has to infer from its absence.
    renderBoard(OPERATOR);
    await screen.findByTestId('board');
    fireEvent.click(screen.getByRole('button', { name: /home-router/ }));

    expect(screen.getByTestId('device-home-router-limit')).toHaveTextContent(
      '30/min, inherited',
    );
  });

  test('a muted device says muted rather than showing a zero with a unit', async () => {
    boardPayload = board({
      devices: [
        device({
          rate_limit_per_minute: 0,
          effective_rate_limit_per_minute: 0,
        }),
      ],
    });
    renderBoard(OPERATOR);
    await screen.findByTestId('board');
    fireEvent.click(screen.getByRole('button', { name: /home-router/ }));
    expect(screen.getByTestId('device-home-router-limit')).toHaveTextContent(
      'muted — may never call',
    );
  });

  test('a device carrying a divergence is expanded without being asked', async () => {
    boardPayload = board({
      devices: [
        device({
          hostnames: [
            hostname({
              strips: [strip({ upper_joint: 'diverged', collapsible: false })],
            }),
          ],
        }),
      ],
    });
    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    // Nothing may hide a divergence by default — the collapsed shape is
    // *defined* as "agrees", so letting a diverged strip render in it,
    // or behind a closed parent, would make the shape a lie.
    expect(
      screen.getByTestId('strip-host-a.example.net-AAAA'),
    ).toBeInTheDocument();
  });

  test('a strip with any other verdict is expanded by default', async () => {
    boardPayload = board({
      devices: [
        device({
          hostnames: [
            hostname({
              strips: [strip({ upper_joint: 'diverged', collapsible: false })],
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    expect(screen.getByTestId('strip-host-a.example.net-AAAA')).toHaveAttribute(
      'data-diverged',
      'true',
    );
    expect(
      screen.queryByTestId('strip-collapsed-host-a.example.net-AAAA'),
    ).not.toBeInTheDocument();
  });
});

describe('the colour scheme', () => {
  test.each(['light', 'dark'] as const)(
    'renders the same DOM under [data-mantine-color-scheme="%s"]',
    async (scheme) => {
      document.documentElement.setAttribute('data-mantine-color-scheme', scheme);
      renderBoard(OPERATOR);
      await screen.findByTestId('board');

      // The host subtree is present and scoped, so the CSS in ddns.css
      // — which is the only thing that differs between the two schemes —
      // has something to hang off.
      const root = document.querySelector('[data-ddns-root]');
      expect(root).not.toBeNull();
      expect(root!.querySelector('[data-testid="board"]')).not.toBeNull();

      // The host's nested MantineProvider must not have rewritten
      // atrium's own attribute on `<html>`. `getRootElement={() =>
      // undefined}` is what stops it; without that prop Mantine writes
      // `data-mantine-color-scheme` onto `document.documentElement` on
      // mount, from inside the host's provider.
      expect(
        document.documentElement.getAttribute('data-mantine-color-scheme'),
      ).toBe(scheme);

      // And nothing was written to `:root`. `withCssVariables={false}`;
      // the one style tag the provider does emit is `MantineClasses`,
      // which carries only `hiddenFrom`/`visibleFrom` media queries.
      expect(
        document.querySelectorAll('style[data-mantine-styles="true"]'),
      ).toHaveLength(0);
    },
  );

  test('the rendered markup does not branch on the scheme', async () => {
    document.documentElement.setAttribute('data-mantine-color-scheme', 'light');
    renderBoard(OPERATOR);
    const light = (await screen.findByTestId('board')).innerHTML;

    cleanup();
    handles.cleanup();
    queryClient.clear();

    document.documentElement.setAttribute('data-mantine-color-scheme', 'dark');
    renderBoard(OPERATOR);
    const dark = (await screen.findByTestId('board')).innerHTML;

    // Identical markup in both schemes is the assertion, not a
    // coincidence: it means every colour decision is made in CSS against
    // the attribute atrium owns, and none of them is made in JS against
    // a scheme value that `use-provider-color-scheme.mjs` can lag.
    expect(dark).toBe(light);
  });
});

describe('empty states', () => {
  test('no devices is an invitation, not a blank panel', async () => {
    boardPayload = board({ devices: [], unassigned_hostnames: [] });
    renderBoard(OPERATOR);
    expect(await screen.findByTestId('board-empty')).toHaveTextContent(
      /Add one to get a DDNS username and password/,
    );
  });

  test('the health-check interval comes from the payload, not the string', async () => {
    boardPayload = board({
      health_check_interval_minutes: 45,
      window_days: 30,
      devices: [
        device({
          hostnames: [
            hostname({
              strips: [
                strip({
                  answered: {
                    address: null,
                    status: 'never_checked',
                    checked_at: null,
                    error: null,
                  },
                  upper_joint: 'not_measured_never',
                  collapsible: false,
                }),
              ],
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board');

    // An operator who changes the interval must not be able to make this
    // sentence wrong, and the same for the column head's denominator.
    expect(screen.getByTestId('board-never-checked')).toHaveTextContent(
      'runs every 45 minutes',
    );
    expect(screen.getByTestId('board-updates-head')).toHaveTextContent(
      'updates / 30 d',
    );
  });

  test('an unassigned hostname is listed rather than hidden', async () => {
    boardPayload = board({
      devices: [],
      unassigned_hostnames: [
        hostname({
          id: 9,
          name: 'orphan.example.net',
          device_id: null,
          strips: [
            strip({
              called_from: {
                address: null,
                seen_at: null,
                reason: 'no_device',
                declared_address: null,
              },
              lower_joint: 'not_applicable',
              joints_compared: 1,
              joints_agreed: 1,
              joints_not_applicable: 1,
              collapsible: false,
            }),
          ],
        }),
      ],
    });

    renderBoard(OPERATOR);
    await screen.findByTestId('board-unassigned');
    expect(
      screen.getByText('called from — no device assigned'),
    ).toBeInTheDocument();
    // The board is not empty: it has no devices and one orphaned name,
    // which is a different fact and must not read as "add a device".
    expect(screen.queryByTestId('board-empty')).toBeNull();
  });
});

describe('the on-demand health-check actions (#75, ui-parity §3.3 G3)', () => {
  /** A run summary in the server's shape. Defaults are a clean, small
   *  sweep; each test overrides only the fields it is about. */
  function runSummary(overrides: Record<string, unknown> = {}) {
    return {
      enabled: true,
      forced: true,
      hostnames_considered: 4,
      hostnames_never_written: 1,
      hostnames_checked: 3,
      records_checked: 4,
      ok: 3,
      mismatch: 0,
      missing: 1,
      error: 0,
      transitions: 1,
      truncated: false,
      batch_size: 200,
      ...overrides,
    };
  }

  /** Replaces the module-level stub so a POST can be scripted. Returns
   *  the recorder for the requests that actually left. */
  function stubWith(
    responder: (url: string, init?: RequestInit) => Response,
  ): { url: string; method: string }[] {
    const sent: { url: string; method: string }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        if (method !== 'GET') sent.push({ url, method });
        if (url.endsWith('/users/me/context')) {
          return new Response(JSON.stringify(currentMe), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        if (url.endsWith('/atrium_ddns/board') && method === 'GET') {
          return new Response(JSON.stringify(boardPayload), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        return responder(url, init);
      }),
    );
    return sent;
  }

  function json(body: unknown, status = 200) {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  test('the cadence sentence quotes the board payload, not a constant', async () => {
    // The button exists because of a wait, and the wait is
    // `health_check_interval_minutes`. Rendering a hardcoded 15 would be
    // right today and silently wrong after an operator changed it — the
    // "derive, don't hardcode" rule aimed at a sentence.
    boardPayload = { ...board(), health_check_interval_minutes: 45 };
    renderBoard(OPERATOR);
    expect(await screen.findByTestId('health-check-cadence')).toHaveTextContent(
      'every 45 minutes',
    );
  });

  test('a run posts once and reports every denominator it was given', async () => {
    const sent = stubWith(() => json(runSummary()));
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));

    await waitFor(() =>
      expect(screen.getByTestId('health-check-ran')).toBeInTheDocument(),
    );
    expect(sent).toEqual([
      { url: '/api/atrium_ddns/health-checks/run', method: 'POST' },
    ]);
    const text = screen.getByTestId('health-check-ran').textContent ?? '';
    // The population, the slice that could not be checked, and the
    // verdicts — not just "done".
    expect(text).toContain('3 of 4 names checked');
    expect(text).toContain('1 never published');
    expect(text).toContain('1 changed');
  });

  test('a run that resolved nothing says why, and does not read as clean', async () => {
    // `0 checked` over a population of 2 that has never published is a
    // measurement. Rendering it as "checked" with no numbers would be
    // the same string a healthy sweep produces.
    stubWith(() =>
      json(
        runSummary({
          hostnames_considered: 2,
          hostnames_never_written: 2,
          hostnames_checked: 0,
          records_checked: 0,
          ok: 0,
          missing: 0,
          transitions: 0,
        }),
      ),
    );
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-ran')).toHaveTextContent(
        'nothing to check: 0 of 2 names has published an address',
      ),
    );
  });

  test('a run against a disabled check is a refusal, not an empty sweep', async () => {
    stubWith(() =>
      json(
        runSummary({
          enabled: false,
          hostnames_considered: 0,
          hostnames_never_written: 0,
          hostnames_checked: 0,
          records_checked: 0,
          ok: 0,
          missing: 0,
          transitions: 0,
        }),
      ),
    );
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-ran')).toHaveTextContent(
        'switched off',
      ),
    );
  });

  test('the batch ceiling is said out loud when it was reached', async () => {
    stubWith(() => json(runSummary({ truncated: true, batch_size: 200 })));
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-ran')).toHaveTextContent(
        'stopped at the 200-name batch',
      ),
    );
  });

  test('a 429 is the debounce, and is not rendered as a failure', async () => {
    // The whole reason `ApiError` carries a numeric status. Rendering
    // "that did not work" here would send an operator looking for a
    // fault in their DNS when the server simply refused to repeat work
    // it did a moment ago.
    stubWith(() =>
      json(
        {
          detail:
            'a manual health check was run less than 60s ago; 42s remaining.',
        },
        429,
      ),
    );
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-debounced')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('health-check-error')).toBeNull();
    // The server's own words, including the seconds remaining.
    expect(screen.getByTestId('health-check-debounced')).toHaveTextContent(
      '42s remaining',
    );
  });

  test('a real failure is still a failure, with the diagnosis intact', async () => {
    // The control for the test above: without it, a component that
    // rendered *every* error as "already just checked" would pass.
    stubWith(() => json({ detail: 'no such thing' }, 500));
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-run'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-error')).toHaveTextContent(
        'no such thing',
      ),
    );
    expect(screen.queryByTestId('health-check-debounced')).toBeNull();
  });

  test('clear reports its denominator and says the log is untouched', async () => {
    const sent = stubWith(() => json({ cleared: 3, in_scope: 4 }));
    renderBoard(OPERATOR);
    fireEvent.click(await screen.findByTestId('health-check-clear'));
    await waitFor(() =>
      expect(screen.getByTestId('health-check-cleared')).toBeInTheDocument(),
    );
    expect(sent).toEqual([
      { url: '/api/atrium_ddns/health-checks/clear', method: 'POST' },
    ]);
    const text = screen.getByTestId('health-check-cleared').textContent ?? '';
    expect(text).toContain('3 of 4 names');
    // `POST /admin/events/clear` is the route the operator struck; this
    // one is not it, and the interface says so rather than leaving an
    // operator to wonder what a "clear" removed.
    expect(text).toContain('the log is untouched');
  });

  test('the actions are absent for a user who cannot read the board', async () => {
    // They post to an endpoint gated on the same permission, so
    // offering them would be a button that answers 403.
    renderBoard(OUTSIDER);
    await screen.findByTestId('board-refused');
    expect(screen.queryByTestId('health-check-actions')).toBeNull();
  });
});
