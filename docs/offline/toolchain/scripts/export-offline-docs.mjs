// Reproducible bundled guide, using only this documentation toolchain.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { tsImport } from "tsx/esm/api";
import { chromium } from "playwright-core";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../..");
const output = path.join(root, "frontend/src/assets/documentation");
const read = (name) => fs.readFileSync(path.join(root, name), "utf8").replace(/\r\n?/g, "\n");
const digest = (bytes) => crypto.createHash("sha256").update(bytes).digest("hex");
const escape = (s) => s.replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const { DOC_ARTICLES, extractDocHeadings } = await tsImport("../src/articles.ts", import.meta.url);
// The standalone JSX renderer uses the imported React runtime.
globalThis.React = React;
const { DocsMarkdown } = await tsImport("../src/DocsMarkdown.tsx", import.meta.url);
const articles = DOC_ARTICLES.filter(a => a.status === "published").map(a => ({...a}));
articles.push({
  id: "model-recovery", path: "/docs/reference/model-recovery", source: "model-recovery.md",
  title: "Install and recover models offline", group: "reference-support",
  description: "Restore the exact speech and cleanup models, verify their files, and set up local enhancement independently.",
});
for (const article of articles) {
  article.input = `docs/offline/articles/${article.source}`;
  article.markdown = read(article.input);
  article.headings = extractDocHeadings(article.markdown);
  if (article.id === "app-basics") {
    article.title = "App basics and updates";
    article.description = "Use the tray app, save offline documentation, and preserve your data when uninstalling.";
  }
  if (article.id === "managed-lockdown-deployment") {
    article.description = "Understand Managed Lockdown capabilities, user controls, and security boundaries.";
  }
}
const routeMap = new Map(articles.map(a => [a.path, a]));
const assets = new Map();
function rewrite(markdown, article, format) {
  return markdown.replace(/(!?\[[^\]]*\]\()([^\s)]+)(\))/g, (whole, lead, href, tail) => {
    if (href.startsWith("/docs") || href.startsWith("#")) {
      const [route, fragment] = href.split("#");
      const target = route ? routeMap.get(route.replace(/\/$/, "")) : article;
      if (!target) throw new Error(`Unknown local documentation link: ${href}`);
      if (fragment && !target.headings.some(h => h.id === fragment)) throw new Error(`Unknown heading: ${href}`);
      const destination = format === "pdf"
        ? `#${target.id}${fragment ? `--${fragment}` : ""}`
        : `${target.source}${fragment ? `#${fragment}` : ""}`;
      return lead + destination + tail;
    }
    if (lead.startsWith("!")) {
      if (!href.startsWith("/") || href.includes("..")) throw new Error(`Image must be a local documentation asset: ${href}`);
      const assetPath = `docs/offline/toolchain/public${href}`;
      const bytes = fs.readFileSync(path.join(root, assetPath));
      const ext = path.extname(href).toLowerCase();
      const mime = {".png":"image/png", ".jpg":"image/jpeg", ".webp":"image/webp", ".svg":"image/svg+xml"}[ext];
      if (!mime) throw new Error(`Unsupported image: ${href}`);
      const filename = `assets/${digest(bytes).slice(0, 16)}${ext}`;
      assets.set(filename, {bytes, input:assetPath});
      return lead + (format === "pdf" ? `data:${mime};base64,${bytes.toString("base64")}` : filename) + tail;
    }
    if (href.startsWith("/")) throw new Error(`Unresolved website dependency: ${href}`);
    return whole;
  });
}
const markdownFiles = new Map(articles.map(a => [a.source,
  `# ${a.title}\n\nWhisperstream 2.0.0.\n\n${rewrite(a.markdown, a, "markdown").trim()}\n`]));
