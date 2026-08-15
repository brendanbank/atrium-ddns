/** The two Mantine traps, asserted rather than commented.
 *
 * Both were read out of `@mantine/core@9`'s installed source by #43 and
 * both **silently corrupt an implementation rather than failing** — a
 * nested provider that restyles atrium's shell, and a colour-scheme prop
 * that stops following atrium after the first mount. A comment saying
 * "we pass `forceColorScheme`" survives any refactor that removes it;
 * these tests do not.
 *
 * The mutation each test catches is named in its own body.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { cleanup, screen } from '@testing-library/react';

import {
  mockAtriumRegistry,
  renderWithAtrium,
  type MockAtriumHandles,
} from '@brendanbank/atrium-test-utils';
import { __resetAtriumColorSchemeCacheForTests } from '@brendanbank/atrium-host-bundle-utils/react';

import { DDNS_ROOT_ATTRIBUTE, DdnsRoot } from '../host/DdnsRoot';
import { queryClient } from '../queryClient';

let handles: MockAtriumHandles;

beforeEach(() => {
  handles = mockAtriumRegistry({ me: null });
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response(null, { status: 401 })),
  );
});

afterEach(() => {
  cleanup();
  handles?.cleanup();
  queryClient.clear();
  document.documentElement.removeAttribute('data-mantine-color-scheme');
  delete (window as { __ATRIUM_COLOR_SCHEME__?: string }).__ATRIUM_COLOR_SCHEME__;
  __resetAtriumColorSchemeCacheForTests();
  vi.unstubAllGlobals();
});

describe('the wrapper', () => {
  test('carries the attribute the stylesheet is scoped by', () => {
    renderWithAtrium(
      <DdnsRoot>
        <p data-testid="child">x</p>
      </DdnsRoot>,
    );
    const child = screen.getByTestId('child');
    expect(child.closest(`[${DDNS_ROOT_ATTRIBUTE}]`)).not.toBeNull();
  });
});

describe('trap 1 — a nested MantineProvider writes to :root', () => {
  test('the host provider emits no CSS variables at all', () => {
    // `MantineProvider.mjs:34` resolves `cssVariablesSelector ?? ":root"`,
    // so a nested provider's variables land on atrium's own `:root`.
    // Today the emitted sheet happens to be empty because
    // `MantineCssVariables` deduplicates against the default theme and
    // this provider passes no theme override — an accident of two
    // defaults lining up. `withCssVariables={false}` makes it structural.
    //
    // The mutation this catches: deleting `withCssVariables={false}`
    // *and* adding any `theme` prop. The first alone is currently
    // survivable, which is precisely why the assertion is on the
    // absence of the emitter rather than on the absence of a colour.
    //
    // `data-mantine-styles="true"` is the variables emitter;
    // `="classes"` is `MantineClasses`, which ships the
    // `hiddenFrom`/`visibleFrom` media queries and writes no variable
    // and no `:root` rule. Asserting on the bare attribute would fail on
    // the harmless one and say nothing about the harmful one — measured,
    // because the first version of this test did exactly that.
    renderWithAtrium(
      <DdnsRoot>
        <p>x</p>
      </DdnsRoot>,
    );
    expect(
      document.querySelectorAll('style[data-mantine-styles="true"]'),
    ).toHaveLength(0);

    // The property that actually matters, stated directly rather than
    // borrowed from an attribute value: nothing this tree emits declares
    // a `--mantine-*` variable or targets `:root`.
    const emitted = [...document.querySelectorAll('style[data-mantine-styles]')];
    expect(emitted.length, 'vacuity: the provider emitted nothing at all').toBe(
      1,
    );
    for (const style of emitted) {
      expect(style.textContent).not.toMatch(/--mantine-/);
      expect(style.textContent).not.toMatch(/:root/);
    }
  });
});

describe('trap 2 — the colour scheme prop is not controlled', () => {
  test.each(['light', 'dark'] as const)(
    'does not rewrite atrium’s [data-mantine-color-scheme] (%s)',
    (shellScheme) => {
      // Atrium has set the attribute on <html>. The host bundle then
      // mounts believing the scheme is the *other* one — the exact
      // divergence `use-provider-color-scheme.mjs` produces when
      // localStorage holds a stale value and `defaultColorScheme` is
      // used as if it were controlled.
      document.documentElement.setAttribute(
        'data-mantine-color-scheme',
        shellScheme,
      );
      const stale = shellScheme === 'dark' ? 'light' : 'dark';
      (window as { __ATRIUM_COLOR_SCHEME__?: string }).__ATRIUM_COLOR_SCHEME__ =
        stale;
      __resetAtriumColorSchemeCacheForTests();

      renderWithAtrium(
        <DdnsRoot>
          <p data-testid="child">x</p>
        </DdnsRoot>,
      );

      // The shell's attribute is untouched. `getRootElement={() =>
      // undefined}` is what does it: `setColorSchemeAttribute` is
      // `getRootElement()?.setAttribute(…)`, so removing the element
      // removes the write. Delete that prop and this test reads `light`
      // where it expects `dark`.
      expect(
        document.documentElement.getAttribute('data-mantine-color-scheme'),
      ).toBe(shellScheme);
      expect(screen.getByTestId('child')).toBeInTheDocument();
    },
  );

  test('an auto scheme is passed as undefined rather than coerced', () => {
    // `useAtriumColorScheme()` can return `'auto'`; `forceColorScheme`'s
    // type is `'light' | 'dark'`. Coercing `'auto'` to `'light'` would
    // pin the host tree light on every shell whose preset resolves auto
    // — which is four of atrium's five.
    document.documentElement.setAttribute('data-mantine-color-scheme', 'dark');
    (window as { __ATRIUM_COLOR_SCHEME__?: string }).__ATRIUM_COLOR_SCHEME__ =
      'auto';
    __resetAtriumColorSchemeCacheForTests();

    renderWithAtrium(
      <DdnsRoot>
        <p data-testid="child">x</p>
      </DdnsRoot>,
    );

    expect(
      document.documentElement.getAttribute('data-mantine-color-scheme'),
    ).toBe('dark');
  });
});
