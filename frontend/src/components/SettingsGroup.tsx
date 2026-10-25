import { type ReactNode } from "react";
import { motion } from "framer-motion";

/**
 * SettingsGroup — the shared group heading for settings pages (Controls).
 * A heading names the *territory* (the feature) and the rows
 * beneath it name their individual switches, so a group never repeats a row's
 * title.
 *
 * Divider grammar (D3): a heading is earned by two or more rows, and a
 * --divider hairline only marks a boundary that has NO heading to mark it. So
 * rows inside a group are joined by whitespace with no hairlines, and two
 * adjacent groups are separated by the 32px gap plus the next heading rather
 * than by a line.
 *
 * The heading recipe (10px semibold uppercase, 0.12em tracking) is deliberately
 * distinct from AdvancedDisclosure's control label (11px medium steel/50), so an
 * "Advanced" collapsible nested inside a group still reads as subordinate. It
 * uses `text-slate` rather than the `text-steel` that HudConfigModal's local
 * Group uses: steel at 10px is a 3.9:1 WCAG AA failure. The heading is quieted
 * by size, case, and tracking, never by contrast.
 *
 * `<section aria-labelledby>` is the a11y payload: every row inside inherits the
 * group name in the accessibility tree, which is what makes the shortened row
 * titles ("Show indicator" under "Recording Indicator") unambiguous.
 */
export function SettingsGroup({
  title,
  children,
  first = false,
  entranceDelay = 0,
  id,
}: {
  /** Title Case in source; CSS uppercases it so search and screen readers still
   *  get real words. */
  title: string;
  children: ReactNode;
  /** Drops the group's top margin. Pass on the first group of a page. */
  first?: boolean;
  /** Framer entrance delay in seconds, so the heading joins the page cascade. */
  entranceDelay?: number;
  id?: string;
}) {
  const headingId = id ?? `settings-group-${slug(title)}`;
  return (
    <section aria-labelledby={headingId} className={first ? undefined : "mt-5"}>
      <motion.h3
        id={headingId}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{
          delay: entranceDelay,
          type: "spring",
          stiffness: 400,
          damping: 30,
        }}
        className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate mb-0.5"
      >
        {title}
      </motion.h3>
      {children}
    </section>
  );
}

function slug(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
