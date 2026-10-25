import { useEffect, useRef, useState, type CSSProperties } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { emit } from "@tauri-apps/api/event";
import { exit } from "@tauri-apps/plugin-process";
import { PopupShell } from "../components/PopupShell";
import { ReadingWell } from "../components/ReadingWell";
import { errorType, flushLogSync } from "../lib/logCapture";
import { acceptEula, getEulaStatus, resolveEulaRequirementState, type EulaEmitPayload } from "../lib/api/consent";
import { saveExactEulaTerms } from "../lib/eulaTerms";
import { applyWindowLifecycle, publishPopupVisibility } from "../lib/windowLifecycle";

// Shared backdrop for the INLINE EULA overlays (amendment + re-acknowledgement)
// rendered over the running main UI. `inset: 0` confines it to the app WINDOW
// (it never touches the rest of the desktop) and it captures clicks so the app
// underneath is inert while the gate is up. The scrim is intentionally a
// whisper (no heavy dim, no blur): the app stays visible and undisturbed, and
// the InlineShell card is opaque on its own so the terms stay legible without
// leaning on the backdrop. Exported so App.tsx and the dev dialog registry
// (UiTest / DialogShowcase) render the exact same overlay -- a backdrop
// regression then shows up in the harness instead of only in production.
export const EULA_INLINE_BACKDROP_STYLE: CSSProperties = {
  position: "fixed",
  inset: 0,
  zIndex: 50,
  background: "rgba(0, 0, 0, 0.14)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

// DEV-ONLY: ?forceAmendment=1 injects a synthetic amendment payload for
// visual verification in Playwright. Guarded by import.meta.env.DEV so it
// never ships in production bundles.
const DEV_FORCE_AMENDMENT =
  import.meta.env.DEV &&
  typeof window !== "undefined" &&
  new URLSearchParams(window.location.search).get("forceAmendment") === "1";

const DEV_AMENDMENT_PAYLOAD: EulaEmitPayload = {
  mode: "substantive-update",
  eulaText: "WHISPERSTREAM END USER LICENSE AGREEMENT\n\nVersion 2.0",
  eulaVersion: "2.0",
  eulaSha256: "abc123",
  softwareVersion: "1.0.0",
  pinnedVersion: "1.0",
  changeBullets: [
    "Added data retention limits for transcription logs (90 days).",
    "Clarified that on-device processing means audio never leaves your machine.",
    "Updated jurisdiction clause to align with Canadian federal law.",
  ],
};

interface EulaPopupProps {
  // When true, the component is rendered inside the main window (workaround
  // for the popup-eula window invisibility bug). Skips Tauri window-hide on
  // accept (parent unmounts via the `eula-accepted` listener instead) and
  // wraps the dialog in an opaque shell instead of PopupShell, which would
  // force document.body transparent.
  inline?: boolean;
  // Pre-supplied payload, used by inline mode so the parent (App.tsx) can
  // pass the already-fetched EulaStatus directly without round-tripping
  // through `eula-data` emit + listen.
  initialPayload?: EulaEmitPayload;
  // Called when the amendment is dismissed (Later or Decline) in inline mode
  // so the parent can unmount this component. Not needed in popup-window mode
  // (the window is hidden directly). Also not called on accept — the parent
  // listens for the `eula-accepted` Tauri event instead.
  onClose?: () => void;
  onAccepted?: () => void | Promise<void>;
}

export function EulaPopup({ inline = false, initialPayload, onAccepted }: EulaPopupProps = {}) {
  const [payload, setPayload] = useState<EulaEmitPayload | null>(
    DEV_FORCE_AMENDMENT ? DEV_AMENDMENT_PAYLOAD : (initialPayload ?? null)
  );

  // Listen for "eula-data" Tauri event; race-protect with a getEulaStatus fallback.
  useEffect(() => {
    if (DEV_FORCE_AMENDMENT) return; // dev override — skip real fetch
    if (initialPayload) return; // inline mode supplies payload directly
    let unlistenFn: (() => void) | null = null;
    (async () => {
      try {
        const w = getCurrentWindow();
        const u = await w.listen<EulaEmitPayload>("eula-data", (e) => setPayload(e.payload));
        unlistenFn = () => u();
      } catch { /* pure-Vite dev */ }
    })();
    // Fallback fetch if emit arrived before listener or if running outside Tauri:
    getEulaStatus().then((s) => {
      setPayload((p) =>
        p ?? {
          mode:
            resolveEulaRequirementState(s) === "substantive_update_required"
              ? "substantive-update"
              : resolveEulaRequirementState(s) === "reacknowledgement_required"
                ? "reacknowledge"
                : resolveEulaRequirementState(s) === "configuration_error"
                  ? "configuration-error"
                  : "first-run",
          eulaText: s.bundled_text ?? "",
          eulaVersion: s.bundled_version ?? "",
          eulaSha256: s.bundled_sha256 ?? "",
          softwareVersion: s.software_version,
          changeBullets: s.summary,
        }
      );
    }).catch(() => {});
    return () => { if (unlistenFn) unlistenFn(); };
  }, [initialPayload]);

  const inner = payload ? (
    payload.mode === "amendment" ||
    payload.mode === "substantive-update" ||
    payload.mode === "configuration-error"
      ? <SubstantiveUpdateView
          payload={payload}
          inline={inline}
          onAccepted={onAccepted}
          configurationError={payload.mode === "configuration-error"}
        />
      : payload.mode === "reacknowledge"
        ? <ReacknowledgeView payload={payload} inline={inline} onAccepted={onAccepted} />
        : <FirstRunView payload={payload} inline={inline} onAccepted={onAccepted} />
  ) : (
    <div data-testid="eula-popup-loading" />
  );

  return inline ? <InlineShell width={540}>{inner}</InlineShell> : <PopupShell width={560}>{inner}</PopupShell>;
}

// Compact inline card sized to fit inside the 760x540 main window. Drops the
// PopupShell side effect that forces document.body transparent (which would
// break the regular app background) and uses tighter padding so the title
// and buttons stay on-screen.
function InlineShell({ children, width }: { children: React.ReactNode; width: number }) {
  return (
    <div className="w-full h-full flex items-center justify-center" style={{ background: "transparent" }}>
      <div
        className="relative rounded-3xl px-6 py-5"
        style={{
          width,
          // Near-opaque (lvl-4 overlay surface) so the card and its legal text
          // stay legible over only a light dim -- we intentionally do NOT rely
          // on a heavy backdrop frost to make the card read as solid.
          background: "var(--surface-overlay)",
          border: "1px solid var(--border-emphasis)",
          boxShadow:
            "inset 0 1px 0 var(--rim-specular), inset 0 -1px 0 rgba(255, 255, 255, 0.2)",
        }}
      >
        {children}
      </div>
    </div>
  );
}

// Shared accept handler logic — abstracted so both views call the same flow.
async function performAccept(
  payload: EulaEmitPayload,
  setBusy: (b: boolean) => void,
  setError: (e: string | null) => void,
  inline: boolean,
  onAccepted?: () => void | Promise<void>,
  errorMessage = "Couldn't save acceptance. Try again in a moment.",
) {
  setBusy(true);
  setError(null);
  try {
    await acceptEula({
      eula_version: payload.eulaVersion,
      eula_sha256: payload.eulaSha256,
    });
    try { await flushLogSync(); } catch {}
    try { await emit("eula-accepted"); } catch {}
    await onAccepted?.();
    // In inline mode the parent unmounts this component when the
    // `eula-accepted` listener refetches status, so calling hide()
    // would hide the main window. Only hide in popup-window mode.
    if (!inline) {
      try {
        if (!(await applyWindowLifecycle("eula", "hide"))) {
          await getCurrentWindow().hide();
          await publishPopupVisibility("popup-eula", false);
        }
      } catch {}
    }
  } catch (err) {
    console.error(`[EULA] accept write failed error_type=${errorType(err)}`);
    setError(errorMessage);
    setBusy(false);
  }
}

function FirstRunView({ payload, inline = false, onAccepted }: { payload: EulaEmitPayload; inline?: boolean; onAccepted?: () => void | Promise<void> }) {
  const wellRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Initial focus on the ReadingWell (design spec).
  useEffect(() => {
    const region = document.querySelector<HTMLDivElement>('[role="region"][aria-label="End User License Agreement, scrollable"]');
    region?.focus();
  }, []);

  // Swallow Escape in first-run mode.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleAccept = async () => {
    if (busy) return;
    console.log("[EULA] accept clicked");
    await performAccept(payload, setBusy, setError, inline, onAccepted);
  };

  const handleDecline = async () => {
    if (busy) return;
    setBusy(true);
    console.log("[EULA] decline clicked action=exit");
    try { await flushLogSync(); } catch {}
    try { await exit(0); } catch { setBusy(false); }
  };

  // Buttons row: Decline | I agree — equal width, gap-2, flex-1.
  return (
    <div role="dialog" aria-modal="true" aria-labelledby="eula-heading" aria-describedby="eula-subtitle">
      <h2 id="eula-heading" className="font-display text-[18px] font-normal text-slate text-center mb-1.5">
        End User License Agreement
      </h2>
      <p id="eula-subtitle" className="text-[12px] text-steel/80 text-center mb-4">
        Review the Whisperstream End User License Agreement below, then choose.
      </p>
      <div className="mb-4" ref={wellRef}>
        <ReadingWell height={inline ? 300 : 420} ariaLabel="End User License Agreement, scrollable">
          {payload.eulaText}
        </ReadingWell>
      </div>
      <div className="flex gap-2 mb-3">
        <button
          type="button"
          onClick={handleDecline}
          disabled={busy}
          className="flex-1 py-2 rounded-xl text-[13px] text-slate bg-white/40 hover:bg-white/60 border border-white/60 transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          Decline
        </button>
        <button
          type="button"
          onClick={handleAccept}
          disabled={busy}
          className="flex-1 py-2 rounded-xl text-[13px] text-white bg-frost-deep hover:bg-frost transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          I agree
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-[12px] text-red-600 text-center mb-2">{error}</p>
      ) : null}
      <p className="text-[11px] text-steel/60 text-center leading-relaxed">
        {/mac/i.test(navigator.platform)
          ? "To remove Whisperstream after declining, move it from Applications to the Trash."
          : "If you chose Decline and want to remove Whisperstream, uninstall via Windows Settings > Apps."}
      </p>
    </div>
  );
}

