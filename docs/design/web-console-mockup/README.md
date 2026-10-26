# Web console v1 mockup

This is a copy of David's Claude Design mockup for the web console. The original canvas is private, so later PRs should cite these files instead of the canvas. The design itself is [`docs/web_console_v1_design.md`](../../web_console_v1_design.md) (PR #143). Where the two disagree, the design doc wins.

## Artboards

| File | Screen | Scope |
|---|---|---|
| `Dashboard.dc.html` | Dashboard: watcher, jobs, tool catalog, decisions waiting | v1 |
| `Main.dc.html` | Tool runner, shown for Library Maintenance | v1 |
| `PlanReview.dc.html` | Plan review for the canonical naming plan | v1 |
| `Settings.dc.html` | Settings: effective configuration and its sources | v1 |
| `SeriesReview.dc.html` | Series Review | v1.1 |

`canvas.json` is the canvas index: the artboard sizes, their order, and the notes.

## Reading these files

- Each `.dc.html` file is a Claude Design component page. The `./support.js` it loads is the Design editor's runtime and isn't included here, so the files don't render on their own. Read them as markup: the layout, the copy, and the sample state in each `renderVals()`.
- All data is sample data. Commit hashes, counts, times and the registry SHA-256 are placeholders, and so are the titles in square brackets.

## Provenance

Copied on 2026-10-26 from canvas version `1793053681-69b6`, byte for byte.

| File | SHA-256 |
|---|---|
| `canvas.json` | `a5f990631d4faba940087679065d0abdacd8921e93d85d3aef5e627072799509` |
| `Dashboard.dc.html` | `420c02be8cbc1f7ecb9a7e4cfe795b0b9dbaaf6893bf8684f4f624af5036b533` |
| `Main.dc.html` | `1c8a55fc16e62a2ae949dc80ee4f7ee10d59cbfa6dec8a7e85602cd541d3e8ed` |
| `PlanReview.dc.html` | `31e8796c677a42412548149d3061da7166ed2975079cba14dd5a482265264dea` |
| `SeriesReview.dc.html` | `13f7a8ac11d05fb0ca8bc78f7d33b63575b4659e69ad0be0fcff57f74e420bf4` |
| `Settings.dc.html` | `da6ee41adb9432ca734b7bd364e159b0fabc6810200fc8160737ffc7e10af454` |
