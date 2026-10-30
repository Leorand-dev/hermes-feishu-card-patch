# Changelog

## v1.0.1

CardKit 2.0 presentation refresh:

- header palette: blue while running, grey when complete, red on error;
- status capsule uses a neutral grey text tag instead of a colored one;
- streaming status is a small grey `notation` line (no blockquote / no heavy styling);
- the initial streaming card contains only the status line; the body element is appended
  when the first answer text arrives, removing the large empty area;
- final card keeps the native `table`, collapsible `运行详情`, completion summary and grey
  footer, with no duplicated status/subtitle;
- unit tests 29/29, integration tests 24/24.

## v1.0.0

Initial pure-code public release:

- build-pinned Feishu streaming-card patch + installer/uninstaller;
- one-card-per-reply layout: status, tools, details, final summary, footer;
- Markdown table → `column_set`/list, long-answer continuation cards, transient
  diagnostics in-card, final fallback;
- clarify single/multi-select cards, slash-confirm buttons;
- optional CardKit 2.0 transport (off by default);
- card metrics + `derive_build.py` for new upstream builds;
- MIT License.
