/** The device card is locked under its own confirmations. #160.
 *
 * ## What was wrong
 *
 * `DeviceCard`'s Save stayed **enabled** while the card's own delete
 * confirmation was open. Its disabled condition read `trimmed === '' ||
 * !dirty || saveSettings.isPending` (`DeviceCard.tsx:510` before this
 * change) — nothing about a confirmation being on screen. So with
 * *"Delete this device?"* up, the form behind it still accepted a Save:
 * two live actions, one of them destructive, with no ordering between
 * them. The name box, the rate limit, `Rotate Credentials`, `Cancel` and
 * `Delete this device` were all live too.
 *
 * **This is the surface #153 and #159 were told to copy**, and it is why
 * the gap is worth a file of its own rather than a line in an existing
 * one. `DeviceCard` already had the right *shape* — a real `Modal` at
 * `SecretOnceModal`'s `zIndex={400}`, and its delete error rendered
 * *inside* the dialog rather than on the form behind it, which is the
 * half `ZoneModal` got wrong. It was missing the lock. The codebase's
 * best example of this pattern taught two thirds of it, so both issues
 * that copied it had to notice the gap rather than inherit it.
 *
 * ## The seven readings of the defect, and why each is separate
 *
 * None of these implies another, which is the reason they are seven
 * tests and not one:
 *
 *  1. **Every control on the card is disabled.** Enumerated — every
 *     `button`, `input`, `textarea` and `select` inside `device-detail`,
 *     each asserted `toBeDisabled()` — rather than named one by one, so
 *     a control added to this card later is covered by the test that
 *     exists instead of by one nobody wrote. The issue asks for exactly
 *     this and says why: an overlay stops a mouse and not a keyboard, an
 *     assistive technology or a test, so *"the overlay renders"* is not
 *     the reading.
 *  2. **Save sends nothing.** Asserted on `fetch`, because `disabled` is
 *     a prop the component set on itself and reading it back is the
 *     component agreeing with itself. A handler wired past the prop
 *     would keep the prop and still save.
 *  3. **Enter in the name box sends nothing.** The same rule reached by
 *     the keyboard rather than the mouse. The name box submits on
 *     `Enter`, so `submit()` refuses on `confirming` as well — the prop
 *     and the handler are two instruments on one rule.
 *  4. **No enabled control on screen says `Cancel`.** The dialog's
 *     dismissal used to say `Cancel`, inches above the form's own
 *     `Cancel` — two live controls sharing one word with two meanings.
 *     That is the incident `ZoneModal`'s comment records and #159's
 *     reading of it: the hazard is two `Cancel`s, not two dialogs.
 *  5. **The rotate confirmation locks the card too.** This card asks
 *     twice, and #160's body names one of them. With only `confirmDelete`
 *     in the lock, `Delete this device` stays live under *"Rotate this
 *     secret?"* and one click stacks a second confirmation on the first
 *     — the exact shape the incident describes. Reported with the issue.
 *  6. **A refusal does not survive into the next attempt.** A 409 left
 *     `deleteError` set after the dialog was dismissed, so reopening it
 *     greeted you with the previous failure as though it were this one.
 *  7. **The lock survives a refusal.** A delete that is refused leaves
 *     the dialog up, and the card under it has to stay locked — the
 *     confirmation is still the only live surface. This one was *not*
 *     predicted; see below.
 *
 * ## Which were shown failing, and which were not
 *
 * Run against the pre-#160 card this file is **7 failed / 4 passed**:
 *
 * | test | against the pre-#160 card |
 * |---|---|
 * | every control on the card is disabled | **fails** — `input[data-testid=device-name-input]` is named first; `Save`, `Cancel`, `Rotate Credentials`, `Delete this device` and the limit box are live too |
 * | Save sends nothing while it is open | **fails** — one `PATCH` leaves the bundle |
 * | Enter in the name box sends nothing | **fails** — same `PATCH`, by the keyboard |
 * | nothing enabled says `Cancel` | **fails** — on the form's own live `Cancel`, which is the right reason rather than on a missing `Keep it` |
 * | the rotate confirmation locks it too | **fails** — the card is live under `Rotate this secret?`, so `Delete this device` stacks a second dialog |
 * | a refusal does not survive the next attempt | **fails** — the 409 is still there when the dialog is reopened |
 * | a refused delete says so inside the dialog | **fails** — on its *last* assertion only; see below |
 * | the confirmation is its own dialog, not in the card | passes |
 * | it names the device and what goes with it | passes |
 * | dismissing releases the lock and sends nothing | passes |
 * | confirming sends exactly one `DELETE` | passes |
 *
 * **The split was predicted as 6/5 and measured as 7/4, and the
 * disagreement is the useful part.** *"A refused delete says so inside
 * the confirmation"* was written as a passes-both-ways guard — the half
 * `DeviceCard` already had right — and it fails, because its closing
 * assertion is that the card is *still locked* after the refusal. That
 * is a seventh reading of the defect wearing a regression test's
 * clothing, and it was found by stating the expected verdicts before
 * running rather than after. It is left where it is, with the table
 * corrected, rather than moved to make the prediction true.
 *
 * The four that pass both ways are not padding and they are not evidence
 * either. They are the half `DeviceCard` already had right — the half
 * the other two issues were sent here to copy — and they are what a
 * change to the button row is most likely to drop on the floor. Saying
 * which is which matters more than the count: *"eleven tests for #160"*
 * would imply eleven readings of the defect, and there are seven.
 *
 * ## Two things about how it is driven
 *
 * **Through `DeviceBoardPage` at `?device=<id>`**, which is how a person
 * reaches this card — `deviceCard.test.tsx` records why. Mounting
 * `DeviceCard` directly would skip `DeviceCardModal`, `DdnsPortalScope`
 * and the query-parameter reader, and this issue is specifically about
 * how a confirmation stacks over the card, which is the part a direct
 * mount would not exercise.
 *
 * **Nothing here queries `detail-delete-confirm`.** That testid is on
 * Mantine's `Modal` **root**, which is in the DOM whether the modal is
 * open or shut — measured by #155's agent, which found it present and
 * empty *before* the click that was supposed to open it. `findByTestId`
 * on it is a probe that cannot fail. The dialog is reached here through
 * `detail-delete-confirmed`, a button that only exists when the modal is
 * open, and then by `closest('[role="dialog"]')`. #161 owns the sweep of
 * that class across the suite; this file adds no new instance of it.
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
const DEVICE_NAME = 'home-router';
/** What gets typed into the name box before the confirmation is opened.
 *  See `askToDelete` — the form has to be *dirty*, or `Save` is disabled
 *  for a reason that has nothing to do with #160. */
