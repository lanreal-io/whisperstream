import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "framer-motion";
import { downloadLegalDocument, LEGAL_DOCUMENTS } from "../lib/legalDocuments";
import { getBYOKConsentContext, type BYOKConsentContext } from "../lib/api/consent";
import { enhancementErrorMessage } from "../lib/enhancementConsent";

/* ---------- Shield Icon ---------- */
function ShieldCheckIcon() {
  return (
    <div className="relative shrink-0 w-12 h-12 mx-auto mb-4" style={{ color: "var(--color-frost-deep)" }}>
      {/* Frost aura */}
      <div
        className="absolute inset-0 rounded-full"
        style={{
          background: "radial-gradient(circle, var(--halo-frost) 0%, transparent 70%)",
          transform: "scale(1.6)",
        }}
      />
      <svg
        viewBox="0 0 48 48"
        fill="none"
        className="relative w-full h-full"
      >
        <motion.path
          d="M24 6L10 12v10c0 9 6.3 17.4 14 20 7.7-2.6 14-11 14-20V12L24 6z"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ scale: 0.85, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.1, type: "spring", stiffness: 400, damping: 25 }}
          style={{ transformOrigin: "24px 24px" }}
        />
        <motion.path
          d="M18 24l4.5 4.5 7.5-8"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          initial={{ opacity: 0, scale: 0.6 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.22, type: "spring", stiffness: 600, damping: 26 }}
          style={{ transformOrigin: "24px 24px" }}
        />
      </svg>
    </div>
  );
}

interface PrivacyModalProps {
  provider: string;
  ollamaEndpoint?: string;
  customBaseUrl?: string;
  model?: string;
  onConfirm: (context: BYOKConsentContext) => void | Promise<void>;
  onCancel: () => void;
}

/** The backend owns both the rendered text and evidence identity. No local
 * fallback may enable consent when that context cannot be loaded. */
