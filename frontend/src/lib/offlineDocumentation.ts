import { open } from "@tauri-apps/plugin-dialog";
import { exists, mkdir, writeFile } from "@tauri-apps/plugin-fs";
import { join } from "@tauri-apps/api/path";
import { revealItemInDir } from "@tauri-apps/plugin-opener";
import pdfUrl from "../assets/documentation/Whisperstream-Documentation.pdf?url";
import manifest from "../assets/documentation/manifest.json";

const markdownUrls = import.meta.glob<string>("../assets/documentation/markdown/**/*", {
  eager: true, query: "?url", import: "default",
});

/** The native picker grants recursive access only to the user's chosen folder. */
export async function saveDocumentation(): Promise<string | null> {
  const selected = await open({
    title: "Choose where to save documentation",
    directory: true, multiple: false, recursive: true,
  });
  if (!selected || Array.isArray(selected)) return null;

  // Read and validate the entire bundled export before creating any files.
  const entries = await Promise.all(Object.entries(manifest.files).map(async ([name, hash]) => {
    if (!/^(Whisperstream-Documentation\.pdf|markdown\/[a-zA-Z0-9_./-]+)$/.test(name)
      || name.split("/").some(part => part === ".." || part === ".")) {
      throw new Error("Invalid documentation filename");
    }
    const url = name.endsWith(".pdf") ? pdfUrl : markdownUrls[`../assets/documentation/${name}`];
    if (!url) throw new Error("Bundled documentation missing");
    const response = await fetch(url);
    if (!response.ok) throw new Error("Bundled documentation unavailable");
    let bytes = new Uint8Array(await response.arrayBuffer());
    // Text hashes are newline-normalized so Windows checkouts remain portable.
    if (name.endsWith(".md")) bytes = new TextEncoder().encode(new TextDecoder().decode(bytes).replace(/\r\n?/g, "\n"));
    const actual = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
      .map(value => value.toString(16).padStart(2, "0")).join("");
    // The receipt labels each hash with its algorithm.
    if (`sha256:${actual}` !== hash) throw new Error("Bundled documentation checksum mismatch");
    return { name, bytes };
  }));

  let folder = await join(selected, "Whisperstream documentation");
  for (let suffix = 2; await exists(folder); suffix++) {
    if (suffix > 1000) throw new Error("No unused documentation folder");
    folder = await join(selected, `Whisperstream documentation (${suffix})`);
  }
  // Exclusive creation: if another process wins the race, fail without overwriting.
  await mkdir(folder);
  const created = new Set<string>();
  for (const { name, bytes } of entries) {
    const parts = name.split("/");
    for (let length = 1; length < parts.length; length++) {
      const subfolder = await join(folder, ...parts.slice(0, length));
      if (!created.has(subfolder)) {
        await mkdir(subfolder);
        created.add(subfolder);
      }
    }
    await writeFile(await join(folder, ...parts), bytes);
  }
  return folder;
}

export async function revealDocumentation(folder: string): Promise<void> {
  await revealItemInDir(await join(folder, "Whisperstream-Documentation.pdf"));
}
