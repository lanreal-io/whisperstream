/** Save a bundled or user-generated document locally, without uploading it. */
export async function saveBlob(blob: Blob, filename: string, extension: string): Promise<"saved" | "cancelled"> {
  if ("__TAURI_INTERNALS__" in window) {
    const { save } = await import("@tauri-apps/plugin-dialog");
    const { writeFile } = await import("@tauri-apps/plugin-fs");
    const path = await save({
      defaultPath: filename,
      filters: [{ name: extension.toUpperCase(), extensions: [extension] }],
    });
    if (!path) return "cancelled";
    await writeFile(path, new Uint8Array(await blob.arrayBuffer()));
    return "saved";
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
  return "saved";
}