export function PrivacyModal({ provider, ollamaEndpoint, customBaseUrl, model, onConfirm, onCancel }: PrivacyModalProps) {
  const request = useMemo(() => ({ provider, model,
    ...(provider === "custom" ? { endpoint: customBaseUrl } : {}),
    ...(provider === "ollama" ? { endpoint: ollamaEndpoint } : {}),
  }), [provider, model, customBaseUrl, ollamaEndpoint]);
  const requestKey = JSON.stringify(request);
  const [loaded, setLoaded] = useState<{ key: string; context: BYOKConsentContext } | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [retry, setRetry] = useState(0);
  const [documentError, setDocumentError] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const context = loaded?.key === requestKey ? loaded.context : null;

  useEffect(() => {
    let cancelled = false;
    setLoaded(null);
    setLoadError(null);
    getBYOKConsentContext(request).then((value) => {
      if (cancelled) return;
      if (!Array.isArray(value.lines) || value.lines.join("\n") !== value.dialog_body_text || !value.context_token) {
        throw new Error("Invalid consent context");
      }
      setLoaded({ key: requestKey, context: value });
    }).catch((error) => { if (!cancelled) setLoadError(enhancementErrorMessage(error)); });
    return () => { cancelled = true; };
  }, [request, requestKey, retry]);

  useEffect(() => {
    cancelRef.current?.focus({ preventScroll: true });
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, []);

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !saving) { event.preventDefault(); onCancel(); }
      if (event.key !== "Tab") return;
      const controls = dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href]');
      if (!controls?.length) return;
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [saving, onCancel]);

  const confirm = async () => {
    if (!context || saving) return;
    setSaving(true);
    setSubmitError(null);
    try { await onConfirm(context); }
    catch (error) {
      if ((error as { errorCode?: string }).errorCode === "consent_changed") {
        setRetry((value) => value + 1);
      }
      setSubmitError(enhancementErrorMessage(error, "Could not save permission. Try again. No request was sent."));
    } finally { setSaving(false); }
  };

  const headings = new Set(["What is sent", "Before using Gemini", "Saved keys", "Your choice"]);
  const linkLines = new Set(context?.links.map((link) => `${link.label}: ${link.url}`));
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="absolute inset-0"
        style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)" }}
        onClick={() => { if (!saving) onCancel(); }} />
      <motion.div ref={dialogRef} initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}
        className="relative flex flex-col w-[460px] max-w-full max-h-[92vh] overflow-hidden rounded-[var(--radius-card)] p-7"
        style={{ background: "var(--surface-modal)", border: "1px solid var(--border-emphasis)", boxShadow: "var(--shadow-glass)" }}
        role="dialog" aria-modal="true" aria-labelledby="privacy-modal-heading" aria-busy={saving}>
        <ShieldCheckIcon />
        <h2 id="privacy-modal-heading" className="shrink-0 font-display text-[18px] font-normal text-slate text-center mb-1">{context?.lines[0] ?? "Privacy notice"}</h2>
        <div ref={bodyRef} className="min-h-0 overflow-y-auto pr-1">
        {context ? <>
          <p className="text-[12px] text-steel text-center mb-4">{context.lines[1]}</p>
          <div className="rounded-[var(--radius-input)] p-3 mb-4 text-[12px] text-slate break-all"
            style={{ background: "var(--surface-control)", border: "1px solid var(--border-hairline)" }}>
            <div>{context.lines[2]}</div><div>{context.lines[3]}</div>
          </div>
          {context.lines.slice(4).filter((line) => !linkLines.has(line)).map((line, index) => {
            if (line === "Whisperstream privacy policy: bundled PRIVACY.pdf") return <div key={index} className="mt-4">
              <a href={LEGAL_DOCUMENTS[0].url} download={LEGAL_DOCUMENTS[0].filename}
                onClick={async (event) => { event.preventDefault(); setDocumentError(false); try { await downloadLegalDocument(LEGAL_DOCUMENTS[0]); } catch { setDocumentError(true); } }}
                className="text-[12px] hover:underline" style={{ color: "var(--color-frost-deep)" }}>{line}</a>
            </div>;
            return headings.has(line)
              ? <h3 key={index} className="text-[13px] font-medium text-slate mt-4 mb-1.5">{line}</h3>
              : <p key={index} className={`${index === 0 ? "text-[13px]" : "text-[12px]"} text-steel leading-relaxed mb-2.5`}>{line}</p>;
          })}
          <div className="mt-1 space-y-1">{context.links.map((link) => <a key={link.url} href={link.url} target="_blank" rel="noopener noreferrer"
            className="block break-words text-[12px] hover:underline" style={{ color: "var(--color-frost-deep)" }}>{link.label}: {link.url}</a>)}</div>
          {documentError && <p role="alert" className="text-[12px] text-rose mt-2">Could not download the document. Please try again.</p>}
        </> : loadError ? <div><p role="alert" className="text-[12px] text-rose my-4">{loadError}</p><button onClick={() => setRetry((value) => value + 1)} className="text-[12px] text-slate underline">Try again</button></div>
          : <p role="status" className="text-[12px] text-steel my-4">Loading privacy notice…</p>}
        </div>
        {submitError && <p role="alert" className="shrink-0 text-[12px] text-rose mt-4">{submitError}</p>}
        <div className="shrink-0 flex items-center justify-end gap-2.5 mt-5">
          <button ref={cancelRef} disabled={saving} onClick={onCancel} className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer disabled:opacity-50"
            style={{ background: "var(--surface-control)", border: "1px solid var(--border-emphasis)" }}>Cancel</button>
          <button disabled={!context || saving} onClick={() => void confirm()} className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer disabled:opacity-50 disabled:cursor-default"
            style={{ background: "var(--accent-fill)", boxShadow: "var(--shadow-pearl)" }}>{saving ? "Saving permission…" : "Allow connection"}</button>
        </div>
      </motion.div>
    </div>
  );
}
