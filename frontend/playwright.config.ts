import { defineConfig } from '@playwright/test';

/**
 * End-to-end harness for the atrium-ddns host bundle.
 *
 * Deliberately shaped like atrium's own `frontend/playwright.config.ts`
 * so the two are learnable as one thing: `testDir: './tests-e2e'`,
 * one worker, `list` + `html` reporters, `trace: 'on-first-retry'`,
 * and a `baseURL` read from the environment rather than guessed.
 *
 * Two differences, each for a reason:
 *
 *   * **No `smoke` / `extended` split.** Atrium has ~20 specs and needs
 *     a PR-gating subset. This repo has three, and all three are the
 *     floor — there is nothing to leave out.
 *   * **A fixed 1440x900 viewport.** `docs/ops/ui-design.md` §12 picks
 *     a detail *route* over a drawer on the strength of a width budget
 *     measured at a 1440px viewport (atrium's shell gives 1168px there,
 *     and one resolution strip needs ~592px). A harness that renders
 *     the signature element at whatever width the machine defaults to
 *     is not testing the thing that was designed.
 *
 * The stack is expected to be up already — `make test-e2e` raises it,
 * migrates it, seeds the admin and promotes the bundle first. The
 * default port is this repo's own (8053, see `.env.example`); the make
 * target overrides it from `.env` so several worktrees can run at once.
 */
export default defineConfig({
  testDir: './tests-e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  // No retries locally, on purpose. The acceptance criterion for this
  // harness is that consecutive runs are green without them; a retry
  // would convert exactly the flakiness being measured into a pass.
  retries: process.env.CI ? 1 : 0,
  /* Short on purpose. A real failure here shows up in ten to fifteen
     seconds — a selector that will never match does not become more
     likely with time — so Playwright's 30s default only lengthens the
     feedback loop when you are iterating on specs. A slow *stack* is a
     different problem and the fix for it is not a longer wait. */
  timeout: 15_000,
  expect: { timeout: 5_000 },
  workers: 1,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:8053',
    viewport: { width: 1440, height: 900 },
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'e2e', use: { browserName: 'chromium' } }],
});
