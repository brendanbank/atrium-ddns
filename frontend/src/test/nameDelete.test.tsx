/** Deleting a name interrupts — it does not appear beside the Save it
 *  competes with. #153.
 *
 * ## What was wrong
 *
 * The confirmation was an inline `Alert` rendered into the Name modal's
 * own body (`NameModal.tsx:502-528` before this change). The result was
 * one surface carrying two sets of buttons and two meanings of
 * "delete": the outer `Delete this name` *opened* the panel, the inner
 * `Delete w3.…` *performed* it, and they sat inches apart reading almost
 * identically. Worse, the form's own `Save` stayed live behind the
 * confirmation, so the surface offered *save* and *destroy* at the same
 * moment with no ordering between them.
 *
 * ## The three readings, and why each is a separate fact
 *
 * A destructive confirmation has to be **its own surface**, it has to
 * **stop the form beneath it**, and it has to **leave nothing behind
 * when dismissed**. Those are three different ways the fix can be got
 * wrong and no one of them implies the others:
 *
 *  1. **It is a second dialog.** Counted by `role="dialog"`, which is
 *     the accessibility tree's own answer to "how many surfaces am I
 *     looking at" — 1 for the inline panel, 2 for the modal. This is
 *     the reading that fails against the inline `Alert`: an `Alert` is
 *     not a dialog no matter what it is titled.
 *  2. **The confirmation is not in the Name modal's body.** `1` is
 *     satisfiable by rendering a modal *and* keeping the panel; this
 *     rules that out by asserting the panel is gone from the subtree it
 *     used to live in. Two instruments on one change, differently
 *     shaped: one counts surfaces, one excludes a subtree.
 *  3. **Save is refused while it is open.** The behaviour the issue
 *     actually names, and the only one of the three that is about what
 *     the product *does* rather than how it is arranged. It is asserted
 *     on `fetch` — no `PATCH` and no `PUT` leaves the browser — rather
 *     than on the button's `disabled` attribute, because a component
 *     agreeing with itself about a prop is not evidence that a request
 *     was not sent. `disabled` is checked too, as the visible half.
 *
 * Plus the round trip: `Keep it` returns to a Name modal with nothing
 * changed and nothing sent, and confirming sends exactly one `DELETE`
 * and closes both.
 *
 * ## Which of these were shown failing, and which were not
 *
 * Run against the inline panel this file is **3 failed / 3 passed**,
 * and the split is deliberate rather than incidental:
 *
 * | test | against the inline panel |
 * |---|---|
 * | a second dialog | **fails** — `expected 1 to be 2` |
 * | not in the Name modal's body | **fails** — the panel is right there |
 * | Save is refused while it is open | **fails** — Save was live behind it |
 * | the sentence, verbatim | passes |
 * | `Keep it` changes nothing | passes |
 * | confirming sends one `DELETE` | passes |
 *
 * The three that pass both ways are not padding and they are not
 * evidence either. They are the half of the behaviour that was already
 * correct and that a move between surfaces is most likely to drop on
 * the floor: the wording, the dismissal, and the request. Saying which
 * is which matters more than the count — a file reported as "six tests
 * for #153" would imply six readings of the defect, and there are
 * three.
 *
 * ## Why it drives `DeviceBoardPage` and not `NameModal`
 *
 * The board's address bar is how a person reaches this modal
 * (`?name=<id>`), and it is what `deviceCard.test.tsx` and
 * `boardAffordance.test.tsx` both do. Rendering `NameModal` directly
 * would skip `DdnsPortalScope` and the query-parameter reader, and
 * would assert about a mount no user can produce — and this issue is
 * specifically about how two modals stack, which is exactly the part a
 * direct mount would not exercise.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { queryClient } from '../queryClient';
import type { CredentialOrigin, Device } from '../api/devices';
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

const NAME_ID = 41;
const NAME = 'w3.example.net';
const ZONE_ID = 11;
const DEVICE_ID = 7;

const ZONE = {
  id: ZONE_ID,
  name: 'example.net',
  hostname_count: 1,
  backends: [],
};

const DEVICES: Device[] = [
  {
    id: DEVICE_ID,
    name: 'home-router',
    username: 'ddns-000000000007',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued' as CredentialOrigin,
    hostname_count: 1,
  },
];

/** The row the modal edits. `last_updated_at` is non-null on purpose:
 *  this name **has** published, which is the state the sentence about
 *  the orphaned record is actually about. */
