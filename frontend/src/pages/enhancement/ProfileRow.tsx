import { type Profile } from "../../lib/profiles";
import { motion } from "framer-motion";
import { ProfileMark } from "../../components/ProfileMark";
import { HoverTooltip } from "../../components/HoverTooltip";
import { AppIcon, SiteGlyph } from "../../components/RunningAppPicker";


/* ---------- One profile row (slim whole-row button) ----------
 * Round 04 (issues 5/6/8): the default radio and the hover edit-pencil /
 * inline row-delete are gone. The WHOLE row is one `<button>` that opens the
 * editor; deleting and setting-default both live inside the editor now. A faint
 * always-visible `›` chevron replaces the pencil. Contents L→R: name, a
 * `Default` pill ONLY on the default profile, the install-gated 18px icon-stack
 * preview (cap 3, then +N, each in HoverTooltip; empty = "No apps yet"), and the
 * chevron.
 *
 * Surface/border per the LOCKED elevation table: non-default rest 0.08 / hover
 * 0.18; default-rest 0.22 with a frost border / default-hover 0.28. */
export function ProfileRow({
  profile,
  isDefault,
  targets,
  onOpenEditor,
  reducedMotion,
}: {
  profile: Profile;
  isDefault: boolean;
  /** App and site targets routed to this profile (apps first, then sites). */
  targets: ({ kind: "app"; processName: string; label: string; icon: string | null } | { kind: "site"; domain: string; label: string; icon: string | null })[];
  /** Open this profile's editor modal (whole-row click). */
  onOpenEditor: () => void;
  reducedMotion: boolean;
}) {
  // Surface tokens per the elevation table (LOCKED).
  const restBg = isDefault ? "var(--surface-row-selected)" : "var(--surface-row)";
  const hoverBg = isDefault ? "var(--surface-row-selected-hover)" : "var(--surface-row-hover)";
  const restBorder = isDefault
    ? "var(--border-accent)"
    : "var(--border-standard)";

  // Icon stack caps at 3 visible; the rest collapse into a +N count chip.
  const VISIBLE_CAP = 3;
  const visibleTargets = targets.slice(0, VISIBLE_CAP);
  const overflowCount = Math.max(0, targets.length - VISIBLE_CAP);

  return (
    <motion.div
      layout={!reducedMotion}
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={
        reducedMotion
          ? { duration: 0 }
          : { duration: 0.25, ease: [0.16, 1, 0.3, 1] }
      }
      className="group/row"
    >
      <button
        type="button"
        onClick={onOpenEditor}
        aria-label={`Edit ${profile.name}`}
        className="w-full rounded-[var(--radius-row)] px-3.5 min-h-[44px] flex items-center gap-3 text-left cursor-pointer transition-colors duration-200"
        style={{ background: restBg, border: `1px solid ${restBorder}` }}
        onMouseEnter={(e) => (e.currentTarget.style.background = hoverBg)}
        onMouseLeave={(e) => (e.currentTarget.style.background = restBg)}
      >
        {/* The same stamp the recording indicator carries. This row is where
            the glyph vocabulary is learned, so it leads the row; it is
            decorative, since the row's aria-label already names the profile. */}
        <ProfileMark
          profileId={profile.id}
          name={profile.name}
          iconKey={profile.icon}
          size={20}
        />

        <span className="flex-1 min-w-0 text-[13px] font-medium text-slate truncate">
          {profile.name}
        </span>

        {isDefault && (
          <span
            className="shrink-0 inline-flex items-center h-5 px-2 rounded-full text-[10px] font-medium"
            style={{
              background: "color-mix(in srgb, var(--color-frost) 18%, transparent)",
              border: "1px solid var(--border-accent)",
              color: "var(--color-frost)",
            }}
          >
            Default
          </span>
        )}

        {/* Target icon stack: 18px icons, cap 3, then +N. Each icon wrapped in
            HoverTooltip (app label or registrable domain). */}
        <div className="flex items-center gap-1 shrink-0">
          {visibleTargets.length === 0 ? (
            <span className="text-[11px] text-steel/50">No apps yet</span>
          ) : (
            visibleTargets.map((target) =>
              target.kind === "app" ? (
                <HoverTooltip
                  key={`app:${target.processName}`}
                  content={target.label}
                  side="bottom-right"
                  width="w-auto"
                >
                  <AppIcon
                    app={{
                      display_name: target.label,
                      process_name: target.processName,
                      icon: target.icon,
                    }}
                  />
                </HoverTooltip>
              ) : (
                <HoverTooltip
                  key={`site:${target.domain}`}
                  content={target.domain}
                  side="bottom-right"
                  width="w-auto"
                >
                  <SiteGlyph icon={target.icon} />
                </HoverTooltip>
              )
            )
          )}
          {overflowCount > 0 && (
            <span
              className="inline-flex items-center h-[18px] px-1.5 rounded-md text-[10px] font-medium text-steel"
              style={{ background: "var(--surface-row-hover)" }}
              aria-label={`${overflowCount} more`}
            >
              +{overflowCount}
            </span>
          )}
        </div>

        {/* Always-faintly-visible chevron (replaces the hover edit pencil). */}
        <svg
          width="14"
          height="14"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
          className="shrink-0 text-steel/40 group-hover/row:text-steel transition-colors duration-200"
        >
          <path d="M4.5 3l3 3-3 3" />
        </svg>
      </button>
    </motion.div>
  );
}
