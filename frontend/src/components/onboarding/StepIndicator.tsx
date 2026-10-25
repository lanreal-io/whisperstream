import { motion } from "framer-motion";

interface StepIndicatorProps {
  current: number;
  total: number;
}

// A platform-derived number of bars at the top of the overlay. Active bar widens 32 → 48px with a 320ms
// tween (slightly longer than step transitions so the indicator feels like
// an unhurried progress arc rather than a UI response). Completed bars keep
// the frost gradient but fade to opacity-50; upcoming bars are a faint white.
export function StepIndicator({ current, total }: StepIndicatorProps) {
  return (
    <div className="flex flex-col items-center gap-2">
      <div className="flex items-center gap-2">
        {Array.from({ length: total }).map((_, i) => {
          const idx = i + 1;
          const isActive = idx === current;
          const isComplete = idx < current;
          return (
            <motion.div
              key={idx}
              animate={{ width: isActive ? 48 : 32 }}
              transition={{ duration: 0.32, ease: [0.2, 0, 0, 1] }}
              className="h-1 rounded-full overflow-hidden"
              style={{
                background:
                  isActive || isComplete
                    ? "var(--accent-fill)"
                    : // Upcoming bars are a steel tint, NOT a white literal.
                      // `rgba(255,255,255,0.4)` composites to literal #FFFFFF
                      // over `--surface-modal` in Light (the setup flow's
                      // ground) and to near-canvas over the onboarding mesh,
                      // so the bar disappeared entirely. Steel resolves
                      // #607C97 in Light and #98AFC5 in Dark, so one value
                      // draws the bar on both grounds. Same token as
                      // `SetupStatusDot`'s idle state.
                      "color-mix(in srgb, var(--color-steel) 50%, transparent)",
                opacity: isComplete ? 0.5 : 1,
                boxShadow: isActive
                  ? "var(--shadow-frost-glow)"
                  : "none",
              }}
            />
          );
        })}
      </div>
      <div className="text-[10px] tracking-[0.2em] uppercase text-steel/40 font-medium">
        Step {current} of {total}
      </div>
    </div>
  );
}
