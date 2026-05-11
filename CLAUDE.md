# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

EasyEDA Pro extension plugin (.eext) that bridges KiPIDA PDN IR Drop analysis into the EasyEDA PCB editor. The TypeScript extension extracts PCB data (tracks, vias, pads, copper pours), converts it to KiPIDA's format, sends it to a local Python FastAPI service for simulation, and displays results as heatmaps in an iframe panel.

## Build Commands

```bash
npm run compile      # esbuild bundle only → dist/index.js
npm run build        # compile + package into build/dist/kipida-bridge_v1.0.0.eext
npm run fix          # prettier + eslint autofix
```

There are no tests. Verification is done by loading the .eext in EasyEDA Pro and running the analysis workflow.

## Architecture

The extension runs as an IIFE in EasyEDA Pro's browser runtime. The global `eda` object (typed by `@jlceda/pro-api-types`) provides all EDA APIs — there is no DOM access outside iframes.

**Data flow:**
1. `extract.ts` — Reads PCB primitives via `eda.pcb_*` APIs, outputs `EasyEDA_PcbData`
2. `convert.ts` — Transforms to `Kipida_PcbData` (nodes, resistances, connections graph)
3. `index.ts` — Opens config iframe (`ui/config.html`) for user to assign sources/loads, then calls the API
4. `api.ts` — HTTP client using `eda.sys_ClientUrl.request()` (not fetch) to talk to localhost:5000
5. `display.ts` — Opens results iframe (`ui/results.html`) with heatmap images

**Communication with iframes** uses `eda.sys_MessageBus` pub/sub (not postMessage).

## Key Constraints

- `eda.sys_ClientUrl.request()` must be used for HTTP — standard `fetch` is unavailable in the extension sandbox
- esbuild config: `platform: 'browser'`, `format: 'iife'`, `globalName: 'edaEsbuildExportName'` — do not change these
- Exported functions in `index.ts` are registered as menu handlers via `extension.json` → `headerMenus[].registerFn`
- The `.edaignore` file controls what goes into the .eext package (similar to .gitignore but inverted — listed paths are excluded)
- Coordinates from EasyEDA APIs are in mils; PourFill coordinates are in 1/10 mil and need ×10 conversion

## Python Service (kipida-service/)

Separate FastAPI app that wraps KiPIDA's `mesh.py` and `solver.py`. Started via `start.bat` or manually with uvicorn on port 5000. The extension only calls `/analyze` (POST) and `/test` (GET health check).

KiPIDA core algorithms are **not bundled** in this repo. Users must download [kbralten/KiPIDA](https://github.com/kbralten/KiPIDA) separately and set the `KIPIDA_PATH` environment variable to point to it. `start.bat` defaults to `kipida-service/KiPIDA/` if unset.
