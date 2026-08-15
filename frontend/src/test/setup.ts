import '@testing-library/jest-dom/vitest';

// jsdom doesn't ship window.matchMedia; Mantine's MantineProvider
// reads it on mount to decide the initial colour scheme. Stub a
// "no preference" response so component tests can render without a
// runtime error.
if (typeof window !== 'undefined' && !window.matchMedia) {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}
