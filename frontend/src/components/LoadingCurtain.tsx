import type { ReactNode } from "react";
import { motion } from "framer-motion";

const meshGradient = `
  radial-gradient(ellipse at 20% 50%, var(--frost-mesh-1) 0%, transparent 50%),
  radial-gradient(ellipse at 80% 20%, var(--frost-mesh-2) 0%, transparent 45%),
  radial-gradient(ellipse at 45% 80%, var(--frost-mesh-3) 0%, transparent 50%),
  var(--color-canvas)
`;

export function LoadingCurtain({ label, children }: {
  label: string;
  children?: ReactNode;
}) {
  return (
    <div
      data-testid="loading-curtain"
      className="w-full min-h-[100dvh] flex items-center justify-center"
      style={{
        background: meshGradient,
        backgroundSize: "200% 200%",
        animation: "mesh-drift 30s ease-in-out infinite",
      }}
    >
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="text-center"
      >
        <div className="font-display text-lg font-light tracking-[0.12em] text-steel mb-3">
          whisperstream
        </div>
        <div className="text-[12px] text-steel/60">{label}</div>
        {children ?? <div
          className="mt-4 w-32 h-0.5 mx-auto rounded-full overflow-hidden"
          style={{ background: "var(--frost-mesh-1)" }}
        >
          <motion.div
            className="h-full w-1/3 rounded-full"
            style={{
              background:
                "linear-gradient(90deg, transparent, var(--color-frost), transparent)",
            }}
            animate={{ x: ["-100%", "400%"] }}
            transition={{
              duration: 1.5,
              repeat: Infinity,
              ease: "easeInOut",
            }}
          />
        </div>}
      </motion.div>
    </div>
  );
}

