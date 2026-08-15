/// <reference types="vite/client" />

// Vite's ambient types: `import.meta.env`, and the module declarations
// that make `import './ddns.css'` a legal side-effect import rather than
// TS2882. The bundle's CSS reaches the page as a runtime `<style>` tag
// (`vite-plugin-css-injected-by-js`, configured by `hostBundleConfig`),
// because a relative `url()` in an emitted stylesheet breaks the moment
// the bundle is served from `system.host_bundle_url` rather than from
// the site root.
