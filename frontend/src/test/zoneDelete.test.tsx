/** Deleting a zone interrupts, and locks what is under it. #159.
 *
 * ## What was wrong
 *
 * The confirmation was an inline `Alert` in the Zone modal's own body
 * (`ZoneModal.tsx:653-691` before this change), with the form's own
 * `Delete this zone` / `Cancel` / `Save` row rendered
 * **unconditionally** immediately beneath it (`:695`). One surface, two
 * sets of buttons, two meanings of "delete" — and *save* and *destroy*
 * offered at the same moment with no ordering between them. Same defect
 * #153 fixed in `NameModal`, over a much larger blast radius: a zone,
 * its stored provider credential, and every name under it.
 *
 * The file also carried a comment claiming the confirmation *"replaces
 * the button row rather than opening a second modal over the first"*.
 * It did not, and a reader who trusted it would have left the worst
 * instance of this defect in place believing it had been rejected on
 * purpose. That is not a thing a test can hold, so it is corrected in
 * the source; what the tests below hold is the behaviour.
 *
 * ## The five readings, and why each is a separate fact
 *
 * A destructive confirmation has to be **its own surface**, it has to
 * **leave nothing behind on the old one**, it has to **stop everything
 * under it**, it must not put **two live `Cancel`s** on screen, and
 * when it fails it has to **say so where it can be read**. No one of
 * those implies another:
 *
 *  1. **It is a second dialog.** Counted by `role="dialog"`, the
 *     accessibility tree's own answer to "how many surfaces am I
 *     looking at" — 1 for the inline panel, 2 for the modal. An
 *     `Alert` is not a dialog no matter what it is titled.
 *  2. **The confirmation is not in the Zone modal's body.** `1` is
 *     satisfiable by rendering a modal *and* keeping the panel; this
 *     rules that out by excluding a subtree. Two instruments, opposite
 *     shapes: one counts surfaces, one excludes one.
 *  3. **Every control beneath is disabled.** Enumerated — every
 *     `button`, `input`, `textarea` and `select` inside
 *     `zone-modal-body`, each asserted `toBeDisabled()` — rather than
 *     named one by one, so a control added later is covered by the
 *     test that exists rather than by one nobody wrote. Asserted on
 *     `fetch` as well: clicking `Save` sends nothing. The issue asks
 *     for exactly this and says why — Mantine's overlay stops a mouse,
 *     not a keyboard, an assistive technology or a test, so "an
 *     overlay renders" is not the reading.
 *  4. **No enabled control on screen says `Cancel`.** This is the
 *     incident in `ZoneModal`'s own comment, asserted directly: *a
 *     `Cancel` that deleted* came from two overlapping surfaces each
 *     with a live `Cancel`. The fix is not one dialog, it is one
 *     meaning per word — the dismissal is spelled `Keep it`, and the
 *     form's `Cancel` is disabled while the dialog is up.
 *  5. **A failed delete is legible.** The form's own error `Alert` is
 *     at the top of the body — *behind* the dialog. Before this change
 *     `dropZone` used `onError: fail`, so a refused delete wrote the
 *     server's words onto a surface nobody was looking at.
 *
 * Plus the round trip: `Keep it` returns to a Zone modal with nothing
 * changed and nothing sent, and confirming sends exactly one `DELETE`
 * and closes both.
 *
 * ## Which of these were shown failing, and which were not
 *
 * Run against the inline panel this file is **5 failed / 3 passed**,
 * and the split is deliberate rather than incidental:
 *
 * | test | against the inline panel |
 * |---|---|
 * | a second dialog over the Zone modal | **fails** — `expected 1 to be 2` |
 * | not in the Zone modal's body | **fails** — the panel is right there |
 * | every control beneath is disabled | **fails** — `Save`, `Cancel`, `Delete this zone` and every field are live |
 * | nothing enabled says `Cancel` | **fails** — the form's `Cancel` is live beside `Keep it` |
 * | a refused delete says so in the dialog | **fails** — it wrote the form's `Alert`, behind it |
 * | the blast-radius sentence, verbatim | passes |
 * | `Keep it` changes nothing | passes |
 * | confirming sends one `DELETE` | passes |
 *
 * The three that pass both ways are not padding and they are not
 * evidence either. They are the half of the behaviour that was already
 * correct and that a move between surfaces is most likely to drop on
 * the floor: the wording, the dismissal, and the request. Saying which
 * is which matters more than the count — "eight tests for #159" would
 * imply eight readings of the defect, and there are five.
 *
 * ## Why it drives `DomainsPage` and not `ZoneModal`
 *
 * `?zone=<id>` on the zones list is how a person reaches this modal,
 * and it is what `affordance.test.tsx` does. Mounting `ZoneModal`
 * directly would skip `DdnsPortalScope` and the query-parameter reader,
 * and would assert about a mount no user can produce — and this issue
 * is specifically about how two modals stack, which is exactly the part
 * a direct mount would not exercise.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { act } from 'react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DomainsPage } from '../DomainsPage';
import {
  DOMAIN_PERMISSION,
  type Domain,
  type Provider,
} from '../api/domains';
import { queryClient } from '../queryClient';

const OPERATOR: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  permissions: [DOMAIN_PERMISSION],
  impersonating_from: null,
};

const ZONE_ID = 11;
const ZONE_NAME = 'example.invalid';

/** One provider, described the way the server describes one: a free-text
 *  setting, a fixed-choice setting, and two credential keys. All four
 *  render a control, which is what makes the enumeration below worth
 *  taking — a fixture with no provider would assert that three buttons
 *  are disabled and call it "every control". */
