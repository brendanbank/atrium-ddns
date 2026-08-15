/** Worked example of testing a host bundle component with
 *  `@brendanbank/atrium-test-utils`.
 *
 *  The widget calls `usePerm()` to gate the bump button on the
 *  `atrium_ddns.write` permission. The test installs the fakes once
 *  via `mockAtriumRegistry` and asserts the gating without standing up
 *  the SPA.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
  type UserContext,
} from '@brendanbank/atrium-test-utils';

import { AtriumDdnsWidget } from '../AtriumDdnsWidget';
import { queryClient } from '../queryClient';

const EDITOR: UserContext = {
  id: 1,
  email: 'admin@example.com',
  full_name: 'Admin',
  is_active: true,
  roles: ['admin'],
  permissions: ['atrium_ddns.write'],
  impersonating_from: null,
};

const VIEWER: UserContext = {
  id: 2,
  email: 'viewer@example.com',
  full_name: 'Viewer',
  is_active: true,
  roles: [],
  permissions: [],
  impersonating_from: null,
};

let handles: MockAtriumHandles;
let currentMe: UserContext | null = null;

function stubFetch() {
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
      if (url.endsWith('/atrium_ddns/state')) {
        return new Response(
          JSON.stringify({ message: 'Hello from Atrium Ddns', counter: 7 }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response('{}', { status: 200 });
    }),
  );
}

beforeEach(() => {
  stubFetch();
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  // The widget self-wraps in a module-singleton QueryClient so clear
  // it between tests, otherwise the previous test's `me` leaks via
  // the cache.
  queryClient.clear();
  currentMe = null;
  vi.unstubAllGlobals();
});

describe('AtriumDdnsWidget', () => {
  test('users with atrium_ddns.write can press the bump button', async () => {
    currentMe = EDITOR;
    handles = mockAtriumRegistry({ me: EDITOR });
    renderWithAtrium(<AtriumDdnsWidget />);
    const button = (await screen.findByTestId('atrium-ddns-bump')) as HTMLButtonElement;
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(screen.getByText('Bump counter')).toBeInTheDocument();
  });

  test('users without atrium_ddns.write see the disabled label', async () => {
    currentMe = VIEWER;
    handles = mockAtriumRegistry({ me: VIEWER });
    renderWithAtrium(<AtriumDdnsWidget />);
    const button = (await screen.findByTestId('atrium-ddns-bump')) as HTMLButtonElement;
    await waitFor(() =>
      expect(screen.getByText(/admin only/)).toBeInTheDocument(),
    );
    expect(button).toBeDisabled();
  });
});
