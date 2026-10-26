import { motion, AnimatePresence } from "framer-motion";
import logoUrl from "../assets/logo.png";
import { EnhancementIcon } from "./EnhancementIcon";
import { usesNativeWindowControls } from "../lib/windowChrome";

export type TabId = "home" | "audio" | "controls" | "dictionary" | "transcripts" | "enhancement" | "agents" | "about" | "ui-test";

function HomeIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="5" height="5" rx="1.5" />
      <rect x="9" y="2" width="5" height="5" rx="1.5" />
      <rect x="2" y="9" width="5" height="5" rx="1.5" />
      <rect x="9" y="9" width="5" height="5" rx="1.5" />
    </svg>
  );
}

function AudioIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5.5" y="1.5" width="5" height="7.5" rx="2.5" />
      <path d="M3.5 7.5a4.5 4.5 0 009 0" />
      <line x1="8" y1="12" x2="8" y2="14" />
      <line x1="5.5" y1="14" x2="10.5" y2="14" />
    </svg>
  );
}

function ShortcutsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="1.5" y="4" width="13" height="8.5" rx="2" />
      <circle cx="5" cy="7" r="0.75" fill="currentColor" stroke="none" />
      <circle cx="8" cy="7" r="0.75" fill="currentColor" stroke="none" />
      <circle cx="11" cy="7" r="0.75" fill="currentColor" stroke="none" />
      <line x1="5" y1="10" x2="11" y2="10" />
    </svg>
  );
}

function DictionaryIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M8 3.5v10" />
      <path d="M8 3.5c-1.5-1-3.5-1.5-5.5-1v9c2-.5 4 0 5.5 1" />
      <path d="M8 3.5c1.5-1 3.5-1.5 5.5-1v9c-2-.5-4 0-5.5 1" />
    </svg>
  );
}

function TranscriptsIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2.5" y="2" width="11" height="12" rx="2" />
      <line x1="5" y1="5.5" x2="11" y2="5.5" />
      <line x1="5" y1="8" x2="11" y2="8" />
      <line x1="5" y1="10.5" x2="8.5" y2="10.5" />
    </svg>
  );
}

function AboutIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="8" r="6.5" />
      <line x1="8" y1="7.5" x2="8" y2="11.5" />
      <circle cx="8" cy="5" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

function AgentsIcon({ className }: { className?: string }) {
  return <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 2h7a1.5 1.5 0 0 1 1.5 1.5V7A1.5 1.5 0 0 1 10 8.5H6l-2.5 2v-2H3A1.5 1.5 0 0 1 1.5 7V3.5A1.5 1.5 0 0 1 3 2ZM7.5 11.5h3l2.5 2v-2a1.5 1.5 0 0 0 1.5-1.5V6.5" /></svg>;
}

function UiTestIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {/* Window / layers glyph */}
      <rect x="1.5" y="3.5" width="13" height="9" rx="1.75" />
      <line x1="1.5" y1="6.5" x2="14.5" y2="6.5" />
      <circle cx="3.75" cy="5" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="6" cy="5" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="8.25" cy="5" r="0.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

const tabs: { id: TabId; label: string; icon: (p: { className?: string }) => React.JSX.Element }[] = [
  { id: "home", label: "Home", icon: HomeIcon },
  { id: "audio", label: "Audio", icon: AudioIcon },
  { id: "controls", label: "Controls", icon: ShortcutsIcon },
  { id: "dictionary", label: "Dictionary", icon: DictionaryIcon },
  { id: "transcripts", label: "Transcripts", icon: TranscriptsIcon },
  { id: "enhancement", label: "Enhancement", icon: EnhancementIcon },
  { id: "agents", label: "Agents", icon: AgentsIcon },
  { id: "about", label: "About", icon: AboutIcon },
  { id: "ui-test", label: "UI Test", icon: UiTestIcon },
];

