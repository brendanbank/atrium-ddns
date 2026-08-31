import { expect, test } from '@playwright/test';

import {
  DOMAINS_PATH,
  chooseFromSelect,
  loginAsUser,
  uniqueZoneName,
} from './helpers';

/**
 * #88 — a zone that publishes nowhere answers `911` and looked fine.
 *
 * ## What this spec is for
 *
 * `docs/ops/ui-design.md` Part II §8.1 states the defect as a wire fact
 * rather than as a preference: the create-zone modal offered one field
 * and a Create button, so **one click produced a zone whose every
 * update answers `911`** — frozen at
 * `tests/compat/protocol_cases.yaml:211`, `update/no-backends-911` —
 * and the list then drew that zone identically to a working one.
 *
 * The unit tests assert what leaves the browser. This asserts what an
 * operator sees, in a browser, which is the thing nobody had checked:
 * *"This entire milestone exists because UI was shipped that nobody had
 * loaded."*
 *
 * ## The `911` is demonstrated, not quoted
 *
 * The second test below takes the "add a provider later" link, creates a
 * name inside the resulting zone through the UI, and then has a device
 * call `/nic/update` exactly as a router does. The assertion is on the
 * wire body. Quoting the YAML would prove that the table says `911`;
 * this proves the deployed service answers it, on a zone this UI just
 * created, today.
 *
 * The control matters as much as the claim: the *same* device, on a name
 * in a zone that **does** have a provider, gets a different answer. A
 * test that only saw `911` could not distinguish "no provider" from
 * "this stack is broken".
 *
 * ## Layout
 *
 * `frontend/tests-e2e/`, atrium's own convention
 * (`/Users/brendan/src/atrium/frontend/tests-e2e/`), and the helper
 * vocabulary is #91's — `loginAsUser`, `deviceCallsIn`,
 * `chooseFromSelect`, `uniqueZoneName`. This file therefore **depends on
 * #91 having landed**; it is written to that harness and does not carry
 * a second copy of it.
 */

/** The credential the create form stores. Never used to reach AWS: the
 *  zone is under RFC 6761 `.invalid`, nothing resolves it, and every
 *  address on screen is RFC 5737 documentation space. Named so that a
 *  reader of a screenshot cannot mistake it for a real key. */
const DEMO_CREDENTIAL = {
  aws_access_key_id: 'AKIA-E2E-NOT-A-SECRET',
  aws_secret_access_key: 'e2e-not-a-secret',
};