const TYPED_NAME = 'attic-router';

function storedDevice(): Device {
  return {
    id: DEVICE_ID,
    name: DEVICE_NAME,
    username: 'ddns-000000000007',
    created_at: '2026-08-15T10:00:00Z',
    last_seen_at: '2026-08-15T13:47:00Z',
    rate_limit_per_minute: null,
    effective_rate_limit_per_minute: 30,
    credential_origin: 'issued' as CredentialOrigin,
    hostname_count: 1,
  };
}

const BOARD = board({
  devices: [boardDevice({ id: DEVICE_ID, name: DEVICE_NAME })],
});

let handles: MockAtriumHandles;
/** Every request the bundle made, in order. Every assertion about what
 *  `Save` and `Delete` did is made on this rather than on the rendered
 *  card: reading a `disabled` prop back off the component that set it is
 *  the component agreeing with itself. */
let sent: { url: string; method: string }[] = [];
/** What `DELETE /devices/:id` answers. Set per test. */
let deleteResponse: () => Response = () => new Response(null, { status: 204 });

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Everything that was not a read. The card issues a handful of GETs on
 *  open and they are not what any of this is about. */
function mutations() {
  return sent.filter((r) => r.method !== 'GET');
}

beforeEach(() => {
  queryClient.clear();
  sent = [];
  deleteResponse = () => new Response(null, { status: 204 });
  handles = mockAtriumRegistry({ me: TENANT });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase();
      sent.push({ url, method });
      if (url.endsWith('/users/me/context')) return json(TENANT);
      if (url.includes('/atrium_ddns/board')) return json(BOARD);
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}`) && method === 'DELETE') {
        return deleteResponse();
      }
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}/rotate`)) {
        return json({ device: storedDevice(), secret: 'ddns-secret-unused' });
      }
      if (url.endsWith(`/atrium_ddns/devices/${DEVICE_ID}`)) return json(storedDevice());
      if (url.endsWith('/atrium_ddns/devices')) return json([storedDevice()]);
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
  // next test with a modal already over the board, which fails as "the
  // table did not render" — the wrong diagnosis entirely.
  // `deviceCard.test.tsx` records the same trap.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/** Open the card the way a person does: the device in the address bar,
 *  then render. */
