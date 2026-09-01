/** The device card: one form, one Save, and the two things that Save
 *  must not do behind your back.
 *
 * ## The four hand-reported behaviours this file owns
 *
 * All four were reported by a human using the product, none of them by
 * a suite, and all four were fixed without a regression test. #133's
 * sweep of the record found them; this file is where they stop being
 * findable only by clicking.
 *
 * | # | reported | where the record says so |
 * |---|---|---|
 * | 1 | *"a device's name cannot be edited"* | `docs/ops/ui-design.md` §8 row 2 → #89 |
 * | 2 | a Save per field on the device card | PR #127, *"Reuse rather than reimplementation"* |
 * | 3 | an untouched rate-limit box pinned an inheriting device | PR #127, *"Two traps found while collapsing those"* |
 * | 4 | rotation printed the credential inline, not in the once-only modal | PR #127, same section |
 *
 * (1) had a test — `frontend/src/test/DeviceDetailPage.test.tsx`, whose
 * describes were *the name is editable, in place*, *the conflict is
 * surfaced, not avoided*, *the rate limit keeps its third state* and
 * *rotation is its own operation*. **#127 deleted that file** along with
 * `/atrium-ddns/devices/:id`, and moved every one of those behaviours
 * into `tenant/DeviceCard.tsx`, which had none. So (1) and the third
 * state of (3) were covered once and are not now, and (2) and (4) never
 * were.
 *
 * ## Why (3) is the one to read first
 *
 * It is the only one of the four whose failure is **silent and
 * durable**. The other three are visible the moment they break: a name
 * that will not save, two Save buttons, a secret in the wrong place. A
 * rate limit that was inheriting and is now pinned looks identical on
 * screen — the box shows `30` either way — and the difference only
 * surfaces months later when someone raises the installation default
 * and one device does not follow. `limitTouched` is the whole fix, it is
 * three lines, and a refactor that drops it changes nothing anybody can
 * see.
 *
 * ## How these drive the card
 *
 * Through `DeviceBoardPage` at `?device=<id>`, which is how a person
 * reaches it: the card is a modal over the board and the address bar is
 * what opens it. Driving `DeviceCard` directly would skip
 * `DeviceCardModal`, `DdnsPortalScope` and the query-parameter reader —
 * three of the files #127 changed — and assert about a mount no user can
 * produce.
 *
 * Every assertion about what Save does is made on **the request body the
 * bundle actually sent**, captured from the `fetch` stub. Reading it off
 * the rendered form would be asserting the component agrees with itself.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DeviceBoardPage } from '../DeviceBoardPage';
import { queryClient } from '../queryClient';
import type { CredentialOrigin, Device } from '../api/devices';
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

const DEVICE_ID = 7;

/** The device under test. **It is inheriting** — `rate_limit_per_minute`
 *  is `null` and the effective figure is `30` — because that is the
 *  state trap (3) destroys and the state most devices are in. A fixture
 *  with a pinned limit would make the trap unreachable. */
function storedDevice(overrides: Partial<Device> = {}): Device {
  return {
    id: DEVICE_ID,
    name: 'home-router',
    username: 'ddns-000000000007',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued' as CredentialOrigin,
    hostname_count: 1,
    ...overrides,
  };
}

const BOARD = board({
  devices: [boardDevice({ id: DEVICE_ID, name: 'home-router' })],
});

type Sent = { url: string; method: string; body: Record<string, unknown> };

let handles: MockAtriumHandles;
let sent: Sent[] = [];
let device: Device;
/** Set by a test that wants the next PATCH refused, the way the server
 *  refuses a duplicate name. Consumed once. */
let nextFailure: { status: number; detail: string } | null = null;
/** What `POST /rotate` answers. */
let rotated: { device: Device; secret: string } | null = null;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

