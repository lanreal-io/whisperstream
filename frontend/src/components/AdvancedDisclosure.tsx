import { useState, type ReactNode } from "react";
import { motion, AnimatePresence } from "framer-motion";

/**
 * AdvancedDisclosure — the shared "Advanced" collapsible used by the Home and
 * Enhancement tabs. Previously each page hand-rolled its own header, and they
 * drifted: Home had an UPPERCASE label with an up/down chevron, Enhancement had
 * a lowercase label with a sideways `>`. The unified version keeps Home's
 * UPPERCASE label and Enhancement's sideways chevron (rotates 90deg, pointing
 * right when collapsed and down when expanded). Single source of truth so the
 * two tabs can never diverge again.
 *
 * The caller owns the open state and renders the rows (SettingRow / Section
 * inline) as children. `entranceDelay` lets a page stagger the header in line
 * with its other settings rows (Home passes its index-based delay).
 */
export function AdvancedDisclosure({
  open,
  onToggle,
  children,
  entranceDelay = 0,
  allowContentOverflow = false,
}: {
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
  entranceDelay?: number;
  /** Allow inline help to escape after expansion finishes; animate while clipped. */
  allowContentOverflow?: boolean;
}) {
  const [expansionSettled, setExpansionSettled] = useState(false);
  return (
    <div>
      <motion.button
        type="button"
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: entranceDelay, type: "spring", stiffness: 400, damping: 30 }}
        onClick={onToggle}
        aria-expanded={open}
        className="flex items-center gap-1.5 mt-3 mb-1 cursor-pointer group"
      >
        <span className="text-[11px] text-steel/50 font-medium uppercase tracking-wider group-hover:text-steel/70 transition-colors">
          Advanced
        </span>
        <motion.svg
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          className="text-steel/40 group-hover:text-steel/60 transition-colors"
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ duration: 0.15 }}
        >
          <path d="M3.5 2.5l3 2.5-3 2.5" />
        </motion.svg>
      </motion.button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ type: "spring", stiffness: 400, damping: 30 }}
            onAnimationStart={() => setExpansionSettled(false)}
            onAnimationComplete={() => setExpansionSettled(true)}
            className={allowContentOverflow && open && expansionSettled ? "overflow-visible" : "overflow-hidden"}
          >
            {children}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