async function openCard() {
  window.history.pushState({}, '', `/atrium-ddns?device=${DEVICE_ID}`);
  renderWithAtrium(<DeviceBoardPage />);
  return (await screen.findByTestId('device-detail')) as HTMLElement;
}

/** Open the card, make the form **dirty**, then ask to delete.
 *
 *  The rename is not incidental. `Save` is disabled on a clean form for
 *  a reason that has nothing to do with this issue, so a test that
 *  opened the confirmation over an untouched card would read `Save` as
 *  disabled and be right by construction. The assertion below is the
 *  vacuity guard for every `Save` reading in this file: it fails loudly
 *  if the setup ever stops producing a live `Save`.
 *
 *  Returns the card body and the confirmation's own dialog element. */
async function askToDelete() {
  const card = await openCard();
  fireEvent.change(screen.getByTestId('device-name-input'), {
    target: { value: TYPED_NAME },
  });
  expect(
    screen.getByTestId('device-save'),
    'the form is not dirty before the confirmation opens, so `Save is ' +
      'disabled` would be true for an unrelated reason and every Save ' +
      'reading in this file would pass by construction (#160)',
  ).toBeEnabled();

  fireEvent.click(screen.getByTestId('detail-delete'));
  // Waited for on the confirm *button*, never on `detail-delete-confirm`
  // — see the docblock, and #161.
  const confirmed = await screen.findByTestId('detail-delete-confirmed');
  const dialog = confirmed.closest('[role="dialog"]') as HTMLElement;
  expect(dialog).not.toBeNull();
  return { card, dialog, confirmed };
}

/** Every interactive control in a subtree, by tag rather than by name. A
 *  named list would only ever cover the controls whoever wrote it
 *  remembered. */
function controlsIn(root: HTMLElement) {
  return Array.from(
    root.querySelectorAll<HTMLElement>('button, input, textarea, select'),
  );
}

/** The dialog's dismissal, found by what it does rather than by what it
 *  is called: `Keep it` after #160, `Cancel` before it.
 *
 *  Written this way on purpose. The round-trip test below holds a
 *  behaviour that was **already right**, so it has to be answerable in
 *  both worlds — a guard that fails with *"unable to find a button named
 *  Keep it"* names the query rather than the fault. The test that holds
 *  the *renaming* is a separate one, and it fails for the right reason.
 */
function dismissal(dialog: HTMLElement) {
  const button = within(dialog)
    .getAllByRole('button')
    .find((candidate) => /^(Keep it|Cancel)$/.test((candidate.textContent ?? '').trim()));
  expect(
    button,
    'the delete confirmation has no dismissal at all — neither `Keep it` ' +
      'nor `Cancel`. A destructive dialog whose only way out is the `×` is ' +
      'a worse version of the defect #160 is about.',
  ).toBeDefined();
  return button as HTMLElement;
}

