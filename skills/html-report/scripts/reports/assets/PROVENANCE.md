# Vendored ECharts

`echarts.min.js` — Apache ECharts **6.1.0**, minified UMD build.

- **License**: Apache-2.0 (license header retained at the top of the file).
- **sha256**: `659dafd219f4db91efb12e6c59ba3a8a571f9917cd8d9075fb0df582d199e13f`
- **Why vendored**: reports are delivered to a private-OSS origin served under
  `Content-Security-Policy: sandbox allow-scripts` (opaque origin). External
  script fetches (CDN) are blocked there, so ECharts must be inlined into every
  report. Vendoring also removes the jsdelivr China-availability risk.
- **Provenance**: this exact build was proven to render under the delivery CSP
  in the Resource Manager egress Gate 0 render probe (inline ECharts + system
  CJK fonts, opaque-origin iframe). It is copied verbatim from that probe.
- **How it is used**: `html_renderer.py` reads this file once and exposes it to
  every template as the `echarts_js` global (marked safe via `Markup`); the base
  template emits it inside a single inline `<script>` tag. No network access is
  needed at render time.

To upgrade: replace this file with another Apache-2.0 ECharts UMD build, keep the
license header, update the version marker asserted in
`tests/test_html_renderer.py`, and re-run the offline suite plus an opaque-origin
render check.