const ROW = {
  id: NAME_ID,
  name: NAME,
  domain_id: ZONE_ID,
  domain_name: ZONE.name,
  device_id: DEVICE_ID,
  device_name: 'home-router',
  created_at: '2026-08-15T10:00:00Z',
  last_ip_v4: '203.0.113.10',
  last_ip_v6: null,
  last_updated_at: '2026-08-15T13:47:00Z',
};

const PUBLISHING = {
  hostname_id: NAME_ID,
  name: NAME,
  domain_id: ZONE_ID,
  domain_name: ZONE.name,
  device_id: DEVICE_ID,
  inherits_backends: true,
  ttl: null,
  default_ttl: 60,
  ttl_min: 30,
  ttl_max: 86400,
  backends: [],
  publishes_to: [],
};

const BOARD = board({
  devices: [
    boardDevice({
      id: DEVICE_ID,
      name: 'home-router',
      hostnames: [hostname({ id: NAME_ID, name: NAME, device_id: DEVICE_ID })],
    }),
  ],
});

let handles: MockAtriumHandles;
/** Every request the bundle made, in order. The assertions about what
 *  Save and Delete do are made on this and not on the rendered form:
 *  reading a `disabled` prop back off the component that set it is the
 *  component agreeing with itself. */
let sent: { url: string; method: string }[] = [];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function mutations() {
  return sent.filter((r) => r.method !== 'GET');
}

beforeEach(() => {
  queryClient.clear();
  sent = [];
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      sent.push({ url, method: (init?.method ?? 'GET').toUpperCase() });
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(BOARD);
      if (url.endsWith('/atrium_ddns/devices')) return json(DEVICES);
      if (url.endsWith('/atrium_ddns/domains')) return json([ZONE]);
      if (url.endsWith('/atrium_ddns/hostnames')) return json([ROW]);
      if (url.endsWith(`/atrium_ddns/hostnames/${NAME_ID}/backends`))
        return json(PUBLISHING);
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: [] });
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // The modal is read out of the address bar, so leaving `?name=41`
  // behind opens the next test on a board that already has a modal over
  // it — which fails as "the table did not render", the wrong diagnosis
  // entirely. `boardAffordance.test.tsx` records the same trap.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/** Open the Name modal on `NAME_ID` the way the address bar does. */
