// @vitest-environment node
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { FRONTEND_EDITION_VAR, loadFrontendEdition } from "../../edition";

// Each case builds a throwaway workspace and an "other checkout" in the OS
// temp directory; links are removed before their trees.
const created: { links: string[]; dirs: string[] } = { links: [], dirs: [] };

afterEach(() => {
  for (const link of created.links.splice(0)) unlinkSync(link);
  for (const dir of created.dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function tree(): { workspace: string; outside: string } {
  const base = mkdtempSync(join(tmpdir(), "edition-test-"));
  created.dirs.push(base);
  const workspace = join(base, "workspace");
  const outside = join(base, "other-checkout");
  for (const dir of [workspace, outside]) {
    mkdirSync(join(dir, "edition", "updater"), { recursive: true });
    writeFileSync(join(dir, "edition", "updater", "entry.ts"), "export {};\n");
    writeFileSync(join(dir, "edition", "edition.json"), JSON.stringify({ updater: "updater/entry.ts" }));
  }
  return { workspace, outside };
}

function link(target: string, path: string): string {
  // A junction needs no administrator rights on Windows; elsewhere "dir".
  symlinkSync(target, path, process.platform === "win32" ? "junction" : "dir");
  created.links.push(path);
  return path;
}

const load = (manifest: string, workspace: string) =>
  loadFrontendEdition({ [FRONTEND_EDITION_VAR]: manifest }, workspace);

describe("frontend edition containment", () => {
  it("loads a provider that really lives in the workspace", () => {
    const { workspace } = tree();
    const edition = load(join(workspace, "edition", "edition.json"), workspace);
    expect(edition?.updater).toBe(realpathSync.native(join(workspace, "edition", "updater", "entry.ts")));
  });

  it("refuses a manifest reached through a link to another checkout", () => {
    const { workspace, outside } = tree();
    const linked = link(join(outside, "edition"), join(workspace, "linked-edition"));
    expect(() => load(join(linked, "edition.json"), workspace)).toThrow(/outside the workspace/);
  });

  it("refuses a provider path reached through a link to another checkout", () => {
    const { workspace, outside } = tree();
    link(join(outside, "edition", "updater"), join(workspace, "edition", "linked-updater"));
    writeFileSync(
      join(workspace, "edition", "edition.json"),
      JSON.stringify({ updater: "linked-updater/entry.ts" }),
    );
    expect(() => load(join(workspace, "edition", "edition.json"), workspace)).toThrow(/outside the workspace/);
  });

  it("refuses a manifest outside the workspace", () => {
    const { workspace, outside } = tree();
    expect(() => load(join(outside, "edition", "edition.json"), workspace)).toThrow(/outside the workspace/);
  });

  it("is unaffected by a linked node_modules", () => {
    const { workspace, outside } = tree();
    mkdirSync(join(outside, "node_modules"));
    link(join(outside, "node_modules"), join(workspace, "node_modules"));
    expect(load(join(workspace, "edition", "edition.json"), workspace)).not.toBeNull();
  });
});
