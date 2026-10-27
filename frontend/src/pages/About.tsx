import { useCallback, useEffect, useState } from "react";
import { motion } from "framer-motion";
import { open } from "@tauri-apps/plugin-shell";
import { PrimaryButton } from "../components/PrimaryButton";
import { SecondaryButton } from "../components/SecondaryButton";
import { exportLogs } from "../lib/api/diagnostics";
import { getVersion } from "../lib/api/system";
import { getLockdownStatus } from "../lib/api/lockdown";
import { DocumentationExportButton } from "../components/DocumentationExportButton";
import { ReportBugDialog } from "../components/ReportBugDialog";
import { downloadLegalDocument, LEGAL_DOCUMENTS } from "../lib/legalDocuments";
import { errorType } from "../lib/logCapture";
import type { DailyUseCapabilityReport } from "../lib/dailyUseCapabilities";
import { officialUpdater, type OfficialUpdateOffer } from "../lib/officialUpdater";

const spring = { type: "spring" as const, stiffness: 400, damping: 30 };

type DiagState = "idle" | "exporting" | "success" | "denied" | "error";

const BUG_REPORT_URL = "https://github.com/lanreal-io/whisperstream/issues/new?template=bug_report.yml";
const BUG_REPORT_EMAIL = "mailto:support@whisperstream.io?subject=Whisperstream%20bug%20report";

const FOOTER_LINKS = [
  { label: "Changelog", url: "https://whisperstream.io/changelog" },
  { label: "Website", url: "https://whisperstream.io" },
];

function HelpIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6.5" />
      <path d="M6 6.5a2 2 0 013.5 1.5c0 1-1.5 1.5-1.5 1.5" />
      <circle cx="8" cy="12" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

function ExternalLinkIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="12"
      height="12"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M6.5 2h3.5v3.5" />
      <path d="M10 2 5.5 6.5" />
      <path d="M9 7v2a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h2" />
    </svg>
  );
}

interface AboutProps {
  onUpdateAvailable?: (update: OfficialUpdateOffer) => Promise<void>;
  lockdownEnabled?: boolean;
  capabilities?: DailyUseCapabilityReport;
}