const PROVIDERS: Provider[] = [
  {
    service: 'route53',
    credential_keys: ['aws_access_key_id', 'aws_secret_access_key'],
    credential_labels: {},
    setting_fields: [
      {
        key: 'hosted_zone_id',
        label: 'Hosted zone id',
        help: '',
        choices: [],
        required: false,
        default: '',
      },
      {
        key: 'record_type',
        label: 'Record type',
        help: '',
        choices: ['A', 'AAAA'],
        required: false,
        default: 'A',
      },
    ],
  },
];

/** `credentials_set: false` so the credential boxes render — the mode
 *  defaults to *replace* with nothing stored. The zone still has three
 *  names under it, because the sentence under test counts them. */
const ZONE: Domain = {
  id: ZONE_ID,
  name: ZONE_NAME,
  created_at: '2026-08-15T10:00:00Z',
  hostname_count: 3,
  backends: [
    {
      id: 3,
      domain_id: ZONE_ID,
      backend_type: 'route53',
      config: { ttl: 60 },
      credentials_set: false,
      known_service: true,
      credential_keys: ['aws_access_key_id', 'aws_secret_access_key'],
    },
  ],
};

let handles: MockAtriumHandles;
/** Every request the bundle made, in order. The assertions about what
 *  `Save` and `Delete` do are made on this and not on the rendered
 *  form: reading a `disabled` prop back off the component that set it
 *  is the component agreeing with itself. */
let sent: { url: string; method: string }[] = [];
/** What `DELETE /domains/:id` answers. Set per test. */
let deleteResponse: () => Response = () => new Response(null, { status: 204 });

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
  deleteResponse = () => new Response(null, { status: 204 });
  handles = mockAtriumRegistry({ me: OPERATOR });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase();
      sent.push({ url, method });
      if (url.endsWith('/users/me/context')) return json(OPERATOR);
      if (url.endsWith('/atrium_ddns/providers')) return json({ providers: PROVIDERS });
      if (url.endsWith('/atrium_ddns/domains') && method === 'GET') return json([ZONE]);
      if (url.endsWith(`/atrium_ddns/domains/${ZONE_ID}`) && method === 'DELETE') {
        return deleteResponse();
      }
      return json({});
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // The modal is read out of the address bar, so leaving `?zone=11`
  // behind opens the next test on a list that already has a modal over
  // it. `affordance.test.tsx` records the same trap, and the failure it
  // produces names the wrong thing entirely.
  window.history.pushState({}, '', '/');
  vi.unstubAllGlobals();
});

