import { useEffect, useRef, useState } from "react";
import { HoverTooltip } from "./HoverTooltip";

export function DocumentationExportButton() {
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error" | "open-error">("idle");
  const [folder, setFolder] = useState<string | null>(null);
  const buttonRef = useRef<HTMLDivElement>(null);
  const restoreFocus = useRef(false);
  useEffect(() => {
    if (restoreFocus.current && state !== "saving") {
      restoreFocus.current = false;
      buttonRef.current?.querySelector("button")?.focus();
    }
  }, [state]);
  const save = async () => {
    if (state === "saving") return;
    restoreFocus.current = true;
    setState("saving");
    try {
      const { saveDocumentation } = await import("../lib/offlineDocumentation");
      const destination = await saveDocumentation();
      setFolder(destination);
      setState(destination ? "saved" : "idle");
    } catch {
      setState("error");
    }
  };
  const reveal = async () => {
    if (!folder) return;
    try {
      const { revealDocumentation } = await import("../lib/offlineDocumentation");
      await revealDocumentation(folder);
      setState("saved");
    } catch {
      setState("open-error");
    }
  };
  const label = state === "saving" ? "Saving documentation..."
    : state === "error" ? "Couldn't save. Try again" : "Save documentation";
  return <div ref={buttonRef} className="relative z-10 inline-flex min-h-6 items-center">
    {state === "saved" || state === "open-error" ? <div className="flex flex-wrap items-center justify-center gap-x-2 text-steel">
      <span role={state === "open-error" ? "alert" : "status"}>{state === "open-error" ? "Couldn't open folder" : "Documentation saved"}</span>
      <button type="button" onClick={reveal} className="underline cursor-pointer">Open folder</button>
    </div> : <HoverTooltip
      content={state === "error" ? "Couldn't save documentation. Choose another folder and try again." : "Save the complete guide as a PDF and searchable Markdown files."}
      bubbleId="documentation-save-help" className="inline-flex" side="top-right"
      focusVisibleClassName="group-focus-within:opacity-100 group-focus-within:visible"
    >
      <button type="button" onClick={save} disabled={state === "saving"}
        aria-label={label} aria-describedby="documentation-save-help" className="text-steel/70 hover:text-steel hover:underline cursor-pointer disabled:opacity-70 disabled:cursor-not-allowed">
        <span role={state === "error" ? "alert" : "status"}>
          {label}
        </span>
      </button>
    </HoverTooltip>}
  </div>;
}