markdownFiles.set("README.md", `# Whisperstream documentation\n\nComplete user guide for Whisperstream 2.0.0. Open the PDF to read or print the guide. These Markdown files contain the same articles for local search and AI tools. Optional external references need internet access; the articles and navigation work offline. Model weights are not included.\n\n${articles.map(a => `- [${a.title}](${a.source})`).join("\n")}\n`);
const docCss = read("docs/offline/toolchain/src/guide.css");
const tokens = read("docs/offline/toolchain/src/tokens.css");
const fonts = [
  ["Inter", "frontend/public/fonts/Inter-Variable.woff2", "normal"],
  ["Outfit", "docs/offline/fonts/Outfit-Variable.ttf", "normal"],
];
const fontCss = fonts.map(([family, input, style]) => `@font-face { font-family: '${family}'; font-style:${style}; font-weight:100 900; src:url(data:font/${input.endsWith("ttf") ? "ttf" : "woff2"};base64,${fs.readFileSync(path.join(root, input)).toString("base64")}); }`).join("\n");
const printCss = read("docs/offline/print.css");
const chapters = articles.map((a, i) => {
  const headings = a.headings.map(h => ({...h, id:`${a.id}--${h.id}`}));
  const prose = renderToStaticMarkup(React.createElement(DocsMarkdown, {source:rewrite(a.markdown,a,"pdf"),headings}));
  return `<article class="chapter" id="${a.id}"><header class="docs-article-header"><p class="eyebrow">Whisperstream / ${String(i+1).padStart(2,"0")}</p><h1>${escape(a.title)}</h1><p class="docs-article-description">${escape(a.description)}</p><dl class="docs-article-metadata"><div><dt>Edition</dt><dd>2.0.0</dd></div></dl></header>${prose}</article>`;
}).join("\n");
const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Whisperstream documentation</title><style>${fontCss}\n:root {${tokens}\n--font-outfit:Outfit;--font-inter:Inter;}\n${docCss}\n${printCss}</style></head><body><section class="cover"><p class="wordmark">whisperstream</p><p class="eyebrow">Whisperstream / 2.0.0</p><h1>Your complete<br>offline guide.</h1><p>Set up dictation, make it your own,<br>and keep your models available.</p><div class="cover-note">Everything in this guide is saved with the app.<br>No website connection is needed to read it.</div></section><nav class="contents"><p class="eyebrow">Whisperstream / Documentation</p><h1>Contents</h1>${articles.map((a,i) => `<a href="#${a.id}"><span>${String(i+1).padStart(2,"0")}</span>${escape(a.title)}</a>`).join("")}</nav>${chapters}</body></html>`;

const inputNames = [...new Set([
  ...articles.map(a => a.input), ...fonts.map(f => f[1]),
  "docs/offline/toolchain/src/articles.ts", "docs/offline/toolchain/src/DocsMarkdown.tsx",
  "docs/offline/toolchain/src/guide.css", "docs/offline/toolchain/src/tokens.css", "docs/offline/print.css", "docs/offline/toolchain/scripts/export-offline-docs.mjs",
  "docs/offline/fonts/OFL.txt", ...[...assets.values()].map(a=>a.input),
])].sort();
// Receipt hashes carry an algorithm label: a bare hex value after a path such
// as src/tokens.css reads as a credential assignment to secret scanners.
const inputs = Object.fromEntries(inputNames.map(name => [name, "sha256:" + digest(
  /\.(ttf|woff2|png|jpg|webp)$/.test(name) ? fs.readFileSync(path.join(root,name)) : read(name)
)]));
const receiptPath = path.join(output,"manifest.json");
if (process.argv.includes("--check")) {
  const receipt = JSON.parse(fs.readFileSync(receiptPath,"utf8"));
  if (JSON.stringify(receipt.inputs) !== JSON.stringify(inputs)) throw new Error("Offline documentation is stale. Run npm --prefix docs/offline/toolchain run docs:offline");
  for (const [name, hash] of Object.entries(receipt.files)) {
    const bytes = fs.readFileSync(path.join(output,name));
    const actual = name.endsWith(".md") ? digest(bytes.toString("utf8").replace(/\r\n?/g,"\n")) : digest(bytes);
    if ("sha256:" + actual !== hash) throw new Error(`Modified or missing output: ${name}`);
  }
  for (const [name, body] of markdownFiles) {
    if (read(`frontend/src/assets/documentation/markdown/${name}`) !== body) throw new Error(`Markdown drift: ${name}`);
  }
  console.log(`Offline documentation current: ${articles.length} articles, ${markdownFiles.size} Markdown files; PDF checksum verified.`);
  process.exit(0);
}
fs.mkdirSync(path.join(output,"markdown"),{recursive:true});
// Keep generated text available for review even when Chromium is unavailable.
for (const [name,body] of markdownFiles) fs.writeFileSync(path.join(output,"markdown",name),body);
for (const [name,{bytes}] of assets) {
  fs.mkdirSync(path.dirname(path.join(output,"markdown",name)),{recursive:true});
  fs.writeFileSync(path.join(output,"markdown",name),bytes);
}
const preview = path.join(root,".tmp/offline-docs.html");
fs.mkdirSync(path.dirname(preview),{recursive:true});
fs.writeFileSync(preview,html);
const browser = await chromium.launch(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
  ? {executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH}
  : {channel:process.env.OFFLINE_DOCS_BROWSER || "msedge"});
try {
  const page = await browser.newPage();
  await page.route("**/*",route => route.abort());
  await page.setContent(html,{waitUntil:"load"});
  await page.evaluate(async () => {
    await Promise.all([document.fonts.load('16px Inter'), document.fonts.load('16px Outfit')]);
    await document.fonts.ready;
  });
  const missingFonts = await page.evaluate(() => [...document.fonts].some(f => f.status !== "loaded"));
  if (missingFonts) throw new Error("An embedded documentation font failed to load");
  const broken = await page.locator('a[href^="#"]').evaluateAll(links => links.filter(a => !document.getElementById(a.getAttribute("href").slice(1))).map(a=>a.getAttribute("href")));
  if (broken.length) throw new Error(`Broken PDF links: ${broken.join(", ")}`);
  await page.pdf({path:path.join(output,"Whisperstream-Documentation.pdf"),format:"A4",printBackground:true,
    preferCSSPageSize:true, tagged:true, outline:true, displayHeaderFooter:true,
    headerTemplate:'<div></div>',footerTemplate:'<div style="width:100%;margin:0 18mm;font-family:Arial;font-size:8px;color:#54677A;display:flex;justify-content:space-between"><span>whisperstream / offline documentation</span><span class="pageNumber"></span></div>',
  });
} finally { await browser.close(); }
const files = {};
for (const [name,body] of markdownFiles) files[`markdown/${name}`] = "sha256:" + digest(body);
for (const [name,{bytes}] of assets) files[`markdown/${name}`] = "sha256:" + digest(bytes);
files["Whisperstream-Documentation.pdf"] = "sha256:" + digest(fs.readFileSync(path.join(output,"Whisperstream-Documentation.pdf")));
fs.writeFileSync(receiptPath, JSON.stringify({edition:"2.0.0",articles:articles.map(({id,title,source})=>({id,title,source})),inputs,files},null,2)+"\n");
console.log(`Saved ${articles.length} chapters, searchable PDF and Markdown to ${output}`);