/** Open the Zone modal on `ZONE_ID` the way the zones list does. */
async function openZone() {
  renderWithAtrium(<DomainsPage />);
  fireEvent.click(await screen.findByTestId(`open-domain-${ZONE_NAME}`));
  const body = await screen.findByTestId('zone-modal-body');
  // The provider's own fields arrive with `/providers`; without waiting
  // the enumeration below runs against a form that has not finished
  // seeding and reads three controls as "every control".
  await screen.findByTestId('zone-credential-field-aws_access_key_id');
  return body;
}

/** Open it and ask to delete. Returns the confirmation's own body. */
async function askToDelete() {
  const body = await openZone();
  fireEvent.click(screen.getByTestId('zone-delete'));
  return { body, confirm: await screen.findByTestId('zone-delete-confirm') };
}

/** Every open dialog, as the accessibility tree sees it. Mantine's
 *  `Modal` renders `role="dialog"`; an `Alert` renders nothing of the
 *  sort, which is the whole distinction under test. */
function dialogs() {
  return document.querySelectorAll('[role="dialog"]');
}

/** Every interactive control in a subtree, by tag rather than by name.
 *  A named list would only ever cover the controls whoever wrote it
 *  remembered. */
function controlsIn(root: HTMLElement) {
  return Array.from(
    root.querySelectorAll<HTMLElement>('button, input, textarea, select'),
  );
}

