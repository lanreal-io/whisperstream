import type { ReactNode } from "react";
import { motion } from "framer-motion";

export function SettingsSection({
  index,
  title,
  description,
  children,
  inline,
  last,
  dimmed,
}: {
  index: number;
  title: string;
  description?: string;
  children: ReactNode;
  inline?: boolean;
  last?: boolean;
  /** Gated child row whose master toggle is off. Dims the text column so the
   *  row doesn't read as "an enabled setting with a broken switch". 50%, not
   *  the Toggle's 40%, so it stays legible as the explanation of why it's
   *  unavailable. */
  dimmed?: boolean;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: inline ? 6 : 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{
        delay: index * 0.03,
        type: "spring",
        stiffness: 400,
        damping: 30,
      }}
    >
      {inline ? (
        <>
          <div className="flex items-center justify-between py-3 gap-4">
            <div
              className={dimmed === undefined ? "min-w-0" : `min-w-0 transition-opacity duration-200 ${dimmed ? "opacity-50" : ""}`}
            >
              <div className="text-[13px] font-medium text-slate">{title}</div>
              {description && <div className="text-[11px] text-steel mt-0.5">{description}</div>}
            </div>
            <div className="shrink-0">{children}</div>
          </div>
          {!last && <div className="h-px" style={{ background: "var(--divider)" }} />}
        </>
      ) : (
        <>
          <div className="text-[13px] font-medium text-slate mb-1">{title}</div>
          {description && (
            <div className="text-[11px] text-steel mb-3">{description}</div>
          )}
          {children}
        </>
      )}
    </motion.div>
  );
}
