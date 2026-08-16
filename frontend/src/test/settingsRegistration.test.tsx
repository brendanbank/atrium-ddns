/** The `registerSettingsGroup` registration — #73's headline artefact.
 *
 * ## Why this file exists separately from `main.test.tsx`
 *
 * `@brendanbank/atrium-test-utils`'s fake registry records
 * `registerHomeWidget`, `registerRoute`, `registerNavItem`,
 * `registerAdminTab`, `registerProfileItem`, `registerNotificationKind`
 * and `registerLocale` — and **not** `registerSettingsGroup`. So on the
 * fake, `reg.registerSettingsGroup` is `undefined`, `main.tsx` takes its
 * fallback branch, and every assertion about the group would pass
 * against a bundle that never registered one. That is the
 * "probe that could not fail" family aimed at a registry: the sweep in
 * `main.test.tsx` counts twelve rendered surfaces whether or not the
 * group exists, because a group is not a rendered surface.
 *
 * The recorder below is installed on the fake registry *before*
 * `main.tsx` is imported, which is the only moment that matters —
 * registration is an import-time side effect.
 *
 * The vacuity guard is the first test: it asserts the property that
 * makes this file necessary (the fake does not carry the method), so if
 * a future SDK release adds it, this file says so instead of quietly
 * becoming a duplicate of a test that now works elsewhere.
 */
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

import {
  mockAtriumRegistry,
  type MockAtriumHandles,
} from '@brendanbank/atrium-test-utils';

import { CONFIG_PERMISSION } from '../api/config';
import {
  SETTINGS_GROUP_KEY,
  SETTINGS_GROUP_KEYS,
  SETTINGS_LABELS,
  SETTINGS_ROUTES,
} from '../settings/settingsRoutes';

type Group = {
  key: string;
  label: string;
  section?: string;
  perm?: string;
  order?: number;
  children: { key: string; label: string; to: string; perm?: string }[];
};

let handles: MockAtriumHandles;
let groups: Group[];

beforeEach(() => {
  handles = mockAtriumRegistry({ me: null });
  groups = [];
  vi.resetModules();
});

afterEach(() => {
  handles?.cleanup();
});

function record(): void {
  (
    handles.registry as unknown as {
      registerSettingsGroup?: (group: Group) => void;
    }
  ).registerSettingsGroup = (group: Group) => {
    groups.push(group);
  };
}

test('the test-utils fake still does not record settings groups', () => {
  // The premise of this file, asserted rather than assumed. If this
  // starts failing, the SDK grew the recorder and these tests can move
  // into `main.test.tsx` against `handles.settingsGroups`.
  expect(
    (handles.registry as unknown as Record<string, unknown>)
      .registerSettingsGroup,
  ).toBeUndefined();
});

test('the bundle registers one settings group with a child per page', async () => {
  record();
  await import('../main');

  expect(groups, 'registerSettingsGroup was never called').toHaveLength(1);
  const group = groups[0];
  expect(group.key).toBe(SETTINGS_GROUP_KEY);
  // Atrium's own config sections live in the admin bucket and gate on
  // this permission. Sitting beside them is the point.
  expect(group.section).toBe('admin');
  expect(group.perm).toBe(CONFIG_PERMISSION);
  expect(group.children.map((child) => child.key)).toEqual(
    SETTINGS_GROUP_KEYS,
  );
});

test('every child points at a path the bundle actually registered', async () => {
  record();
  const main = await import('../main');
  expect(main).toBeDefined();

  // A `SettingsGroup` child is nav-only: atrium's `/admin/:section`
  // route does not look groups up, so a child whose `to` names a path
  // no route serves is a dead sidebar entry that no component test can
  // see. Compared against the *registry's* route table, not against the
  // map both were built from.
  const paths = new Set(handles.routes.map((route) => route.path));
  for (const child of groups[0].children) {
    expect(child.to).toBe(SETTINGS_ROUTES[child.key]);
    expect(child.label).toBe(SETTINGS_LABELS[child.key]);
    expect(paths.has(child.to), `${child.to} is registered by no route`).toBe(
      true,
    );
  }
  // Vacuity: three children, not zero.
  expect(groups[0].children.length).toBe(3);
});

test('the pages are registered even when the group cannot be', async () => {
  // The fallback branch, exercised: on an atrium older than 0.25 the
  // registry has no `registerSettingsGroup`, and the pages must still
  // be reachable by URL. Silence here would reproduce #73 exactly — a
  // surface that exists and nothing names.
  const errors: unknown[][] = [];
  const spy = vi
    .spyOn(console, 'error')
    .mockImplementation((...args: unknown[]) => {
      errors.push(args);
    });
  try {
    await import('../main');
  } finally {
    spy.mockRestore();
  }

  expect(groups).toHaveLength(0);
  const paths = new Set(handles.routes.map((route) => route.path));
  for (const key of SETTINGS_GROUP_KEYS) {
    expect(paths.has(SETTINGS_ROUTES[key])).toBe(true);
  }
  // …and it said so, naming what was lost and what still works.
  const said = errors.flat().join(' ');
  expect(said).toContain('registerSettingsGroup');
  expect(said).toContain(SETTINGS_ROUTES['rate-limits']);
});
