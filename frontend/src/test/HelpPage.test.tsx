/** The help entry — #75, ui-parity §3.3 G5.
 *
 * `GET /admin/help` swept to a **negative result** across both deployed
 * bundles: seven spellings each, zero help surfaces anywhere. So the
 * first thing this file asserts is that there is now exactly one, and
 * that it is registered rather than merely written — a page nothing
 * mounts is the same absence one indirection along.
 *
 * The assertion that earns its place, though, is the second one:
 * **every route this bundle registers appears on the help page.** The
 * page's own list is built from the registrations' path constants, so
 * this closes the other direction — a surface added later without a
 * help entry fails here instead of leaving the page quietly incomplete.
 * A hand-kept list of five links would pass a "the page renders" test
 * forever.
 */
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { DOCUMENTS, DOCS_BASE, HelpPage, SURFACES } from '../HelpPage';
import { queryClient } from '../queryClient';

const ANYONE: UserContext = {
  id: 1,
  email: 'operator@example.com',
  full_name: 'Operator',
  is_active: true,
  roles: ['user'],
  // Deliberately empty. This page reads no tenant data and calls no
  // endpoint, so there is nothing here to be refused — and a help page
  // that needed a permission would be unreachable to exactly the person
  // most likely to want it.
  permissions: [],
  impersonating_from: null,
};

let handles: MockAtriumHandles;
let requests: string[] = [];

beforeEach(() => {
  requests = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      requests.push(url);
      if (url.endsWith('/users/me/context')) {
        return new Response(JSON.stringify(ANYONE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response('{}', { status: 200 });
    }),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  queryClient.clear();
  vi.unstubAllGlobals();
  vi.resetModules();
});

function render() {
  handles = mockAtriumRegistry({ me: ANYONE });
  return renderWithAtrium(<HelpPage />);
}

test('the bundle registers exactly one help route and one nav item', async () => {
  handles = mockAtriumRegistry({ me: ANYONE });
  vi.resetModules();
  const main = await import('../main');

  const route = handles.routes.filter((r) => r.key === 'atrium-ddns-help');
  const nav = handles.navItems.filter((n) => n.key === 'atrium-ddns-help-nav');
  expect(route, 'the help route was never registered').toHaveLength(1);
  expect(nav, 'the help nav item was never registered').toHaveLength(1);
  // One constant, two consumers: a nav item pointing at a path no route
  // serves is a dead link that no component test can see.
  expect(route[0].path).toBe(main.HELP_PATH);
  expect(nav[0].to).toBe(main.HELP_PATH);
  // No `perm`. Not for the refusal-versus-empty reason its neighbours
  // carry — this page has nothing to refuse.
  expect(nav[0]).not.toHaveProperty('perm');
});

test('every route this bundle registers is listed on the help page', async () => {
  handles = mockAtriumRegistry({ me: ANYONE });
  vi.resetModules();
  const main = await import('../main');

  const listed = new Set(SURFACES.map((s) => s.to));
  // The scaffold's demo page and the help page itself are the two
  // registrations that are deliberately not in the list: one is the
  // template's placeholder and the other is this page.
  const exempt = new Set(['/atrium-ddns', main.HELP_PATH]);

  const missing = handles.routes
    .map((route) => route.path)
    .filter((path): path is string => typeof path === 'string')
    .filter((path) => !listed.has(path) && !exempt.has(path));

  expect(
    missing,
    'these routes are registered and have no entry on the help page',
  ).toEqual([]);
  // Vacuity: the sweep has to be over a real population, and the list
  // must not name a path nothing serves.
  expect(handles.routes.length).toBeGreaterThan(exempt.size);
  const served = new Set(
    handles.routes.map((route) => route.path).filter(Boolean),
  );
  for (const to of listed) {
    expect(served.has(to), `${to} is on the help page and is not a route`).toBe(
      true,
    );
  }
});

test('the page renders both lists and calls nothing', async () => {
  render();
  await waitFor(() =>
    expect(screen.getByTestId('help-surfaces')).toBeInTheDocument(),
  );
  expect(screen.getByTestId('help-documents')).toBeInTheDocument();
  for (const surface of SURFACES) {
    expect(screen.getByTestId(`help-to-${surface.to}`)).toHaveTextContent(
      surface.label,
    );
  }
  for (const doc of DOCUMENTS) {
    expect(screen.getByTestId(`help-doc-${doc.path}`)).toHaveAttribute(
      'href',
      `${DOCS_BASE}/${doc.path}`,
    );
  }
  // It reads no tenant data, so the only request it can be responsible
  // for is atrium's own identity probe.
  expect(requests.filter((url) => url.includes('atrium_ddns'))).toEqual([]);
});

test('the documentation is linked rather than claimed to be served here', () => {
  // `docs/` is not copied into the image — the Dockerfile copies
  // `backend/` and the built bundle and nothing else. A page that read
  // a Markdown file at run time would work in a checkout and 404 in the
  // container, which is the template's "a file read at runtime must be
  // copied into the image" trap. Every link is therefore absolute.
  render();
  for (const doc of DOCUMENTS) {
    expect(
      DOCS_BASE.startsWith('https://'),
      'a relative link would resolve against this SPA, which does not serve docs/',
    ).toBe(true);
    expect(doc.path.endsWith('.md')).toBe(true);
  }
  expect(screen.getByTestId('help-docs-caveat')).toHaveTextContent(
    'not copied into the deployed image',
  );
});