describe('the device card is locked under its delete confirmation — #160', () => {
  test('every control on the card is disabled while it is open', async () => {
    const { card } = await askToDelete();

    const controls = controlsIn(card);
    // Vacuity guard. "Every control is disabled" is true of the empty
    // set, so a card that failed to render would pass this test against
    // anything. Six is the card's own row of verbs plus its two fields:
    // name, rate limit, Rotate Credentials, Delete this device, Cancel,
    // Save.
    expect(
      controls.length,
      'the device card rendered fewer controls than it has — the ' +
        'enumeration below would be asserting about an empty or ' +
        'half-mounted form',
    ).toBeGreaterThanOrEqual(6);
    for (const control of controls) {
      expect(
        control,
        'a control on the device card is still live under the delete ' +
          `confirmation: ${control.tagName.toLowerCase()}` +
          `[data-testid=${control.getAttribute('data-testid') ?? '?'}] — ` +
          'Mantine’s overlay stops a mouse, not a keyboard, an assistive ' +
          'technology or a test, so the card beneath must be locked and ' +
          'not merely covered (#160)',
      ).toBeDisabled();
    }
  });

  test('Save sends nothing while it is open', async () => {
    await askToDelete();

    fireEvent.click(screen.getByTestId('device-save'));
    // Let the click's handler and any request it started settle.
    // `deviceCard.test.tsx` flushes the same way; `act` from `react`
    // prints "the current testing environment is not configured to
    // support act(...)" in this suite, which is noise in a file whose
    // whole point is that a failure names the real fault.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      mutations(),
      'Save fired a request while the delete confirmation was open — the ' +
        'card offered save and destroy at the same moment, with no ' +
        'ordering between them (#160)',
    ).toEqual([]);
  });

  test('Enter in the name box sends nothing while it is open', async () => {
    // The same rule reached by the keyboard. The name box submits on
    // `Enter`, and an overlay does not stop a keystroke — which is the
    // sentence the issue uses and the reason `submit()` refuses on
    // `confirming` as well as `Save` being disabled. Two instruments on
    // one rule: the prop and the handler.
    await askToDelete();

    fireEvent.keyDown(screen.getByTestId('device-name-input'), {
      key: 'Enter',
      code: 'Enter',
    });
    // Let the click's handler and any request it started settle.
    // `deviceCard.test.tsx` flushes the same way; `act` from `react`
    // prints "the current testing environment is not configured to
    // support act(...)" in this suite, which is noise in a file whose
    // whole point is that a failure names the real fault.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(
      mutations(),
      'pressing Enter in the name box saved the card while the delete ' +
        'confirmation was open. `Save` being disabled is a statement about ' +
        'a button; this is the statement about the action (#160)',
    ).toEqual([]);
  });

  test('no enabled control on screen is labelled Cancel', async () => {
    const { dialog } = await askToDelete();

    // Queried across the whole document — both surfaces at once, by
    // accessible name. The incident in `ZoneModal`'s comment is *a
    // `Cancel` that deleted*, and it came from two live `Cancel`s rather
    // than from two dialogs.
    for (const button of screen.queryAllByRole('button', { name: 'Cancel' })) {
      expect(
        button,
        'a `Cancel` is live while the delete confirmation is open — that is ' +
          'the exact shape the incident in ZoneModal’s comment describes, ' +
          'and #159 named the hazard as two live `Cancel`s (#160)',
      ).toBeDisabled();
    }
    // And the dismissal is not spelled that way at all, so no two
    // controls on screen share a word with two meanings.
    expect(
      within(dialog).queryByRole('button', { name: 'Cancel' }),
      'the delete confirmation dismisses with `Cancel`, the one word this ' +
        'dialog must not use — `NameModal` and `ZoneModal` both spell it ' +
        '`Keep it` (#153, #159)',
    ).toBeNull();
    expect(within(dialog).getByRole('button', { name: 'Keep it' })).toBeEnabled();
  });

  test('the rotate confirmation locks the card too, so the two cannot stack', async () => {
    // #160's body names the delete confirmation. This card asks twice,
    // and the lock has to cover both: with only `confirmDelete` in the
    // condition, `Delete this device` stays live underneath "Rotate this
    // secret?" and one click puts a second confirmation on top of the
    // first — two overlapping dialogs, each with its own idea of what
    // the buttons at the bottom mean. Reported with the issue.
    const card = await openCard();
    fireEvent.click(screen.getByTestId('detail-rotate'));
    await screen.findByTestId('detail-rotate-warning');

    for (const control of controlsIn(card)) {
      expect(
        control,
        'a control on the device card is still live under the rotate ' +
          `confirmation: ${control.tagName.toLowerCase()}` +
          `[data-testid=${control.getAttribute('data-testid') ?? '?'}] — ` +
          '`Delete this device` here opens a second confirmation on top ' +
          'of the first (#160)',
      ).toBeDisabled();
    }
    // Nothing was sent by merely asking, in either direction.
    expect(mutations()).toEqual([]);
  });

  test('a refusal does not survive into the next attempt', async () => {
    deleteResponse = () =>
      new Response('device still has names bound to it', { status: 409 });
    const { dialog } = await askToDelete();

    fireEvent.click(within(dialog).getByTestId('detail-delete-confirmed'));
    await screen.findByTestId('detail-delete-error');

    // Dismiss, then ask again. The second dialog is about the second
    // attempt; greeting it with the first attempt's refusal reads as a
    // failure that has already happened this time.
    fireEvent.click(dismissal(dialog));
    await waitFor(() =>
      expect(screen.queryByTestId('detail-delete-confirmed')).toBeNull(),
    );
    fireEvent.click(screen.getByTestId('detail-delete'));
    await screen.findByTestId('detail-delete-confirmed');

    expect(
      screen.queryByTestId('detail-delete-error'),
      'the previous attempt’s refusal is still in the confirmation when it ' +
        'is reopened, so a fresh dialog opens already reporting a failure ' +
        'that has not happened yet (#160)',
    ).toBeNull();
  });
});

/** The half `DeviceCard` already had right and must keep — plus one that
 *  turned out not to be. See the docblock's 6/5-predicted, 7/4-measured
 *  note: the refusal test below is in this block because that is what it
 *  was written to be, and it is left here with the correction stated
 *  rather than quietly re-filed. */