interface SidebarProps {
  activeTab: TabId;
  onTabChange: (tab: TabId) => void;
  status: { message: string; isError: boolean } | null;
  devEnabled?: boolean;
  /** Gold attention dot on a tab, with its reason as the accessible label. */
  attention?: Partial<Record<TabId, string>>;
}

export function Sidebar({ activeTab, onTabChange, status, devEnabled = false, attention }: SidebarProps) {
  const nativeWindowControls = usesNativeWindowControls();
  return (
    <div className="w-[228px] h-full shrink-0 flex flex-col select-none overflow-hidden backdrop-blur-xl border-r"
      style={{ backgroundColor: "var(--surface-sidebar)", borderColor: "var(--border-standard)" }}>
      {/* Brand */}
      <motion.div
        initial={{ opacity: 0, y: -3 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: [0.2, 0.8, 0.2, 1] }}
        className={`px-4 ${nativeWindowControls ? "pt-13" : "pt-8"} pb-5 flex items-center gap-[7px]`}
      >
        <img
          src={logoUrl}
          alt=""
          aria-hidden="true"
          draggable={false}
          className="h-3.5 w-auto shrink-0 translate-y-[2px] select-none pointer-events-none"
          style={{
            filter: "drop-shadow(0 1px 2px rgba(120, 160, 200, 0.18))",
          }}
        />
        <h1 className="font-display text-[14px] font-light tracking-[0.06em] text-slate/80 leading-none">
          whisperstream
        </h1>
      </motion.div>

      {/* Navigation */}
      <nav className="flex-1 flex flex-col gap-0.5 px-2.5">
        {tabs.filter(t => {
          if (t.id === "ui-test") return devEnabled;
          return true;
        }).map(({ id, label, icon: Icon }) => {
          const isActive = activeTab === id;
          return (
            <button
              key={id}
              onClick={() => onTabChange(id)}
              aria-describedby={attention?.[id] ? `sidebar-attention-${id}` : undefined}
              className="relative flex items-center gap-2.5 px-3.5 py-2 rounded-full text-[13px] font-medium text-left transition-colors duration-200 cursor-pointer group"
            >
              {isActive && (
                // No backdrop blur: nested inside the sidebar's blur it left a
                // vertical seam in WebView2.
                <div
                  data-testid="active-tab-surface"
                  className="absolute inset-0 rounded-full border"
                  style={{
                    backgroundColor: "var(--nav-active-fill)",
                    borderColor: "var(--nav-active-border)",
                    boxShadow: "var(--nav-active-glow)",
                  }}
                />
              )}
              <Icon
                className={`relative z-10 w-4 h-4 transition-colors duration-200 ${
                  isActive ? "text-slate" : "text-steel group-hover:text-slate/60"
                }`}
              />
              <span
                className={`relative z-10 transition-colors duration-200 ${
                  isActive ? "text-slate" : "text-steel group-hover:text-slate/60"
                }`}
              >
                {label}
              </span>
              {(id === "ui-test") && (
                <span
                  aria-hidden
                  className="relative z-10 ml-auto w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ background: "var(--color-gold)" }}
                />
              )}
              {attention?.[id] && (
                <>
                  <span id={`sidebar-attention-${id}`} className="sr-only">{attention[id]}</span>
                  <span
                    aria-hidden
                    className="relative z-10 ml-auto w-1.5 h-1.5 rounded-full shrink-0"
                    style={{ background: "var(--color-gold)" }}
                  />
                </>
              )}
            </button>
          );
        })}
      </nav>

      {/* Bottom section: transient status */}
      <div className="px-2.5 pb-5 flex flex-col">
        {/* Status toast. The container is always mounted and carries the live
            region so an inserted message is announced; a region that appears
            with its own text is unreliable in screen readers. */}
        <div role="status" aria-live="polite" className="px-0.5">
          <AnimatePresence>
            {status && (
              <motion.p
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={{ duration: 0.25 }}
                className={`text-[11px] text-center leading-snug px-1 ${
                  status.isError ? "text-rose" : "text-mint"
                }`}
              >
                {status.message}
              </motion.p>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
