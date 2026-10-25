// ---------------------------------------------------------------------------
// useAudioImport — Tauri-native path delivery for the Transcripts import view.
//
// Windows delivers native paths to POST /api/import. Mac keeps paths inside a
// native/storage registry and sends one-shot opaque tokens instead. Sources are
// Tauri-shell-only (graceful no-op in the Vite dev browser):
//   1. chooseFiles() — the native file picker (@tauri-apps/plugin-dialog open()).
//   2. native drag-drop — getCurrentWindow().onDragDropEvent (real paths in
//      WebView2; HTML5 DnD does NOT give real paths, per research).
//
// Why PATHS, not file bytes: the backend import endpoint loops _load_audio_file
// over real paths (avoids large multipart transfers and the /api/transcribe
// .wav-suffix bug). The picker/drag-drop only function inside the packaged or
// `cargo tauri dev` shell; every Tauri call is guarded so dev-browser screenshots
// don't trip the error boundary.
//
// This hook does NOT touch transcript text, license keys, or emails — it only
// passes file PATHS, so the DO-NOT-LOG banner doesn't apply here.
// ---------------------------------------------------------------------------

import { useCallback, useEffect, useState } from "react";
import { importAudio, importAudioSelections } from "./api/transcripts";
import {
  chooseMacosImportAudio,
  releaseMacosNativeSelections,
} from "./nativeFileWorkflows";
import { errorType } from "./logCapture";

/** Locked supported formats — the dialog filter extensions AND the drag-drop
 *  path filter. Mirrors copy-review.md's supported-formats list (WAV always
 *  works; MP3/M4A/FLAC/OGG depend on the user's setup). Lowercase, no dot. */
export const SUPPORTED_AUDIO_EXTENSIONS = [
  "wav",
  "mp3",
  "m4a",
  "flac",
  "ogg",
] as const;

function hasSupportedExtension(path: string): boolean {
  const dot = path.lastIndexOf(".");
  if (dot < 0) return false;
  const ext = path.slice(dot + 1).toLowerCase();
  return (SUPPORTED_AUDIO_EXTENSIONS as readonly string[]).includes(ext);
}

export interface UseAudioImport {
  /** True while a native file drag is hovering the window (enter/over). The
   *  import view brightens the drop zone from this (Task 6.2 consumes it). */
  dragActive: boolean;
  pickerError: boolean;
  importUnavailable: boolean;
  /** Open the native file picker and deliver the chosen paths to /api/import.
   *  Graceful no-op (and returns) outside the Tauri shell. */
  chooseFiles: () => Promise<void>;
}

/**
 * Wire the native picker + drag-drop path delivery for the import view.
 *
 * @param active  Whether the import view is currently shown. The drag-drop
 *   listener is registered ONLY while active and torn down when the view leaves
 *   (or the component unmounts), so drops elsewhere in the tab don't import.
 */
export function useAudioImport(active: boolean, privateMacFiles = false): UseAudioImport {
  const [dragActive, setDragActive] = useState(false);
  const [pickerError, setPickerError] = useState(false);
  const [importUnavailable, setImportUnavailable] = useState(false);

  const deliver = useCallback((paths: string[]) => {
    const supported = paths.filter(hasSupportedExtension);
    if (supported.length === 0) return;
    importAudio(supported).catch((err) => {
      setImportUnavailable(true);
      console.error(`[Transcripts] import start failed error_type=${errorType(err)}`);
    });
  }, []);

  const chooseFiles = useCallback(async () => {
    if (privateMacFiles) {
      setPickerError(false);
      setImportUnavailable(false);
      const result = await chooseMacosImportAudio();
      if (!result || result.status === "cancelled") return;
      if (result.status === "failed") {
        setPickerError(true);
        console.error("[Transcripts] native picker failed error_type=NativePickerError");
        return;
      }
      const tokens = result.selections.map((selection) => selection.token);
      try {
        await importAudioSelections(tokens);
      } catch (err) {
        await releaseMacosNativeSelections(tokens).catch(() => undefined);
        setImportUnavailable(true);
        console.error(`[Transcripts] import start failed error_type=${errorType(err)}`);
      }
      return;
    }
    let paths: string[] | null = null;
    try {
      // Dynamic import so the dev browser (no Tauri runtime) doesn't crash on
      // module load; the call throws there and we fall through to the no-op.
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selection = await open({
        multiple: true,
        filters: [
          { name: "Audio", extensions: [...SUPPORTED_AUDIO_EXTENSIONS] },
        ],
      });
      if (selection == null) return; // user cancelled
      paths = Array.isArray(selection) ? selection : [selection];
    } catch {
      // Not running inside Tauri (Vite dev browser) — picker is unavailable.
      // Graceful no-op; the queue/WS path stays testable via the curl import.
      return;
    }
    if (paths && paths.length > 0) deliver(paths);
  }, [deliver, privateMacFiles]);

  // Native drag-drop listener, registered only while the import view is active.
  useEffect(() => {
    if (!active || privateMacFiles) {
      setDragActive(false);
      return;
    }

    let unlisten: (() => void) | null = null;
    let cancelled = false;

    (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const win = getCurrentWindow();
        // onDragDropEvent yields native events with REAL file paths (enter /
        // over / drop / leave). HTML5 DnD does not in WebView2 (per research).
        const stop = await win.onDragDropEvent((event) => {
          const payload = event.payload;
          if (payload.type === "enter" || payload.type === "over") {
            setDragActive(true);
          } else if (payload.type === "drop") {
            setDragActive(false);
            deliver(payload.paths);
          } else {
            // "leave" / cancel
            setDragActive(false);
          }
        });
        if (cancelled) {
          stop();
        } else {
          unlisten = stop;
        }
      } catch {
        // Not running inside Tauri (Vite dev browser) — drag-drop is a no-op.
        // The drop zone simply never brightens; the picker/curl path still works.
      }
    })();

    return () => {
      cancelled = true;
      if (unlisten) unlisten();
      setDragActive(false);
    };
  }, [active, deliver, privateMacFiles]);

  return { dragActive, pickerError, importUnavailable, chooseFiles };
}
