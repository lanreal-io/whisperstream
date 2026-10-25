import { BASE } from "./client";
import { getLogBuffer } from "../logCapture";


/** Export logs as a zip via the Tauri save dialog.
 *  Returns `"saved"` if the user completed the save, `"cancelled"` if they
 *  dismissed the dialog, and throws on actual errors. */
export async function exportLogs(): Promise<"saved" | "cancelled" | "denied"> {
  const buffer = getLogBuffer();
  const res = await fetch(`${BASE}/api/logs/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ frontend_logs: buffer }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  if (res.status === 204) return "denied";
  const { routePrivateFileWorkflowResponse, saveMacosPrivateFile } = await import(
    "../nativeFileWorkflows"
  );
  const routed = await routePrivateFileWorkflowResponse(res);
  if (routed.macosNative) {
    const nativeResult = await saveMacosPrivateFile(
      "export-logs",
      "whisperstream-logs.zip",
      routed.bytes,
    );
    if (nativeResult === "unsupported") {
      throw new Error("native private file capability mismatch");
    }
    return nativeResult;
  }
  const blob = await routed.response.blob();

  const { saveBlob } = await import("../saveBlob");
  return saveBlob(blob, "whisperstream-logs.zip", "zip");
}