describe('what the card already had right, and must keep — #160', () => {
  test('the confirmation is its own dialog, and none of it is in the card', async () => {
    const { card, dialog, confirmed } = await askToDelete();

    // Two surfaces: the card's own modal, and the confirmation over it.
    // `role="dialog"` is the accessibility tree's own answer to "how
    // many surfaces am I looking at"; an `Alert` renders nothing of the
    // sort, which is what `NameModal` and `ZoneModal` had instead.
    expect(document.querySelectorAll('[role="dialog"]').length).toBe(2);
    // The exclusion half. A count of 2 is satisfiable by rendering the
    // dialog *and* leaving a panel in the card; this is the reading that
    // is not. The card body is a separate portal subtree, so this is a
    // real exclusion rather than a restatement.
    expect(card.contains(confirmed)).toBe(false);
    expect(card.contains(dialog)).toBe(false);
  });

  test('it names the device and what goes with it', async () => {
    const { dialog } = await askToDelete();

    // The only place the blast radius is stated. Whitespace is
    // normalised because the sentence is split across elements, but no
    // word of it is.
    const text = dialog.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain(
      `${DEVICE_NAME} is deleted, along with its DDNS credential. Any name ` +
        'it updates is left with no device, so nothing will update it until ' +
        'you assign another. This cannot be undone.',
    );
    // Named on the button as well, so the last click says what it
    // destroys — the context the board row could not carry (#155).
    expect(within(dialog).getByTestId('detail-delete-confirmed')).toHaveTextContent(
      `Delete ${DEVICE_NAME}`,
    );
  });

  test('a refused delete says so inside the confirmation, which stays up', async () => {
    deleteResponse = () =>
      new Response('device still has names bound to it', { status: 409 });
    const { card, dialog } = await askToDelete();

    fireEvent.click(within(dialog).getByTestId('detail-delete-confirmed'));

    const error = await screen.findByTestId('detail-delete-error');
    expect(error.textContent).toContain('device still has names bound to it');
    // On the surface being looked at. The card's own refusal `Alert`
    // (`device-save-refusal`) is up at the top of the body, behind the
    // dialog — which is where `ZoneModal` sent its delete errors until
    // #159, and is the half `DeviceCard` already had right.
    expect(
      card.contains(error),
      'the delete error rendered on the card, behind the confirmation — ' +
        'the server’s words went to a surface nobody was looking at',
    ).toBe(false);
    expect(dialog.contains(error)).toBe(true);
    // A refusal is not a dismissal: the dialog is still up, and it is
    // still the only live surface.
    expect(within(dialog).getByTestId('detail-delete-confirmed')).toBeTruthy();
    // …and *this* line is the one that made this test fail against the
    // pre-#160 card, when it was written expecting to pass. The lock has
    // to survive a refusal, and there was no lock to survive it. Reading
    // seven, found by predicting the verdict before running.
    expect(
      screen.getByTestId('device-save'),
      'the card unlocked itself when the delete was refused, so Save is ' +
        'live again with the confirmation still on screen (#160)',
    ).toBeDisabled();
  });

  test('dismissing releases the lock and sends nothing', async () => {
    const { card, dialog } = await askToDelete();

    fireEvent.click(dismissal(dialog));
    await waitFor(() =>
      expect(screen.queryByTestId('detail-delete-confirmed')).toBeNull(),
    );

    // The card is still there — dismissing the confirmation must not
    // take the card with it.
    expect(document.body.contains(card)).toBe(true);
    // …and usable again, which is the other half of `locked`: a lock
    // that is never released is a broken form. Enumerated the same way
    // it was asserted disabled, so a control that stays stuck is named.
    for (const control of controlsIn(card)) {
      expect(
        control,
        `a control on the device card is still disabled after the ` +
          `confirmation was dismissed: ${control.tagName.toLowerCase()}` +
          `[data-testid=${control.getAttribute('data-testid') ?? '?'}] — ` +
          'a lock that is never released is a broken form (#160)',
      ).toBeEnabled();
    }
    // Nothing was sent, in either direction.
    expect(mutations()).toEqual([]);
  });

  test('confirming sends exactly one DELETE', async () => {
    const { dialog } = await askToDelete();

    fireEvent.click(within(dialog).getByTestId('detail-delete-confirmed'));

    await waitFor(() => expect(mutations().length).toBe(1));
    expect(mutations()).toEqual([
      { url: `/api/atrium_ddns/devices/${DEVICE_ID}`, method: 'DELETE' },
    ]);
  });
});