test.describe('#88 — zones and providers are one object', () => {
  test.describe.configure({ timeout: 30_000 });

  test('a zone and its first provider arrive in one submission', async ({
    page,
  }) => {
    // Surface a crashing render rather than waiting 30s for a locator on
    // a blank page — the Mantine failure mode the playwright-debug skill
    // catalogues.
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));

    // Every request the browser makes, so "one submission" can be
    // asserted as *one request* rather than as one button press. Two
    // requests would be able to half-succeed, and the half that succeeds
    // is the zone.
    const creates: string[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && request.url().includes('atrium_ddns')) {
        creates.push(request.url());
      }
    });

    await loginAsUser(page);
    const zone = uniqueZoneName();

    await page.goto(DOMAINS_PATH);
    await expect(page.getByTestId('domains-empty')).toBeVisible({
      timeout: 15_000,
    });
    await page.getByTestId('add-domain').click();

    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible();
    // The zone field and the provider fields are in one modal, in one
    // reading order — §10.1's wireframe, rendered.
    await modal.getByTestId('zone-name').fill(zone);
    await expect(modal.getByTestId('zone-provider')).toBeVisible();
    // Pick the provider explicitly. `BackendForm` defaults to
    // `providers[0]`, and `known_services()` is **sorted**, so the
    // default is `hetzner` — whose one credential key is
    // `hetzner_api_token`, not the route53 pair below. Adjusted by #91
    // when this spec was first run: it is a fact about the catalogue's
    // order, which nothing in the file could have known unrun.
    await chooseFromSelect(page, 'zone-provider', 'route53');
    // Assert the select took before touching the submit.
    //
    // `zone-submit` is disabled until a provider is chosen, so a click
    // on it after a select that silently missed does not fail — it
    // waits for the button to become actionable and times out thirty
    // seconds later, naming the button instead of the cause. This
    // only showed up in the full run, where the providers query is
    // warm and the option list can render a beat after the click.
    await expect(modal.getByTestId('zone-provider')).toHaveValue(
      'route53',
    );
    await expect(modal.getByTestId('zone-submit')).toBeEnabled();
    // The credential fields come from `GET /providers`, i.e. from
    // `BaseProvider.REQUIRED_CREDENTIALS`. This is `BackendForm`, not a
    // create-only copy of it.
    for (const [key, value] of Object.entries(DEMO_CREDENTIAL)) {
      await modal.getByTestId(`zone-credential-field-${key}`).fill(value);
    }
    await modal.getByTestId('zone-submit').click();

    const row = page.getByTestId(`domain-${zone}`);
    await expect(row).toBeVisible({ timeout: 8_000 });
    // Not diverged. §1.2 Rule 1 — agreement has no colour, so a working
    // zone carries no mark at all.
    await expect(row).toHaveAttribute('data-diverged', 'false');
    await expect(row).not.toContainText('publishes nowhere');
    await expect(row).not.toContainText('911');
    // The row shows the provider itself now, not a count — the count
    // restated what the card would show and named nothing actionable.
    await expect(page.getByTestId(`provider-${zone}`)).toContainText(
      'route53',
    );

    // One request, and it is the zone's. A second POST to `/backends`
    // would mean the browser had assembled the state in two steps.
    expect(
      creates.filter((url) => url.includes('/atrium_ddns/domains')),
    ).toHaveLength(1);
    expect(creates.filter((url) => url.includes('/backends'))).toHaveLength(0);

    // --- the detail route, §10.2 -----------------------------------
    //
    // **Rewritten by #97.** A plain click on the row now opens the card
    // in a *modal* — Part III §17, the operator's reversal of §12 —
    // rather than navigating. The route is not gone and is not
    // demoted: §12's two surviving arguments are linkability and Back,
    // both properties of the URL, so the row is still an `<a href>` and
    // the address still resolves. That is what is exercised here.
    // #97's own spec (`card-affordance.spec.ts`) covers the modal.
    const detailUrl = new URL(
      (await page
        .getByTestId(`open-domain-${zone}`)
        .getAttribute('href')) as string,
      page.url(),
    ).toString();
    // The address is a query parameter on the list route now, not a
    // path segment. §17: two registered routes meant opening and closing
    // swapped atrium's route element, which unmounts the host root and
    // orphaned the portalled modal.
    expect(detailUrl).toMatch(/\/atrium-ddns\/domains\?zone=\d+$/);
    await page.goto(detailUrl);
    await expect(page).toHaveURL(/\/atrium-ddns\/domains\?zone=\d+$/);

    const card = page.getByTestId('zone-modal-body');
    await expect(card).toBeVisible({
      timeout: 15_000,
    });
    // The provider is listed *inside* the zone, which is the whole of
    // §10.2: the previous build nested it in an accordion on a shared
    // list page, three clicks from the thing it describes.
    // One provider per zone, so it is the Provider field's value rather
    // than a list entry — the per-name checkbox list is gone with the
    // model that made a zone hold several.
    await expect(card.getByTestId('zone-provider')).toHaveValue('route53');
    await expect(card.getByTestId('zone-name')).toHaveValue(
      zone,
    );
    // The credential is a word, never a masked value, and never a
    // prefix — "a prefix of an API token is still a disclosure".
    // The wording moved with the rewrite; what is asserted is the
    // property, not the sentence — a credential is acknowledged and never
    // echoed. Pinning the exact prose made this fail on a copy edit.
    await expect(page.locator('body')).toContainText(/credential is stored/i);
    await expect(page.locator('body')).not.toContainText('AKIA-E2E');

    // The width the route exists to preserve (§12). Asserted rather
    // than assumed: the argument for a route over a Mantine `lg` drawer
    // is that 620px is below the 592px one-strip minimum once the
    // drawer's own padding is taken, and a detail surface narrower than
    // its own signature element is the failure the drawer was rejected
    // for.
    const contentWidth = await page
      .getByTestId('zone-modal-body')
      .evaluate((el) => el.getBoundingClientRect().width);
    expect(contentWidth).toBeGreaterThan(592);

    // Back works — the second of §12's two survivors. A drawer teaches
    // the browser nothing, and neither would a modal; this is the
    // reason §17 kept the route rather than replacing it.
    await page.goBack();
    await expect(page).toHaveURL(new RegExp(`${DOMAINS_PATH}$`));

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });

  test('a zone cannot be created without a provider', async ({ page }) => {
    // The inverse of the test this replaces.
    //
    // §10.1 kept an "add a provider later" link on the argument that
    // staging a migration is a real reason to want a zone before its
    // credentials exist. The operator overruled it: a zone with no
    // provider answers `911` for every update under it, and a form whose
    // only escape hatch produces that state is offering a trap.
    //
    // So the assertion is that the hatch is gone — and that the submit
    // refuses rather than silently creating the zone alone. A spec that
    // only checked the link's absence would pass against a form that had
    // simply hidden it while still posting `backend: null`.
    const pageErrors: Error[] = [];
    page.on('pageerror', (error) => pageErrors.push(error));

    await loginAsUser(page);
    await page.goto(DOMAINS_PATH);
    await page.getByTestId('add-domain').click();

    const modal = page.getByRole('dialog');
    await expect(modal).toBeVisible();
    await expect(modal.getByTestId('zone-later-link')).toHaveCount(0);
    await expect(modal.getByTestId('zone-later-submit')).toHaveCount(0);

    // A name but no provider: the submit stays unavailable.
    await modal.getByTestId('zone-name').fill(`nohatch-${Date.now()}.example.invalid`);
    await expect(modal.getByTestId('zone-submit')).toBeDisabled();

    expect(pageErrors, 'the page threw while rendering').toEqual([]);
  });
});