async function openName() {
  renderWithAtrium(<DeviceBoardPage />);
  await screen.findByTestId('board-table');
  act(() => {
    window.history.pushState({}, '', `/atrium-ddns?name=${NAME_ID}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  return screen.findByTestId('name-modal-body');
}

/** Open it and ask to delete. Returns the confirmation's own body. */
async function askToDelete() {
  const body = await openName();
  fireEvent.click(screen.getByTestId('name-delete'));
  return {
    body,
    confirm: await screen.findByTestId('name-delete-confirm'),
  };
}

/** Every open dialog, as the accessibility tree sees it. Mantine's
 *  `Modal` renders `role="dialog"`; an `Alert` renders nothing of the
 *  sort, which is the whole distinction under test. */
function dialogs() {
  return document.querySelectorAll('[role="dialog"]');
}

describe('deleting a name confirms in its own modal, not inline — #153', () => {
  test('the confirmation is a second dialog over the Name modal', async () => {
    const { confirm } = await askToDelete();

    // One surface before, two after. Against the inline `Alert` this
    // reads 1 and the test fails here, naming the panel.
    expect(
      dialogs().length,
      'the delete confirmation did not open a dialog of its own — it is ' +
        'still a panel inside the Name modal, so the form and the ' +
        'confirmation are one surface with two sets of buttons (#153)',
    ).toBe(2);
    // …and the confirmation is inside one of them, rather than a
    // second dialog having appeared for some unrelated reason. Without
    // this, any stray modal would satisfy the count.
    expect(confirm.closest('[role="dialog"]')).not.toBeNull();
  });

  test('nothing of it is left inside the Name modal’s body', async () => {
    const { body, confirm } = await askToDelete();

    // The exclusion half. `1` above is satisfiable by rendering the
    // modal *and* leaving the panel in place; this is the reading that
    // is not.
    expect(
      within(body).queryByTestId('name-delete-confirm'),
      'the confirmation is still rendered inside the Name modal body, ' +
        'so `Delete this name` and `Delete <name>` are still inches ' +
        'apart on one surface (#153)',
    ).toBeNull();
    expect(body.contains(confirm)).toBe(false);
  });

  test('the sentence about the orphaned record survives verbatim', async () => {
    const { confirm } = await askToDelete();

    // The thing users get wrong, and the reason the panel existed at
    // all. Whitespace is normalised because the sentence is split
    // across elements — `<span class="ddns-data">` for the name and
    // `<strong>` for the "not" — but no word of it is.
    const text = confirm.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain(`This removes ${NAME} from this service.`);
    expect(text).toContain(
      'It does not remove the record your provider has already published — ' +
        'that stays in the zone with nothing maintaining it.',
    );
  });

  test('the form beneath cannot be saved while it is open', async () => {
    await askToDelete();

    const save = screen.getByTestId('name-submit');
    // The visible half: the control says no.
    expect(
      save,
      'the Name modal’s Save is still enabled behind the delete ' +
        'confirmation, so the surface offers save and destroy at the ' +
        'same moment with no ordering between them (#153)',
    ).toBeDisabled();

    // The half that matters: clicking it sends nothing. Asserted on
    // `fetch`, because `disabled` is a prop the component set on itself
    // and a handler wired past it would keep the prop and still save.
    fireEvent.click(save);
    // A `PATCH` would be in flight by the next tick if one were sent.
    await act(async () => {
      await Promise.resolve();
    });
    expect(
      mutations(),
      'Save fired a request while the delete confirmation was open',
    ).toEqual([]);
  });

  test('dismissing returns to the Name modal with nothing changed', async () => {
    const { body, confirm } = await askToDelete();

    // By its accessible name, not by a testid this change introduced.
    // The inline panel had a `Keep it` button too, so queried this way
    // the test is answerable in both worlds — which is the point: it is
    // holding a behaviour that was already right, not demonstrating the
    // defect. A guard that fails with "unable to find
    // [data-testid=…]" names the query rather than the fault.
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep it' }));

    await waitFor(() =>
      expect(screen.queryByTestId('name-delete-confirm')).toBeNull(),
    );
    // The form is still there — dismissing the confirmation must not
    // take the modal with it.
    expect(document.body.contains(body)).toBe(true);
    expect(screen.getByTestId('name-modal-body')).toBeTruthy();
    // …and usable again.
    expect(screen.getByTestId('name-submit')).not.toBeDisabled();
    // Nothing was sent, in either direction.
    expect(mutations()).toEqual([]);
  });

  test('confirming sends exactly one DELETE and closes both', async () => {
    const { confirm } = await askToDelete();

    fireEvent.click(within(confirm).getByTestId('name-delete-confirmed'));

    await waitFor(() =>
      expect(screen.queryByTestId('name-modal-body')).toBeNull(),
    );
    expect(screen.queryByTestId('name-delete-confirm')).toBeNull();
    expect(mutations()).toEqual([
      { url: `/api/atrium_ddns/hostnames/${NAME_ID}`, method: 'DELETE' },
    ]);
  });
});
