# Roadmap — gaps agents can close

Canonical “what to build next” for contributors. For agent-facing detail and
the list of **already implemented** MCP tools, see [AGENTS.md](AGENTS.md) in this folder.

## High priority

| Gap | Status | Notes |
|-----|--------|--------|
| Import → ColumnDefs binding | **Partial** | `configure_node(source_file=...)` copies header, code page, delimiter and types; `save_package` propagates types downstream. Non-import upstreams still need explicit columns |

## Medium priority

| Gap | Notes |
|-----|--------|
| Special visualizer configure | `coarse_classes`, `data_tree` still virgin. Cluster profiles, statistics flags, ROC, and regression reports are configurable |
| Special visualizers configure | `coarse_classes` and `data_tree` still virgin |
| Deeper `configure_node` for virgin-only components | imputation, exports, submodel port publishing, modeling nodes — one engine per PR |
| More / fresher `g6_blobs` | Mirror from real Loginom packages |

## Lower priority

- `remove_node` / `unlink_nodes` / `clear_package`
- CI golden `.lgp` fixtures for `verify_package` without UI
- Node preset export/import

## Done (do not reinvent)

MCP tools: `list_loginom_components`, `list_visualizers`, `create_package`,
`add_node`, `configure_node`, `link_nodes`, `add_visualizer`,
`configure_visualizer` (table / duplicates filters+sort, chart, cube, statistics,
quality, cluster profiles, ROC, regression reports, session or saved `.lgp`), `audit_visualizers`, `rename_node`, `get_package`, `inspect_lgp`,
`inspect_source_schema` (text and `.xlsx`), `save_package`, `verify_package`,
`lint_lgp`, `check_package`, `loginom_log_errors`, `loginom_windows`,
`close_loginom_windows`.

Diagnostics: a package that Loginom refuses to open leaves no useful dialog in
7.4 (the window silently stays on the start page), but the real reason is in
`%APPDATA%/Loginom/Community/Logs/app.log`:
`Ошибка загрузки пакета: не удалось загрузить элемент "Variables"` →
`Элемент конфигурации с именем Variables не найден` (`EBGAggregateException`).
`check_package(path)` opens the file, diffs the log by byte offset and returns
those entries; `loginom_log_errors()` reads the tail on demand. Loginom is CEF:
`WM_CLOSE` is ignored, so closing uses Alt+F4 and only falls back to
`Stop-Process` for a process this server started itself.

Libraries: virgin visualizers + `VISUALIZERS.md` + `vendor/viz_configure.py` +
`tools/inspect_viz.py`.
