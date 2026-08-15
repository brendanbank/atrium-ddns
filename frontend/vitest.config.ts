import { defineConfig } from 'vitest/config';

// Vite's built-in esbuild loader handles tsx via the project's
// tsconfig (`jsx: react-jsx`). No `@vitejs/plugin-react` needed for
// unit tests — the package isn't listed as a peer dep so we'd be
// adding one for tests alone, which the host-bundle pattern keeps
// out by design.
export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    server: {
      deps: {
        // `@brendanbank/atrium-host-bundle-utils@0.27.0` ships
        // extensionless relative ESM imports (`dist/react/index.js`
        // does `export { __atrium_t__ } from '../i18n'`). Vite's
        // bundler resolution is fine with that, so `pnpm build`
        // works — but vitest externalises node_modules by default and
        // Node's native ESM loader requires the extension, so every
        // suite importing the package fails to collect with
        // `Cannot find module .../dist/i18n`. Inlining routes it
        // through Vite's resolver instead. Drop this once the
        // upstream package emits extensioned specifiers.
        // `atrium-test-utils` re-exports through it, so both have to
        // be inlined — inlining only the leaf leaves the importer
        // externalised and the same error is raised from there.
        inline: [
          '@brendanbank/atrium-host-bundle-utils',
          '@brendanbank/atrium-test-utils',
        ],
      },
    },
  },
});
