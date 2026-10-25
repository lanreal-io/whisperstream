# Bundled documentation

Edit the source-edition articles in [articles](articles). The registry in
[articles.ts](toolchain/src/articles.ts) supplies ordering, headings and links;
the exporter adds the model-recovery chapter. There is one article source per
chapter and no marketing-site fallback.

The standalone [toolchain](toolchain/package.json) renders the PDF and searchable
Markdown into `frontend/src/assets/documentation`. Its renderer, guide styles,
print styles, embedded fonts and locked dependencies are included here. No website
server, deployment configuration or network-loaded assets are needed to render.

From the repository root:

```powershell
npm --prefix docs/offline/toolchain ci
npm --prefix docs/offline/toolchain run docs:offline
npm --prefix docs/offline/toolchain run docs:check
node scripts/check-offline-docs.mjs
```

PDF generation uses installed Microsoft Edge by default. Set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` for another installed Chromium executable.
The check commands validate inputs, local chapter links, Markdown output and
the complete output inventory against the generated manifest; they do not need
to launch a browser. Frontend prebuild runs the dependency-free inventory guard.

Commit changed inputs and generated outputs together. Inspect the PDF after
content or style changes, and preserve [font licensing](fonts/OFL.txt). Changes
to user-visible help must match the selected app release and its legal terms.

The deployment chapter includes the optional Windows `DisableHistory` policy for
new installations. It documents `/DISABLEHISTORY=1`, direct 64-bit HKLM deployment,
restart and validation steps, and independent removal without deleting old data.