describe('deleting a zone confirms in its own modal, over a locked form — #159', () => {
  test('the confirmation is a second dialog over the Zone modal', async () => {
    const { confirm } = await askToDelete();

    // One surface before, two after. Against the inline `Alert` this
    // reads 1 and the test fails here, naming the panel.
    expect(
      dialogs().length,
      'the delete confirmation did not open a dialog of its own — it is ' +
        'still a panel inside the Zone modal, so the form and the ' +
        'confirmation are one surface with two sets of buttons (#159)',
    ).toBe(2);
    // …and the confirmation is inside one of them, rather than a second
    // dialog having appeared for some unrelated reason. Without this,
    // any stray modal would satisfy the count.
    expect(confirm.closest('[role="dialog"]')).not.toBeNull();
  });

  test('nothing of it is left inside the Zone modal’s body', async () => {
    const { body, confirm } = await askToDelete();

    // The exclusion half. `2` above is satisfiable by rendering the
    // modal *and* leaving the panel in place; this is the reading that
    // is not.
    expect(
      within(body).queryByTestId('zone-delete-confirm'),
      'the confirmation is still rendered inside the Zone modal body, so ' +
        '`Delete this zone` and `Delete <zone>` are still inches apart on ' +
        'one surface (#159)',
    ).toBeNull();
    expect(body.contains(confirm)).toBe(false);
  });

  test('every control beneath it is disabled, and Save sends nothing', async () => {
    const { body } = await askToDelete();

    const controls = controlsIn(body);
    // Vacuity guard. If the form failed to seed, "every control is
    // disabled" is true of the empty set and this test would pass
    // against anything.
    expect(controls.length).toBeGreaterThanOrEqual(8);
    for (const control of controls) {
      expect(
        control,
        `a control under the delete confirmation is still live: ` +
          `${control.tagName.toLowerCase()}` +
          `[data-testid=${control.getAttribute('data-testid') ?? '?'}] — ` +
          'Mantine’s overlay stops a mouse, not a keyboard or a test, so ' +
          'the form beneath must be locked and not merely covered (#159)',
      ).toBeDisabled();
    }

    // The half that matters: clicking `Save` sends nothing. Asserted on
    // `fetch`, because `disabled` is a prop the component set on itself
    // and a handler wired past it would keep the prop and still save.
    fireEvent.click(screen.getByTestId('zone-submit'));
    await act(async () => {
      await Promise.resolve();
    });
    expect(
      mutations(),
      'Save fired a request while the delete confirmation was open — the ' +
        'surface offered save and destroy at the same moment (#159)',
    ).toEqual([]);
  });

  test('no enabled control on screen is labelled Cancel', async () => {
    const { confirm } = await askToDelete();

    // The incident `ZoneModal`'s own comment records, asserted rather
    // than argued about: a `Cancel` that deleted came from two live
    // `Cancel`s, not from two dialogs. Queried across the whole
    // document, both surfaces at once, by accessible name.
    for (const button of screen.queryAllByRole('button', { name: 'Cancel' })) {
      expect(
        button,
        'a `Cancel` is live while the delete confirmation is open — that is ' +
          'the exact shape the incident in ZoneModal’s comment describes (#159)',
      ).toBeDisabled();
    }
    // And the dismissal is not spelled that way at all.
    expect(within(confirm).queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(within(confirm).getByRole('button', { name: 'Keep it' })).toBeEnabled();
  });

  test('a refused delete says so inside the confirmation', async () => {
    deleteResponse = () =>
      new Response('zone still has names bound to it', { status: 409 });
    const { body, confirm } = await askToDelete();

    fireEvent.click(within(confirm).getByTestId('zone-delete-confirmed'));

    const error = await screen.findByTestId('zone-delete-error');
    expect(error.textContent).toContain('zone still has names bound to it');
    // On the surface being looked at. The form's own error `Alert` is
    // at the top of the body, behind the dialog — which is where this
    // went before, and is why `dropZone` no longer uses `onError: fail`.
    expect(
      body.contains(error),
      'the delete error rendered in the form’s own Alert, behind the ' +
        'confirmation dialog — the server’s words went to a surface nobody ' +
        'was looking at (#159)',
    ).toBe(false);
    expect(confirm.contains(error)).toBe(true);
    // The dialog is still up: a refusal is not a dismissal.
    expect(screen.getByTestId('zone-delete-confirm')).toBeTruthy();
  });

  test('the sentence about the blast radius survives verbatim', async () => {
    const { confirm } = await askToDelete();

    // The only place the consequence is stated: the zone, the stored
    // credential, and every name under it — and what it does *not*
    // touch. Whitespace is normalised because the sentence is split
    // across elements (`<code class="ddns-data">`, two `<strong>`s and
    // the name count), but no word of it is.
    const text = confirm.textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain(
      `This destroys ${ZONE_NAME}, its provider binding — stored credentials ` +
        'included — and the 3 names under it.',
    );
    expect(text).toContain(
      'It does not remove whatever your DNS provider has already published ' +
        'for those names; those records stay in the zone with nothing ' +
        'maintaining them.',
    );
  });

  test('dismissing returns to the Zone modal with nothing changed', async () => {
    const { body, confirm } = await askToDelete();

    // By its accessible name, not by a testid this change introduced.
    // The inline panel had a `Keep it` button too, so queried this way
    // the test is answerable in both worlds — which is the point: it is
    // holding a behaviour that was already right, not demonstrating the
    // defect. A guard that fails with "unable to find [data-testid=…]"
    // names the query rather than the fault.
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep it' }));

    await waitFor(() =>
      expect(screen.queryByTestId('zone-delete-confirm')).toBeNull(),
    );
    // The form is still there — dismissing the confirmation must not
    // take the modal with it.
    expect(document.body.contains(body)).toBe(true);
    expect(screen.getByTestId('zone-modal-body')).toBeTruthy();
    // …and usable again, which is the other half of `locked`: a lock
    // that is never released is a broken form.
    expect(screen.getByTestId('zone-submit')).toBeEnabled();
    expect(screen.getByTestId('zone-cancel')).toBeEnabled();
    expect(screen.getByTestId('zone-name')).toBeEnabled();
    // Nothing was sent, in either direction.
    expect(mutations()).toEqual([]);
  });

  test('confirming sends exactly one DELETE and closes both', async () => {
    const { confirm } = await askToDelete();

    fireEvent.click(within(confirm).getByTestId('zone-delete-confirmed'));

    await waitFor(() =>
      expect(screen.queryByTestId('zone-modal-body')).toBeNull(),
    );
    expect(screen.queryByTestId('zone-delete-confirm')).toBeNull();
    expect(mutations()).toEqual([
      { url: `/api/atrium_ddns/domains/${ZONE_ID}`, method: 'DELETE' },
    ]);
  });
});