beforeEach(() => {
  queryClient.clear();
  sent = [];
  device = storedDevice();
  nextFailure = null;
  rotated = null;
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET';
      if (method !== 'GET') {
        sent.push({
          url,
          method,
          body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {},
        });
      }
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(BOARD);
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}/rotate`)) {
        return json(rotated ?? { device, secret: 'unset' });
      }
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}`) && method === 'PATCH') {
        if (nextFailure) {
          const failure = nextFailure;
          nextFailure = null;
          return json({ detail: failure.detail }, failure.status);
        }
        return json(device);
      }
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}`)) return json(device);
      if (url.endsWith('/atrium_ddns/devices')) return json([device]);
      if (url.endsWith('/atrium_ddns/domains')) return json([]);
      if (url.endsWith('/atrium_ddns/hostnames')) return json([]);
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // The card is the address bar. Leaving `?device=7` behind opens the
  // next test with a modal already over the board, which fails as
  // "the table did not render" — the wrong diagnosis entirely.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/** Open the card the way a person does: put the device in the address
 *  bar, then render. */
async function openCard() {
  window.history.pushState({}, '', `/atrium-ddns?device=${DEVICE_ID}`);
  renderWithAtrium(<DeviceBoardPage />);
  return (await screen.findByTestId('device-detail')) as HTMLElement;
}

/** The PATCHes that reached the network, in order. */
function patches(): Sent[] {
  return sent.filter((s) => s.method === 'PATCH');
}

function nameBox(): HTMLInputElement {
  return screen.getByTestId('device-name-input') as HTMLInputElement;
}

function limitBox(): HTMLInputElement {
  return screen.getByTestId('detail-limit-input') as HTMLInputElement;
}

describe('the name is editable — ui-design §8 row 2, #89', () => {
  test('a rename reaches the server, on the card’s own Save', async () => {
    await openCard();
    expect(nameBox().value).toBe('home-router');

    fireEvent.change(nameBox(), { target: { value: 'attic-router' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(
      patches()[0].body.name,
      'Save did not send the typed name. The name box is bound to state ' +
        'that Save does not read — which is exactly the shape the field ' +
        'had before #89, when it rendered and could not be submitted.',
    ).toBe('attic-router');
    expect(patches()[0].url).toContain(`/atrium_ddns/devices/${DEVICE_ID}`);
  });

  test('a name that is only whitespace is not a rename, and is not sent', async () => {
    // #89 closed a hole where `"   "` created a device named `""`.
    // Same rule on the edit path: an empty name is not a name.
    await openCard();
    fireEvent.change(nameBox(), { target: { value: '   ' } });
    expect(screen.getByTestId('device-save')).toBeDisabled();
    fireEvent.click(screen.getByTestId('device-save'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(patches().length).toBe(0);
  });

  test('the 409 is rendered in the server’s own words, not reworded', async () => {
    // #89: *"Do not avoid the conflict by generating a suffix or silently
    // accepting a duplicate."* The refusal the API guarantees is more
    // useful than any sentence this component could compose, so the
    // assertion is on the server's string appearing verbatim.
    const detail = "you already have a device called 'attic-router'";
    nextFailure = { status: 409, detail };
    await openCard();
    fireEvent.change(nameBox(), { target: { value: 'attic-router' } });
    fireEvent.click(screen.getByTestId('device-save'));

    const refusal = await screen.findByTestId('device-save-refusal');
    expect(refusal.textContent).toContain(detail);
    // …and the card is still open with the typed name in it. A refusal
    // that closes the card loses what you typed and reads as a success.
    expect(nameBox().value).toBe('attic-router');
  });
});

describe('one form, one Save — PR #127', () => {
  test('the card carries exactly one Save, for every field on it', async () => {
    // The reported shape was a Save *per field*: a rename saved
    // separately from a rate limit, on a card where both are one
    // sentence about one device. Counting the controls is the direct
    // reading of "one form": a second Save reappearing is a second
    // request body, which is how the two traps below became reachable
    // in the first place.
    const card = await openCard();
    const saves = within(card)
      .getAllByRole('button')
      .filter((button) => (button.textContent ?? '').trim() === 'Save');
    expect(
      saves.map((b) => b.getAttribute('data-testid')),
      'the device card has grown a second Save. One card, one form, one ' +
        'request body — see PR #127.',
    ).toEqual(['device-save']);
  });

  test('one Save sends the name and the limit in one request', async () => {
    // Two fields changed, one PATCH. Two requests here would mean two
    // ways for the card to end up half-saved.
    await openCard();
    fireEvent.change(nameBox(), { target: { value: 'attic-router' } });
    fireEvent.change(limitBox(), { target: { value: '60' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body).toEqual({
      name: 'attic-router',
      rate_limit_per_minute: 60,
    });
  });

  test('Save is unavailable while nothing has changed', async () => {
    // The control for the two tests above: if Save were always live,
    // "one Save sent one request" would be true of a card that sends a
    // request for no reason, and the rate-limit trap below would fire on
    // every open-and-close.
    await openCard();
    expect(screen.getByTestId('device-save')).toBeDisabled();
  });
});

describe('an untouched rate limit does not pin an inheriting device — PR #127', () => {
  test('renaming alone sends the stored null, not the effective default', async () => {
    // The trap, exactly as PR #127 describes it: the box is prefilled
    // with the limit *in force* (30, inherited), so with one Save for
    // the card a plain rename would write 30 as an **explicit** value
    // and stop the device following the installation default — with
    // nothing on screen different afterwards.
    await openCard();
    expect(
      limitBox().value,
      'the limit box is meant to open showing the limit actually in ' +
        'force, which is the inherited 30 — without that prefill this ' +
        'test is not exercising the trap',
    ).toBe('30');

    fireEvent.change(nameBox(), { target: { value: 'attic-router' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(
      patches()[0].body.rate_limit_per_minute,
      'a rename sent the inherited default as an explicit limit. The ' +
        'device was following the installation setting and now is not, ' +
        'and nothing on screen says so — see `limitTouched`.',
    ).toBeNull();
  });

  test('typing in the box is what makes the number explicit', async () => {
    // The other half, and the reason the fix is `limitTouched` rather
    // than "never send the limit": a limit the operator actually typed
    // must be pinned. Without this assertion, `rate_limit_per_minute:
    // null` hardcoded would pass the test above.
    await openCard();
    fireEvent.change(limitBox(), { target: { value: '90' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body.rate_limit_per_minute).toBe(90);
  });

  test('clearing the box is how a pinned device goes back to inheriting', async () => {
    // #73's third state, from the deleted `DeviceDetailPage.test.tsx`.
    // Empty is *inherit*, and it travels as `null` — which is a value on
    // that column and not an omission. Without this a device, once
    // opened and pinned, could never inherit again.
    device = storedDevice({ rate_limit_per_minute: 45 });
    await openCard();
    expect(limitBox().value).toBe('45');
    fireEvent.change(limitBox(), { target: { value: '' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body.rate_limit_per_minute).toBeNull();
  });

  test('zero is muted, and is not the same thing as inheriting', async () => {
    // `0` is *may never call*; `null` is *follow the setting*. They are
    // two states and the mapping is not `|| null`, which would collapse
    // them and silently un-mute a device someone deliberately stopped.
    device = storedDevice({ rate_limit_per_minute: 45 });
    await openCard();
    fireEvent.change(limitBox(), { target: { value: '0' } });
    fireEvent.click(screen.getByTestId('device-save'));

    await waitFor(() => expect(patches().length).toBe(1));
    expect(patches()[0].body.rate_limit_per_minute).toBe(0);
  });
});