export function About({ onUpdateAvailable, lockdownEnabled, capabilities }: AboutProps = {}) {
  const [lockdown, setLockdown] = useState<boolean | null>(null);
  const [helpError, setHelpError] = useState("");
  const [reportOpen, setReportOpen] = useState(false);
  const [legalError, setLegalError] = useState(false);
  const effectiveLockdown = lockdownEnabled === true || lockdown === true
    ? true : lockdown === false ? false : null;
  useEffect(() => {
    const refresh = () => {
      getLockdownStatus()
        .then(status => setLockdown(status.enabled !== false || status.managed !== false))
        .catch(() => setLockdown(null));
    };
    refresh();
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [lockdownEnabled]);

  // Admission is checked again at every external handoff, including an email
  // composed while the policy was different. Unknown/incomplete policy blocks.
  const withExternalAccess = useCallback(async (action: () => void | Promise<void>) => {
    setHelpError("");
    try {
      const status = await getLockdownStatus();
      if (status.enabled !== false || status.managed !== false) {
        setLockdown(true);
        setHelpError("External links are unavailable in Lockdown Mode.");
        return;
      }
      await action();
    } catch {
      setHelpError("Could not open the link. Try again.");
    }
  }, []);

  // Report a bug asks for logs first; each destination still passes the
  // fresh external-access check above.
  const handleReportHandoff = useCallback(async (url: string) => {
    await withExternalAccess(() => open(url));
    setReportOpen(false);
  }, [withExternalAccess]);

  const [version, setVersion] = useState<string | null>(null);
  const [logState, setLogState] = useState<DiagState>("idle");

  useEffect(() => {
    getVersion()
      .then((res) => setVersion(res.version))
      .catch(() => {});
  }, []);

  const handleExportLogs = async () => {
    if (logState === "exporting") return;
    setLogState("exporting");
    try {
      const result = await exportLogs();
      if (result === "cancelled") {
        setLogState("idle");
      } else if (result === "denied") {
        setLogState("denied");
        setTimeout(() => setLogState("idle"), 3000);
      } else {
        setLogState("success");
        setTimeout(() => setLogState("idle"), 1500);
      }
    } catch {
      setLogState("error");
      setTimeout(() => setLogState("idle"), 3000);
    }
  };

  const logLabel =
    logState === "idle"
      ? "Export logs"
      : logState === "exporting"
        ? "Preparing\u2026"
        : logState === "success"
          ? "Exported"
          : logState === "denied"
            ? "Unavailable in Lockdown"
            : "Couldn't export";

  const logColor =
    logState === "success"
      ? "var(--color-mint)"
      : logState === "error" || logState === "denied"
        ? "var(--color-rose)"
        : "var(--color-steel)";

  return (
    <div className="flex flex-col h-full max-w-lg mx-auto">
          <motion.div
            key="about"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="flex flex-col h-full"
          >
            {/* Header: wordmark + version cluster (version + check-for-updates link + status caption) */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0 }}
              className="flex items-start justify-between pt-4 pr-12"
            >
              <h3 className="font-display text-[18px] font-light tracking-[0.06em] text-slate/80 leading-none">
                whisperstream
              </h3>
              <div className="flex flex-col items-end gap-0.5">
                <span
                  className="text-[13px] text-slate"
                  style={{ fontVariantNumeric: "tabular-nums" }}
                >
                  {version ? `Version ${version}` : "Version unavailable"}
                </span>
                {officialUpdater && (
                  <officialUpdater.AboutUpdateCheck
                    capabilities={capabilities}
                    lockdown={effectiveLockdown}
                    onUpdateAvailable={onUpdateAvailable}
                  />
                )}
                {effectiveLockdown === false && <button
                  type="button"
                  aria-label="Source code, opens in your browser"
                  onClick={() => void withExternalAccess(() => open("https://github.com/lanreal-io/whisperstream"))}
                  className="mt-1 text-[11px] font-medium text-steel hover:text-slate hover:underline cursor-pointer transition-colors"
                >Source code</button>}
              </div>
            </motion.div>

            {/* Action column — narrow, centered horizontally and vertically */}
            <div className="max-w-xs w-full mx-auto my-auto">
            {effectiveLockdown === false ? <>
            {/* Get help — primary */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0.04 }}
            >
              <PrimaryButton
                type="button"
                aria-haspopup="dialog"
                onClick={() => setReportOpen(true)}
                fullWidth
                className="flex items-center justify-center gap-2 h-11 text-[13px]"
              >
                <HelpIcon className="text-white/80" />
                Report a bug
              </PrimaryButton>
            </motion.div>

            {/* Request a feature — secondary */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0.07 }}
              className="mt-3"
            >
              <SecondaryButton
                type="button"
                aria-label="Request a feature on GitHub, opens in your browser"
                onClick={() => void withExternalAccess(() => open("https://github.com/lanreal-io/whisperstream/issues/new?template=feature_request.yml"))}
                className="flex items-center justify-center gap-1.5 w-full h-10 px-6 rounded-full text-[13px] font-medium text-steel"
              >
                Request a feature
                <ExternalLinkIcon className="text-steel/70" />
              </SecondaryButton>
            </motion.div>

            {/* Send feedback — secondary (matches Request a feature) */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0.085 }}
              className="mt-3"
            >
              <SecondaryButton
                type="button"
                aria-label="Read documentation, opens in your browser"
                onClick={() => void withExternalAccess(() => open("https://whisperstream.io/docs"))}
                className="flex items-center justify-center w-full h-10 px-6 rounded-full text-[13px] font-medium text-steel"
              >
                Read documentation
              </SecondaryButton>
            </motion.div>

            </> : <p className="text-center text-[12px] text-steel">{effectiveLockdown === true ? "External links are unavailable in Lockdown Mode." : "Checking external-link availability..."}</p>}
            {/* Export logs — tertiary text link */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0.1 }}
              className="mt-4 flex flex-col items-center"
            >
              <button
                type="button"
                onClick={handleExportLogs}
                disabled={logState === "exporting"}
                className="text-[12px] font-medium hover:underline cursor-pointer transition-colors disabled:cursor-not-allowed disabled:opacity-70 disabled:hover:no-underline py-0.5"
                style={{ color: logColor }}
              >
                {logLabel}
              </button>
            </motion.div>
            </div>

            {/* Footer — legal/info links */}
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ ...spring, delay: 0.13 }}
              className="flex items-center justify-center flex-wrap gap-x-1.5 text-[11px] leading-tight"
            >
              <DocumentationExportButton />
              {LEGAL_DOCUMENTS.map(link => <button key={link.url} type="button"
                onClick={async () => {
                  setLegalError(false);
                  try { await downloadLegalDocument(link); }
                  catch (err) { setLegalError(true); console.error(`[About] legal download failed error_type=${errorType(err)}`); }
                }} aria-label={`Download ${link.label}`}
                className="text-steel/70 hover:text-steel hover:underline cursor-pointer">{link.label}</button>)}
              {effectiveLockdown === false && FOOTER_LINKS.map((link, i) => (
                <span key={link.url} className="flex items-center gap-x-1.5">
                  <button
                    type="button"
                    onClick={() => void withExternalAccess(() => open(link.url))}
                    aria-label={`${link.label}, opens in your browser`}
                    className="text-steel/70 hover:text-steel hover:underline cursor-pointer transition-colors"
                  >
                    {link.label}
                  </button>
                  {i < FOOTER_LINKS.length - 1 && (
                    <span className="text-steel/40" aria-hidden="true">
                      ·
                    </span>
                  )}
                </span>
              ))}
            </motion.div>
          </motion.div>

      {helpError && <p role="alert" className="text-[12px] text-rose text-center mt-2">{helpError}</p>}
      {legalError && <p role="alert" className="text-[12px] text-rose text-center mt-2">Could not download the document. Please try again.</p>}
      <ReportBugDialog
        open={reportOpen}
        onOpenIssues={() => void handleReportHandoff(BUG_REPORT_URL)}
        onEmail={() => void handleReportHandoff(BUG_REPORT_EMAIL)}
        onClose={() => setReportOpen(false)}
      />
    </div>
  );
}

export default About;
