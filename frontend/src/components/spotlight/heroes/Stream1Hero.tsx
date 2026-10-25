import { motion, useReducedMotion } from "framer-motion";

/* Stream-1 hero: the Whisperstream logo mark itself (the frost DNA/infinity
   ribbon), gently breathing over a soft frost glow and flanked by two small
   frost satellite sparks - the same centered brand-mark idiom as the Gemini
   hero, but carrying our own identity (these are Whisperstream's own fine-tuned
   local models). The mark is an inline vector filled with a theme-token frost
   gradient, so it recolors correctly in Light and Dark. The same path ships as
   the portable master asset at frontend/src/assets/logo.svg. Animated by
   default; a static composition under reduced motion. */

// Flattened single-path silhouette of the Whisperstream mark (viewBox 550.64 x 307.24).
const MARK_D =
  "M148.953,214.937C141.632,228.780 133.339,242.529 122.696,255.933C100.223,284.235 72.241,300.247 37.178,300.946C31.345,301.062 26.050,298.740 25.149,298.344C23.442,297.595 13.641,293.294 8.907,281.655C8.183,279.877 4.000,269.590 9.231,257.684C14.280,246.196 24.195,242.146 25.920,241.441C32.434,238.781 37.738,239.128 38.779,239.196C54.016,240.192 67.701,234.415 83.527,216.341C87.329,211.998 92.106,205.047 92.804,204.031C102.599,189.775 111.885,172.347 119.739,156.794C116.405,150.367 111.212,140.500 107.101,133.310C91.531,106.084 82.151,96.506 76.663,91.406C61.880,77.665 49.497,75.436 37.930,76.295C35.866,76.107 33.773,76.122 31.737,75.732C29.901,75.381 19.417,73.373 12.160,62.951C4.765,52.331 6.821,41.473 7.177,39.595C7.525,37.758 9.510,27.273 19.940,20.018C28.228,14.253 36.507,14.467 38.019,14.506C89.297,15.834 116.769,49.977 130.730,69.493C137.215,78.558 142.971,88.132 148.462,98.282C151.279,92.944 154.280,87.531 157.399,82.333C173.561,55.408 190.058,38.215 209.312,26.698C217.701,21.681 226.659,18.608 228.233,18.068C240.117,13.991 276.616,4.000 317.879,25.822C355.331,45.627 377.561,80.169 416.136,103.104C420.955,105.968 427.266,108.808 428.258,109.255C448.359,118.300 469.284,122.123 491.703,122.346C496.585,122.096 501.621,121.970 506.813,121.983C508.125,121.910 509.441,121.826 510.764,121.732C516.692,121.311 519.504,121.718 520.256,121.826C522.105,122.095 532.666,123.622 540.368,133.727C544.116,138.645 545.411,143.557 545.655,144.483L546.012,146.724C546.227,147.706 546.326,148.404 546.369,148.701L546.384,149.053L546.638,150.646C546.595,151.640 546.597,152.637 546.580,153.633L546.636,154.936C546.305,156.987 546.176,159.083 545.644,161.092C545.165,162.899 542.432,173.203 531.504,179.693C523.379,184.518 515.438,184.065 513.998,183.983C511.866,183.862 509.511,183.718 506.955,183.588C504.444,183.591 501.546,183.558 498.198,183.475C496.008,183.420 493.852,183.342 491.727,183.240C472.387,183.465 447.120,186.644 420.890,201.130C397.538,214.026 375.811,237.222 359.294,252.910C336.565,274.499 303.633,303.235 252.850,298.416C224.390,295.715 206.413,283.137 200.103,278.571C177.136,261.949 161.308,237.875 148.953,214.937ZM388.586,153.468C386.356,152.141 384.318,150.880 382.458,149.713C334.502,119.625 309.548,78.150 275.501,71.215C268.533,69.795 259.348,69.478 251.317,71.359C248.952,71.913 243.262,73.232 234.756,78.427C209.009,94.154 187.851,135.519 177.020,156.544C178.022,158.532 184.814,171.783 191.696,183.291C201.384,199.493 218.196,225.378 239.125,235.432C240.131,235.916 253.203,243.510 272.592,240.427C301.631,235.810 323.021,205.277 358.012,175.738C358.986,174.916 371.223,164.236 388.586,153.468ZM37.930,76.295L37.954,76.296L37.895,76.298L37.930,76.295Z";

const breathe = {
  animate: { opacity: [0.85, 1, 0.85], scale: [0.965, 1, 0.965] },
  transition: { duration: 3.6, repeat: Infinity, ease: "easeInOut" as const },
};

const spark = {
  animate: { opacity: [0.35, 0.9, 0.35], scale: [0.8, 1, 0.8] },
  transition: { duration: 3.6, repeat: Infinity, ease: "easeInOut" as const, delay: 1.2 },
};

export function Stream1Hero() {
  const reduce = useReducedMotion() ?? false;
  return (
    <div
      className="relative flex h-[120px] w-[172px] items-center justify-center"
      aria-hidden="true"
    >
      {/* Frost satellite sparks (theme-token fills, no raw literals). */}
      <svg
        width="172"
        height="120"
        viewBox="0 0 172 120"
        fill="none"
        className="pointer-events-none absolute inset-0"
      >
        <motion.circle
          cx="24"
          cy="32"
          r="3"
          fill="var(--color-frost)"
          style={{ transformOrigin: "24px 32px" }}
          {...(reduce ? {} : spark)}
        />
        <motion.circle
          cx="150"
          cy="90"
          r="2.5"
          fill="var(--color-frost-deep)"
          style={{ transformOrigin: "150px 90px" }}
          {...(reduce
            ? {}
            : { ...spark, transition: { ...spark.transition, delay: 2.4 } })}
        />
      </svg>

      {/* The Whisperstream logo mark - the graphic. */}
      <motion.svg
        width="132"
        height="74"
        viewBox="0 0 550.64 307.24"
        fill="none"
        className="relative"
        style={{
          transformOrigin: "center",
          opacity: reduce ? 0.95 : undefined,
          filter:
            "drop-shadow(0 4px 16px color-mix(in srgb, var(--color-frost-deep) 45%, transparent))",
        }}
        {...(reduce ? {} : breathe)}
      >
        <defs>
          <linearGradient id="spotlight-stream1-mark" x1="0" y1="0" x2="1" y2="0.35">
            <stop offset="0" stopColor="var(--color-frost-light)" />
            <stop offset="0.5" stopColor="var(--color-frost)" />
            <stop offset="1" stopColor="var(--color-frost-deep)" />
          </linearGradient>
        </defs>
        <path d={MARK_D} fill="url(#spotlight-stream1-mark)" />
      </motion.svg>
    </div>
  );
}