describe('rotation shows the secret in the once-only modal — PR #127', () => {
  const SECRET = 'zzzz-rotated-secret-value-0001';

  test('the rotated secret appears in the modal, and not inline in the card', async () => {
    // Reported: rotation printed the credential **inside the card it was
    // rotated from**, competing with the form and scrolling away like
    // ordinary content — for the one string in this product that can
    // never be recovered. The fix is that create and rotate use the same
    // `SecretOnceModal`.
    //
    // Two readings, because "it is in the modal" and "it is not also in
    // the card" are different facts and only the pair rules out an
    // inline copy that happens to have a modal beside it.
    rotated = { device: storedDevice(), secret: SECRET };
    const card = await openCard();

    fireEvent.click(screen.getByTestId('detail-rotate'));
    fireEvent.click(await screen.findByTestId('detail-rotate-confirm'));

    // `device-secret-once` is the modal's *body* — the `Alert` inside
    // `SecretOnceModal`. Mantine's `Modal` does not forward `data-testid`
    // to a node the DOM query can reach, so every test in this suite
    // queries a modal by its body; `sharedCard.test.tsx` and
    // `boardAffordance.test.tsx` do the same.
    const modal = await screen.findByTestId('device-secret-once');
    expect(within(modal).getByTestId('issued-secret').textContent).toContain(
      SECRET,
    );
    // The card body is a separate subtree from the portalled modal, so
    // this is a real exclusion rather than a restatement.
    expect(
      card.textContent,
      'the rotated secret is rendered inline in the device card as well ' +
        'as (or instead of) in the once-only modal — PR #127',
    ).not.toContain(SECRET);
  });

  test('the sentence that makes it once-only is shown with it', async () => {
    // The modal's whole job. A rotated secret displayed without the
    // "this is the only time" sentence is the same defect one step
    // along: the user reads it as something they can come back for.
    rotated = { device: storedDevice(), secret: SECRET };
    await openCard();
    fireEvent.click(screen.getByTestId('detail-rotate'));
    fireEvent.click(await screen.findByTestId('detail-rotate-confirm'));

    const modal = await screen.findByTestId('device-secret-once');
    expect(
      within(modal).getByTestId('secret-once-warning').textContent,
    ).toMatch(/only time/i);
    // And there is no "show it again" control, disabled or otherwise —
    // a disabled control for an impossible operation teaches that the
    // operation exists.
    expect(within(modal).queryByText(/show again/i)).toBeNull();
  });

  test('nothing is shown until the rotation is confirmed', async () => {
    // The control. Rotation stops a working router immediately, so the
    // confirmation is what protects the device — and a test that clicked
    // straight through would pass against a card that rotated on the
    // first click.
    rotated = { device: storedDevice(), secret: SECRET };
    await openCard();
    fireEvent.click(screen.getByTestId('detail-rotate'));
    await screen.findByTestId('detail-rotate-warning');
    expect(screen.queryByTestId('issued-secret')).toBeNull();
    expect(sent.filter((s) => s.url.endsWith('/rotate')).length).toBe(0);
  });
});
