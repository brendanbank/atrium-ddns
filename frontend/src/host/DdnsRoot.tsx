/** The one wrapper every registration in this bundle mounts inside.
 *
 * Atrium loads the bundle and calls `register*`; each `render()` hands
 * back `makeWrapperElement(<Something />)`, so atrium's React owns a
 * `<div>` and *this* tree — the host's React — owns everything under
 * it. That subtree needs its own `MantineProvider`, its own
 * `QueryClientProvider` and atrium's `AtriumProvider`, and getting any
 * of the three wrong is silent rather than loud.
 *
 * ## Why this is a component and not four copies of the same JSX
 *
 * The scaffold repeated the provider stack in every registered
 * component. Three of the props below are *corrections* — a repeated
 * stack is three places to get each of them wrong, and issues #45 and
 * #46 add more registrations on top of this one. Mount inside
 * `<DdnsRoot>` and the corrections come with you.
 *
 * ## The three props that are not decoration
 *
 * All three were read out of `@mantine/core@9`'s installed source
 * (`esm/core/MantineProvider/…`), which is the instrument
 * `docs/ops/ui-design.md` §5 used, and each one silently corrupts the
 * host application rather than failing.
 *
 * **1. `withCssVariables={false}`.** `MantineProvider.mjs:34` resolves
 * `cssVariablesSelector ?? ":root"`, so a nested provider writes its
 * CSS variables to `:root` — atrium's own. Today that happens to be
 * harmless because `MantineCssVariables` deduplicates against the
 * default theme and this provider passes no theme override, so the
 * emitted stylesheet is empty. That is an accident of two defaults
 * lining up, not a guarantee: the moment anything passes a `theme`
 * prop, the host restyles atrium's shell. Turning the emitter off
 * makes it structural. The host inherits every `--mantine-*` from
 * atrium's `:root`, which is what `docs/theme.md` requires and what an
 * operator's branding depends on.
 *
 * **2. `getRootElement={() => undefined}`.**
 * `use-provider-color-scheme.mjs` writes `data-mantine-color-scheme`
 * onto `getRootElement()`, which defaults to `document.documentElement`
 * — the shell's attribute, from inside the host's provider, on mount
 * and on every scheme change. `setColorSchemeAttribute` is
 * `getRootElement()?.setAttribute(…)`, so returning `undefined` removes
 * the write entirely. Atrium owns that attribute and is always right
 * about it; this bundle reads it and never writes it.
 *
 * **3. `forceColorScheme`, never `defaultColorScheme`.**
 * `useState(() => manager.get(defaultColorScheme))` runs its
 * initialiser once and `localStorageColorSchemeManager` wins whenever a
 * stored value exists — so `defaultColorScheme` is not a controlled
 * prop and the scaffold's `<MantineProvider defaultColorScheme={scheme}>`
 * stops following atrium after the first mount. `forceColorScheme`
 * short-circuits both the manager and the state
 * (`const colorSchemeValue = forceColorScheme || value`).
 *
 * `useAtriumColorScheme()` can return `'auto'`, which `forceColorScheme`
 * does not accept — its type is `'light' | 'dark'`. `'auto'` is passed
 * as `undefined`, which is correct rather than a fallback: with
 * `getRootElement` returning `undefined` this provider cannot write the
 * attribute either way, and every colour in `ddns.css` keys off the
 * attribute atrium already set. The scheme value is not on the path
 * that decides how anything looks; it is there so Mantine's own
 * components (which read `colorScheme` from context) match.
 *
 * ## The wrapper div
 *
 * `data-ddns-root` is the selector `ddns.css` hangs the six palette
 * values off. It is on the div rather than on `:root` because a host
 * bundle owns its own subtree and nothing else — the same boundary the
 * three props above defend from the JS side.
 */
import { MantineProvider } from '@mantine/core';
import { QueryClientProvider } from '@tanstack/react-query';
import {
  AtriumProvider,
  useAtriumColorScheme,
} from '@brendanbank/atrium-host-bundle-utils/react';
import type { ReactNode } from 'react';

import { queryClient } from '../queryClient';
import '../ddns.css';

/** Attribute the host's CSS is scoped by. Exported so a test can look
 *  for the real string rather than restating it. */
export const DDNS_ROOT_ATTRIBUTE = 'data-ddns-root';

export function DdnsRoot({ children }: { children: ReactNode }) {
  const scheme = useAtriumColorScheme();
  return (
    <MantineProvider
      forceColorScheme={scheme === 'auto' ? undefined : scheme}
      withCssVariables={false}
      getRootElement={() => undefined}
    >
      <QueryClientProvider client={queryClient}>
        <AtriumProvider>
          <div data-ddns-root="">{children}</div>
        </AtriumProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}