function SubstantiveUpdateView({
  payload,
  inline = false,
  onAccepted,
  configurationError = false,
}: {
  payload: EulaEmitPayload;
  inline?: boolean;
  onAccepted?: () => void | Promise<void>;
  configurationError?: boolean;
}) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [view, setView] = useState<"summary" | "terms">("summary");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(
    configurationError
      ? "Couldn't save your acceptance. Nothing was accepted. Try again."
      : null,
  );
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const reviewAvailable = payload.eulaText.length > 0;
  const acceptanceAvailable =
    !configurationError &&
    reviewAvailable &&
    payload.eulaVersion.length > 0 &&
    payload.eulaSha256.length > 0;

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (view === "terms") {
      document
        .querySelector<HTMLElement>(
          `[role="region"][aria-label="End User License Agreement, version ${payload.eulaVersion}"]`,
        )
        ?.focus();
    } else {
      headingRef.current?.focus();
    }
  }, [payload.eulaVersion, view]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleSave = async () => {
    if (busy || !reviewAvailable) return;
    setBusy(true);
    setSaveStatus("Saving...");
    try {
      const result = await saveExactEulaTerms(
        payload.eulaText,
        payload.eulaVersion,
      );
      setSaveStatus(result === "saved" ? "Terms saved." : null);
    } catch {
      setSaveStatus("Couldn't save the terms. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const handleAccept = async () => {
    if (busy || !acceptanceAvailable) return;
    await performAccept(
      payload,
      setBusy,
      setError,
      inline,
      onAccepted,
      "Couldn't save your acceptance. Nothing was accepted. Try again.",
    );
  };

  const handleDecline = async () => {
    if (busy) return;
    setBusy(true);
    try { await flushLogSync(); } catch {}
    try { await exit(0); } catch { setBusy(false); }
  };

  const controlStyle = {
    background: "var(--surface-control)",
    border: "1px solid var(--border-hairline)",
    boxShadow: "inset 0 1px 0 var(--rim-specular)",
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="eula-updated-heading"
      aria-describedby="eula-updated-intro"
      data-testid="eula-substantive-update"
    >
      <h2
        ref={headingRef}
        tabIndex={-1}
        id="eula-updated-heading"
        className="font-display text-[18px] font-normal text-slate text-center mb-1.5 outline-none"
      >
        Review updated terms
      </h2>
      <p id="eula-updated-intro" className="text-[12px] text-steel text-center mb-2 leading-relaxed">
        This release requires your acceptance of updated terms before Whisperstream can process your content.
      </p>
      {payload.eulaVersion ? (
        <p className="text-[11px] text-steel/70 text-center mb-4">
          End User License Agreement, version {payload.eulaVersion}
        </p>
      ) : (
        <div className="mb-4" />
      )}

      {view === "summary" ? (
        <div data-testid="eula-summary-view" className="mb-4">
          <ReadingWell
            height={inline ? 246 : 340}
            ariaLabel="Summary of updated terms, scrollable"
          >
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-slate mb-2">
                What changed
              </p>
              <ul className="space-y-1.5 mb-3">
                {(payload.changeBullets ?? []).map((bullet, index) => (
                  <li key={index} className="relative pl-4 text-[12px] leading-relaxed text-slate">
                    <span aria-hidden="true" className="absolute left-0 top-[0.58em] h-1 w-1 rounded-full" style={{ background: "var(--color-frost-deep)" }} />
                    {bullet}
                  </li>
                ))}
              </ul>
              <div className="space-y-2 text-[11px] leading-relaxed text-steel">
                <p>Until you accept, content features such as dictation, imports, enhancement, transcript access, and Agent Speak stay unavailable.</p>
                <p>Accepting updated terms does not change the agreement that governed an earlier release or rights that already accrued.</p>
                <p>Installing or relaunching this release does not accept the updated terms.</p>
              </div>
            </div>
          </ReadingWell>
        </div>
      ) : (
        <div data-testid="eula-terms-view" className="mb-4">
          <div className="outline-none">
            <ReadingWell height={inline ? 246 : 340} ariaLabel={`End User License Agreement, version ${payload.eulaVersion}`}>
              {payload.eulaText}
            </ReadingWell>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 mb-2">
        <button
          type="button"
          onClick={() => setView((current) => current === "summary" ? "terms" : "summary")}
          disabled={!reviewAvailable || busy}
          className="py-2 rounded-xl text-[12px] text-slate transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50"
          style={controlStyle}
        >
          {view === "summary" ? "Read complete terms" : "Back to summary"}
        </button>
        <button
          type="button"
          onClick={handleSave}
          disabled={!reviewAvailable || busy}
          className="py-2 rounded-xl text-[12px] text-slate transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50"
          style={controlStyle}
        >
          {saveStatus === "Saving..." ? "Saving..." : "Save a copy"}
        </button>
        <button
          type="button"
          onClick={handleAccept}
          disabled={!acceptanceAvailable || busy}
          className="py-2 rounded-xl text-[12px] font-medium text-white disabled:opacity-50"
          style={{ background: "var(--accent-fill)", border: "1px solid var(--border-accent)", boxShadow: "var(--shadow-pearl)" }}
        >
          Accept updated terms
        </button>
        <button
          type="button"
          onClick={handleDecline}
          disabled={busy}
          className="py-2 rounded-xl text-[12px] text-slate transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50"
          style={controlStyle}
        >
          Decline and quit
        </button>
      </div>

      <div aria-live="polite" aria-atomic="true" className="min-h-4 text-center text-[11px] text-steel">
        {saveStatus}
      </div>
      {error ? <p role="alert" className="text-[11px] text-rose text-center mt-1">{error}</p> : null}
      <p className="text-[11px] text-steel/70 text-center mt-2 leading-relaxed">
        Declining or closing Whisperstream does not accept these terms or create an acceptance record.
      </p>
    </div>
  );
}

// Re-acknowledgement view (EULA Section 20, "Re-acknowledgement"). Shown once
// when the affirmative re-acknowledgement floor is raised for a NON-material
// update: the user already accepted a prior version and must affirmatively
// acknowledge the current terms to continue. Accept-or-exit, like first-run:
// there is deliberately NO decline-and-pin / pro-rata-refund branch (that is
// the material-amendment flow only, and offering it here would wrongly signal
// materiality). Shows a plain-language summary of what changed plus the full
// current terms to acknowledge.
function ReacknowledgeView({ payload, inline = false, onAccepted }: { payload: EulaEmitPayload; inline?: boolean; onAccepted?: () => void | Promise<void> }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Swallow Escape: this is an accept-or-exit acknowledgement, not dismissible.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const handleAccept = async () => {
    if (busy) return;
    console.log("[EULA] reacknowledge accept clicked");
    await performAccept(payload, setBusy, setError, inline, onAccepted);
  };

  const handleDecline = async () => {
    if (busy) return;
    setBusy(true);
    console.log("[EULA] reacknowledge decline clicked action=exit");
    try { await flushLogSync(); } catch {}
    try { await exit(0); } catch { setBusy(false); }
  };

  const hasBullets = payload.changeBullets && payload.changeBullets.length > 0;

  return (
    <div role="dialog" aria-modal="true" aria-labelledby="eula-heading" aria-describedby="eula-subtitle">
      <h2 id="eula-heading" className="font-display text-[18px] font-normal text-slate text-center mb-1.5">
        We&apos;ve updated our terms
      </h2>
      <p id="eula-subtitle" className="text-[12px] text-steel/80 text-center mb-4">
        Please review and acknowledge the current terms to continue.
      </p>

      {hasBullets ? (
        <div className="mb-4">
          <p className="text-[11px] uppercase tracking-wider text-steel/70 mb-2">
            What&apos;s changed
          </p>
          <ul className="space-y-2">
            {payload.changeBullets!.map((bullet, i) => (
              <li key={i} className="text-[13px] text-slate leading-snug pl-4 relative">
                <span
                  aria-hidden="true"
                  className="absolute left-0 top-[0.55em] w-1 h-1 rounded-full"
                  style={{ background: "var(--color-frost-deep)" }}
                />
                {bullet}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="mb-4">
        <ReadingWell height={inline ? 220 : 320} ariaLabel="End User License Agreement, scrollable">
          {payload.eulaText}
        </ReadingWell>
      </div>

      <div className="flex gap-2 mb-3">
        <button
          type="button"
          onClick={handleDecline}
          disabled={busy}
          className="flex-1 py-2 rounded-xl text-[13px] text-slate bg-white/40 hover:bg-white/60 border border-white/60 transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          Decline
        </button>
        <button
          type="button"
          onClick={handleAccept}
          disabled={busy}
          className="flex-1 py-2 rounded-xl text-[13px] text-white bg-frost-deep hover:bg-frost transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          I agree
        </button>
      </div>
      {error ? (
        <p role="alert" className="text-[12px] text-red-600 text-center mb-2">{error}</p>
      ) : null}
      <p className="text-[11px] text-steel/60 text-center leading-relaxed">
        Declining closes Whisperstream; your current terms stay in effect, and we&apos;ll ask again next time you open it.
      </p>
    </div>
  );
}
