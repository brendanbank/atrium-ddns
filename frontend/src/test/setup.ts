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
//
// Two issues found this independently and described the same fault from
// two angles — #45 through `Modal`, #46 through `Select`. Worth noting
// because the symptom is the misleading part: #46 saw only "An error
// occurred in the <Scrollbar> component" on stderr and an empty
// `<body>`, so every query in the file failed with "unable to find an
// element" and none of them said why.
if (typeof globalThis.ResizeObserver === 'undefined') {
  class NoopResizeObserver implements ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = NoopResizeObserver;
}

// Same shape, second element: Mantine's combobox scrolls the active
// option into view when the dropdown opens, and jsdom's Element has no
// `scrollIntoView`. Needed by #46's filter selects; harmless otherwise.
if (
  typeof Element !== 'undefined' &&
  !(Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
) {
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    writable: true,
    value: () => {},
  });
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

// jsdom doesn't ship `document.fonts` (the CSS Font Loading API), and
// Mantine's autosize `Textarea` subscribes to it unguarded —
// `document.fonts.addEventListener('loadingdone', …)` in a `useEffect`,
// so the throw lands in React's passive-effect commit and surfaces as
// "Cannot read properties of undefined (reading 'addEventListener')"
// with no mention of fonts, textareas or Mantine anywhere in it.
//
// A no-op FontFaceSet is enough: nothing here asserts on font loading,
// and the listener exists only to re-measure after a webfont swaps in.
if (typeof document !== 'undefined' && !document.fonts) {
  Object.defineProperty(document, 'fonts', {
    writable: true,
    value: {
      addEventListener: () => {},
      removeEventListener: () => {},
      ready: Promise.resolve(),
    },
  });
}
