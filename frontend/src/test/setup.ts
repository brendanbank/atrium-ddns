import '@testing-library/jest-dom/vitest';

// jsdom doesn't ship window.matchMedia; Mantine's MantineProvider
// reads it on mount to decide the initial colour scheme. Stub a
// "no preference" response so component tests can render without a
// runtime error.
// jsdom doesn't ship ResizeObserver either, and Mantine's ScrollArea —
// which `Modal` and `Select` mount unconditionally — constructs one in a
// layout effect. Without it the component throws *during commit*, so the
// failure surfaces as an "Uncaught Exception" attributed to whichever
// test happened to be running rather than to the element that needs it.
// A no-op is the right stub: nothing in this bundle's tests asserts on a
// resize, and a stub that fired callbacks would be inventing layout
// events jsdom has no geometry to produce.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class NoopResizeObserver implements ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = NoopResizeObserver;
}

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
