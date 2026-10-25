import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { PrimaryButton } from "./PrimaryButton";
import { SecondaryButton } from "./SecondaryButton";
import { saveBlob } from "../lib/saveBlob";
import {
  parseVocabularyCsv, reviewVocabulary, VOCABULARY_EXAMPLE,
  type DictionaryImportConfig, type VocabularyFile,
} from "../lib/dictionaryImport";

interface Props {
  config: DictionaryImportConfig;
  onClose: () => void;
  onImport: (file: VocabularyFile) => Promise<number>;
}

const PREVIEW_ROWS = 100;
const buttonClass = "rounded-full px-4 py-2 text-[12px] text-slate";
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
export function DictionaryFileIcon({ size = 14 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 1.5h5l3 3v8H3zM8 1.5v3h3M5 7h4M5 9.5h4" /></svg>;
}

/** Workspace-family modal. Deliberately independent of other dialog shells. */
export function DictionaryImportModal({ config, onClose, onImport }: Props) {
  const titleId = useId(), introId = useId();
  const rootRef = useRef<HTMLDivElement>(null), closeRef = useRef<HTMLButtonElement>(null);
  const fileRef = useRef<HTMLInputElement>(null), dropRef = useRef<HTMLDivElement>(null);
  const epoch = useRef(0), savingRef = useRef(false);
  const mountedRef = useRef(false);
  const [file, setFile] = useState<VocabularyFile | null>(null);
  const [filename, setFilename] = useState("");
  const [error, setError] = useState("");
  const [reading, setReading] = useState(false), [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState(false), [dropUnavailable, setDropUnavailable] = useState(false);
  const [success, setSuccess] = useState<number | null>(null);
  const review = useMemo(() => file ? reviewVocabulary(file, config) : null, [file, config]);
  const readRef = useRef<(paths: string[]) => void>(() => {});
  const onCloseRef = useRef(onClose); onCloseRef.current = onClose;

  // Portal escapes the main card's blur. Inert siblings, contain focus, and
  // return it to the original trigger on every dismissal/unmount path.
  useEffect(() => {
    mountedRef.current = true;
    const opener = document.activeElement as HTMLElement | null;
    const root = rootRef.current!;
    const siblings = [...document.body.children].filter(node => node !== root && node instanceof HTMLElement) as HTMLElement[];
    const prior = siblings.map(node => ({ node, inert: node.inert }));
    siblings.forEach(node => { node.inert = true; });
    closeRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingRef.current) { event.preventDefault(); onCloseRef.current(); }
      if (event.key !== "Tab") return;
      const nodes = [...root.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], summary, input:not([type="file"])')]
        .filter(node => !node.hidden && node.getClientRects().length > 0);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (!first) { event.preventDefault(); return; }
      if (event.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !root.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      mountedRef.current = false;
      epoch.current++;
      document.removeEventListener("keydown", keydown);
      prior.forEach(({ node, inert }) => { node.inert = inert; });
      if (opener?.isConnected) opener.focus();
    };
  }, []);

  const startRead = async (name: string, read: () => Promise<string>) => {
    if (savingRef.current || !mountedRef.current) return;
    const token = ++epoch.current;
    setError(""); setSuccess(null); setFile(null);
    if (!/\.csv$/i.test(name)) { setReading(false); setError("Choose a CSV file. Download the example to see the format."); return; }
    setReading(true);
    let text: string;
    try { text = await read(); }
    catch {
      if (token === epoch.current) { setReading(false); setError("Couldn't read this file. Choose it again or select another CSV."); }
      return;
    }
    if (token !== epoch.current) return;
    setReading(false);
    try { setFile(parseVocabularyCsv(text)); setFilename(name); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Couldn't read this CSV. Check its format or start with the example file."); }
    closeRef.current?.focus();
  };
  const readNative = (paths: string[]) => {
    if (paths.length !== 1) { setError("Choose one CSV file at a time."); return; }
    const path = paths[0];
    void startRead(path.split(/[\\/]/).pop() ?? "", async () => {
      const { readFile } = await import("@tauri-apps/plugin-fs");
      return new TextDecoder("utf-8", { fatal: true }).decode(await readFile(path));
    });
  };
  readRef.current = readNative;
  const readBrowser = (selected: File) => startRead(selected.name, async () =>
    new TextDecoder("utf-8", { fatal: true }).decode(await selected.arrayBuffer()),
  );

  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in window)) return;
    let cancelled = false, unlisten: (() => void) | undefined, dragSequence = 0, lastDropSequence = 0;
    void (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const windowHandle = getCurrentWindow();
        const stop = await windowHandle.onDragDropEvent(async ({ payload }) => {
          const sequence = ++dragSequence;
          if (cancelled || savingRef.current) return;
          if (payload.type === "leave") { setDragging(false); return; }
          if (payload.type === "drop") lastDropSequence = sequence;
          // Physical drop coordinates must use the current monitor's scale.
          // Ignore stale async results after a later event or modal dismissal.
          let scale: number;
          try { scale = await windowHandle.scaleFactor(); }
          catch { if (!cancelled && sequence === dragSequence) { setDragging(false); setDropUnavailable(true); } return; }
          // A following leave clears hover but must not discard a completed
          // drop while its current-monitor scale is still being resolved.
          if (cancelled || savingRef.current || (payload.type === "drop" ? sequence !== lastDropSequence : sequence !== dragSequence)) return;
          const rect = dropRef.current?.getBoundingClientRect();
          const inside = !!rect && payload.position.x / scale >= rect.left && payload.position.x / scale <= rect.right
            && payload.position.y / scale >= rect.top && payload.position.y / scale <= rect.bottom;
          setDragging(inside && payload.type !== "drop");
          if (inside && payload.type === "drop") readRef.current(payload.paths);
        });
        if (cancelled) stop(); else unlisten = stop;
      } catch { if (!cancelled) setDropUnavailable(true); }
    })();
    return () => { cancelled = true; unlisten?.(); };
  }, []);

  const choose = async () => {
    if (!("__TAURI_INTERNALS__" in window)) { fileRef.current?.click(); return; }
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({ multiple: false, filters: [{ name: "CSV", extensions: ["csv"] }] });
      if (selected) readNative(Array.isArray(selected) ? selected : [selected]);
    } catch { setError("Couldn't read this file. Choose it again or select another CSV."); }
  };
  const example = async () => {
    setError("");
    try { await saveBlob(new Blob([VOCABULARY_EXAMPLE], { type: "text/csv;charset=utf-8" }), "vocabulary-example.csv", "csv"); }
    catch { setError("Couldn't save the example CSV. Try downloading it again."); }
  };
  const commit = async () => {
    if (!file || !review?.additions.length || review.invalid.length || savingRef.current) return;
    savingRef.current = true; setSaving(true); setError("");
    try { setSuccess(await onImport(file)); }
    catch { setError("Couldn't import these entries. Try again."); }
    finally { savingRef.current = false; setSaving(false); }
  };
  const dismiss = () => { if (!savingRef.current) onClose(); };

  return createPortal(
    <div ref={rootRef} className="fixed inset-0 z-[100] flex items-center justify-center p-6" style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)" }} onMouseDown={event => { if (event.target === event.currentTarget) dismiss(); }}>
      <section role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={introId} aria-busy={reading || saving} className="relative w-[520px] max-w-full max-h-full flex flex-col rounded-[var(--radius-card)] p-7" style={{ background: "var(--surface-modal)", border: "1px solid var(--border-emphasis)", boxShadow: "var(--shadow-glass)", backdropFilter: "blur(24px)" }}>
        <button ref={closeRef} aria-label="Close import" disabled={saving} onClick={dismiss} className="absolute right-[18px] top-[18px] w-7 h-7 rounded-full text-steel cursor-pointer disabled:opacity-40">×</button>
        <div className="pr-6"><h2 id={titleId} className="text-[18px] font-medium text-slate">{review && success === null ? "Review import" : "Import vocabulary"}</h2><p id={introId} className="text-[12px] text-steel leading-relaxed mt-1">{review && success === null ? "Review the additions before importing. Existing entries stay unchanged." : "Add Boost words and replacement rules from a CSV file."}</p></div>
        <div className="mt-5 min-h-0 overflow-y-auto">
          {error && <div role="alert" className="text-[12px] text-slate leading-relaxed p-3 mb-3 rounded-[var(--radius-input)]" style={{ background: "color-mix(in srgb, var(--color-rose) 8%, transparent)", border: "1px solid color-mix(in srgb, var(--color-rose) 35%, transparent)" }}>{error}</div>}
          {success !== null ? <div role="status" className="py-6 text-center text-slate text-[14px]">Added {plural(success, "entry", "entries")} to Dictionary.</div> : reading ? <p role="status" className="text-[12px] text-steel">Reading file...</p> : review ? <>
            <div className="flex gap-2 items-center text-[12px] text-steel mb-4"><DictionaryFileIcon /><span className="flex-1 truncate">{filename}</span><button disabled={saving} onClick={choose} className="text-[11px] underline cursor-pointer disabled:opacity-40">Choose another file</button></div>
            {review.invalid.length > 0 && <div role="alert" className="p-3 mb-3 rounded-[var(--radius-input)] text-[12px] text-slate" style={{ background: "color-mix(in srgb, var(--color-rose) 8%, transparent)", border: "1px solid color-mix(in srgb, var(--color-rose) 35%, transparent)" }}><p>{plural(review.invalid.length, "row needs", "rows need")} attention</p><p>Fix these rows in your CSV, then choose the file again.</p>{review.invalid.slice(0, PREVIEW_ROWS).map((message, index) => <p key={index}>{message}</p>)}{review.invalid.length > PREVIEW_ROWS && <p className="mt-2">Showing 100 of {review.invalid.length} rows that need attention.</p>}</div>}
            <div className="flex justify-between gap-2 items-center mb-2"><h3 className="text-[13px] text-slate">{review.additions.length ? "Ready to add" : "No new entries to import."}</h3>{review.additions.length > 0 && <span className="text-[12px] text-steel">{plural(review.additions.length, "entry", "entries")} to add</span>}</div>
            {review.additions.length > 0 && <div className="max-h-[188px] overflow-auto rounded-[var(--radius-input)]" style={{ border: "1px solid var(--border-standard)", background: "var(--surface-control)" }}><table className="w-full table-fixed text-[12px] text-slate"><thead className="sticky top-0" style={{ background: "var(--surface-modal)" }}><tr>{["Word", "Replacement", "Action"].map(label => <th key={label} className={`text-left text-[11px] font-medium text-steel px-2 py-1.5 ${label === "Action" ? "w-[92px]" : ""}`}>{label}</th>)}</tr></thead><tbody>{review.additions.slice(0, PREVIEW_ROWS).map(row => <tr key={row.row} style={{ borderTop: "1px solid var(--border-hairline)" }}><td className="p-2 break-words align-top">{row.word}</td><td className="p-2 break-words align-top">{row.replacement}</td><td className="p-2 align-top text-[11px]" style={{ color: row.replacement ? "var(--color-gold-deep)" : "var(--color-mint)" }}>{row.replacement ? "→ Replace" : "+ Boost"}</td></tr>)}</tbody></table></div>}
            {review.additions.length > PREVIEW_ROWS && <p className="mt-2 text-[11px] text-steel">Showing 100 of {review.additions.length} entries. All {review.additions.length} will be added when you import.</p>}
            {review.duplicates.length > 0 && <details className="mt-2.5 text-[11px] text-steel"><summary className="cursor-pointer">{plural(review.duplicates.length, "duplicate", "duplicates")} skipped</summary><ul className="list-disc pl-5 mt-1">{review.duplicates.slice(0, PREVIEW_ROWS).map(row => <li key={row.row}>{row.word}</li>)}</ul>{review.duplicates.length > PREVIEW_ROWS && <p className="mt-1">Showing 100 of {review.duplicates.length} duplicates.</p>}</details>}
            {review.conflicts.length > 0 && <details className="mt-2.5 text-[11px] text-steel"><summary className="cursor-pointer">{plural(review.conflicts.length, "conflicting entry", "conflicting entries")} skipped</summary><p className="mt-1">These words already have different rules. Edit them in Dictionary to change them.</p><ul className="list-disc pl-5 mt-1">{review.conflicts.slice(0, PREVIEW_ROWS).map(row => <li key={row.row}>{row.word}</li>)}</ul>{review.conflicts.length > PREVIEW_ROWS && <p className="mt-1">Showing 100 of {review.conflicts.length} conflicting entries.</p>}</details>}
          </> : <>
            <div ref={dropRef} className="min-h-[160px] p-5 flex flex-col justify-center items-center gap-2.5 rounded-[var(--radius-row)]" style={{ border: `1.5px dashed ${dragging ? "var(--color-frost)" : "color-mix(in srgb, var(--color-steel) 35%, transparent)"}`, background: dragging ? "color-mix(in srgb, var(--color-frost) 12%, transparent)" : "var(--surface-control)" }} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); const files = event.dataTransfer.files; if (files.length !== 1) setError("Choose one CSV file at a time."); else void readBrowser(files[0]); }}>
              <span className="text-steel"><DictionaryFileIcon size={30} /></span><h3 className="text-[15px] text-slate">Drop a CSV file here</h3><SecondaryButton onClick={choose} className={buttonClass}>Choose file</SecondaryButton>
            </div>
            <div className="mt-3.5 text-[12px] text-steel"><button onClick={example} className="inline-flex items-center gap-1.5 mb-3 cursor-pointer hover:underline"><span aria-hidden="true">↓</span>Download example CSV</button><p>The word header is required; replacement is optional.</p></div>
            {dropUnavailable && <p className="text-[11px] text-steel mt-2" role="status">Drag and drop is unavailable. Use Choose file instead.</p>}
          </>}
        </div>
        <footer className="mt-6 flex gap-2 justify-end shrink-0">
          {success !== null ? <PrimaryButton onClick={dismiss} className="px-5 py-2 text-[12px]">Done</PrimaryButton> : <><SecondaryButton onClick={dismiss} disabled={saving} className={buttonClass}>Cancel</SecondaryButton>{review && !reading && <PrimaryButton disabled={!review.additions.length || !!review.invalid.length || saving} loading={saving} onClick={commit} className="px-5 py-2 text-[12px]">{saving ? "Importing..." : `Import ${plural(review.additions.length, "entry", "entries")}`}</PrimaryButton>}</>}
        </footer>
        <input ref={fileRef} type="file" accept=".csv,text/csv" className="hidden" aria-label="Choose a vocabulary CSV file" onChange={event => { const selected = event.target.files?.[0]; event.target.value = ""; if (selected) void readBrowser(selected); }} />
      </section>
    </div>, document.body,
  );
}
