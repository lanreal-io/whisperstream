import { isTauri } from "@tauri-apps/api/core";
import { saveMacosPrivateFile } from "./nativeFileWorkflows";

const termsFilename = (version: string): string =>
  `Whisperstream-EULA-${version}.txt`;

export async function saveExactEulaTerms(
  terms: string,
  version: string,
): Promise<"saved" | "cancelled"> {
  const filename = termsFilename(version);
  const bytes = new TextEncoder().encode(terms);
  const nativeResult = await saveMacosPrivateFile("retain-eula", filename, bytes);
  if (nativeResult !== "unsupported") return nativeResult;
  if (isTauri()) {
    const { save } = await import("@tauri-apps/plugin-dialog");
    const { writeFile } = await import("@tauri-apps/plugin-fs");
    const path = await save({
      defaultPath: filename,
      filters: [{ name: "Text", extensions: ["txt"] }],
    });
    if (!path) return "cancelled";
    await writeFile(path, bytes);
    return "saved";
  }
  const blob = new Blob([bytes], { type: "text/plain" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
  return "saved";
}

export const eulaTermsFilenameForTest = termsFilename;
