import type { CSSProperties } from "react";

// Shared password/recovery field surface. Callers retain validation, icons,
// focus handlers and lifetime; the global frost outline owns focus appearance.
// The resting edge is the themed steel hairline: a white-alpha border vanishes
// on the near-white modal card in light. Blur handlers that restore the resting
// look must use passwordInputRestingBorderColor.
export const passwordInputRestingBorderColor = "var(--border-hairline-steel)";
export const passwordInputClassName =
  "w-full h-11 rounded-full px-5 text-[13px] font-body text-slate placeholder:text-steel/40 outline-none transition-all duration-300";
export const passwordInputStyle: CSSProperties = {
  background: "var(--surface-control)",
  border: `1px solid ${passwordInputRestingBorderColor}`,
  boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
};
