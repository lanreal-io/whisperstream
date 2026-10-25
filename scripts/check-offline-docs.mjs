// Dependency-free app-build guard. Regeneration belongs to the offline documentation toolchain.
import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const bundle = "frontend/src/assets/documentation";
const receipt = JSON.parse(fs.readFileSync(path.join(root, bundle, "manifest.json"), "utf8"));
function check(name, expected) {
  let bytes = fs.readFileSync(path.join(root, name));
  if (!/\.(pdf|ttf|woff2|png|jpg|webp)$/.test(name)) {
    bytes = bytes.toString("utf8").replace(/\r\n?/g, "\n");
  }
  if (crypto.createHash("sha256").update(bytes).digest("hex") !== expected) {
    throw new Error(`Offline documentation is stale: ${name}. Run npm --prefix docs/offline/toolchain run docs:offline`);
  }
}
// Receipt hashes are labelled "sha256:<hex>" (see export-offline-docs.mjs).
function unlabel(name, hash) {
  if (!hash.startsWith("sha256:")) throw new Error(`Offline documentation receipt has an unlabelled hash: ${name}`);
  return hash.slice("sha256:".length);
}
for (const [name, hash] of Object.entries(receipt.inputs)) check(name, unlabel(name, hash));
for (const [name, hash] of Object.entries(receipt.files)) check(`${bundle}/${name}`, unlabel(name, hash));
const actual = fs.readdirSync(path.join(root, bundle), { recursive: true, withFileTypes: true })
  .filter(entry => entry.isFile())
  .map(entry => path.relative(path.join(root, bundle), path.join(entry.parentPath, entry.name)).replaceAll("\\", "/"))
  .filter(name => name !== "manifest.json").sort();
if (JSON.stringify(actual) !== JSON.stringify(Object.keys(receipt.files).sort())) {
  throw new Error("Offline documentation bundle has missing or extra files");
}
console.log(`Offline documentation verified: ${receipt.articles.length} articles and PDF.`);
