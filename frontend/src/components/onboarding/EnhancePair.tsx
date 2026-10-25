import type { CSSProperties, ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";

// EnhancePair: the onboarding Enhance step's one recurring object, in the
// "Split" direction (round 01 design.md, prototype-preview-pair.html dir=3).
// One glass surface holds two columns: a recessed left column for what you
// say, and a right column for what you get, whose label carries the arrow.
// No element sits on the seam. The intro carousel cards and try-it share it,
// so "this became that" reads the same way everywhere.
//
// Try-it starts with only the left column (full width, the script to read)
// and splits when enhancement starts. An email example adds an address strip
// that spans both columns.
//
// Results are never diffed. Struck words exist only for the canned intro
// preview, through `segments`; live results arrive as plain text.

export type SpokenSegment = { text: string; dropped?: boolean };

export interface Spoken {
  label: string;
  /** "script" is text to read aloud (slate); "example" is a canned intro
   *  input and "said" the user's raw transcript (both steel: the input). */
  tone: "script" | "example" | "said";
  text?: string;
  /** Canned intro preview only: marks dropped words with a strike. */
  segments?: SpokenSegment[];
}

export type Written =
  | { variant: "expected"; label: string; text: string }
  | { variant: "result"; label: string; text: string }
  | { variant: "pending"; label: string }
  | {
      variant: "missing";
      label: string;
      note: string;
      retryLabel: string;
      onRetry: () => void;
    };

const labelClass =
  "mb-[3px] flex items-center gap-[5px] text-[10px] font-medium uppercase tracking-[0.14em] text-steel";

// Frost text for small accent copy: deep frost on light glass, the lighter
// frost on dark glass (deep frost is too dim on #0C141D).
const accentText = "text-frost-deep [[data-theme=dark]_&]:text-frost";

const RIM = "inset 0 1px 0 color-mix(in srgb, var(--rim-specular) 70%, transparent)";

export function EnhancePair({
  spoken,
  written,
  email = false,
  size = "try",
  className = "",
}: {
  spoken: Spoken;
  /** Omitted until enhancement starts: the surface stays one column. */
  written?: Written;
  /** Adds the address strip ("Email apps only") across both columns. */
  email?: boolean;
  /** "intro" is the carousel's 13px text; "try" the try-it step's 14px. */
  size?: "intro" | "try";
  className?: string;
}) {
  const result = written?.variant === "result";
  const textSize = size === "intro" ? "text-[13px]" : "text-[14px]";
  const split = Boolean(written);

  return (
    <div
      className={`relative overflow-hidden rounded-[var(--radius-row)] ${className}`}
      style={{
        background: result ? "var(--surface-hover)" : "var(--surface-control)",
        border: `1px solid ${result ? "var(--border-accent)" : "var(--border-hairline)"}`,
        boxShadow: RIM,
      }}
      data-enhance-pair
      data-split={split || undefined}
    >
      {email ? <AddressStrip /> : null}
      <div
        className="grid"
        style={{
          gridTemplateColumns: split ? "minmax(0, 1fr) minmax(0, 1.14fr)" : "minmax(0, 1fr)",
        }}
      >
        <SpokenColumn spoken={spoken} split={split} textSize={textSize} />
        {written ? <WrittenColumn written={written} textSize={textSize} loose={email} /> : null}
      </div>
    </div>
  );
}

function SpokenColumn({
  spoken,
  split,
  textSize,
}: {
  spoken: Spoken;
  split: boolean;
  textSize: string;
}) {
  const body = spoken.segments
    ? spoken.segments.map((seg, i) =>
        seg.dropped ? (
          <del
            key={i}
            className="no-underline line-through text-steel/55"
            style={{
              textDecorationColor:
                "color-mix(in srgb, var(--color-frost-deep) 60%, transparent)",
            }}
          >
            {seg.text}
          </del>
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )
    : spoken.text;

  const recessed: CSSProperties = split
    ? {
        background: "color-mix(in srgb, var(--color-steel) 5%, transparent)",
        borderRight: "1px solid color-mix(in srgb, var(--color-steel) 14%, transparent)",
      }
    : {};

  return (
    <section
      aria-label={spoken.label}
      className="pt-2.5 pr-3.5 pb-[11px] pl-4"
      style={recessed}
      data-slot="spoken"
    >
      <span className={labelClass} aria-hidden="true">{spoken.label}</span>
      <p
        className={`${textSize} leading-normal ${spoken.tone === "script" ? "text-slate" : "text-steel"}`}
        style={{ fontFamily: "var(--font-quote)" }}
      >
        {spoken.tone === "said" ? body : <q className="[quotes:none]">{body}</q>}
      </p>
    </section>
  );
}

function ArrowRight() {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"
      strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
      className="shrink-0 text-frost-deep">
      <path d="M1.5 5h7M5.8 2.3 8.5 5 5.8 7.7" />
    </svg>
  );
}

function WrittenColumn({
  written,
  textSize,
  loose,
}: {
  written: Written;
  textSize: string;
  loose: boolean;
}) {
  const pending = written.variant === "pending";
  const label = (
    <span className={labelClass} aria-hidden="true">
      <ArrowRight />
      {written.label}
    </span>
  );

  if (written.variant === "missing") {
    // A visible gap rather than a disguised result: the column keeps its
    // place and carries the note with an inline retry.
    return (
      <div
        className="relative pt-2.5 pr-4 pb-[11px] pl-3.5"
        data-slot="written"
        data-written-variant="missing"
      >
        {label}
        <p className="text-[12px] leading-normal text-steel">{written.note}</p>
        <button
          onClick={written.onRetry}
          className={`mt-1 whitespace-nowrap text-[12px] font-medium hover:underline cursor-pointer ${accentText}`}
        >
          {written.retryLabel}
        </button>
      </div>
    );
  }

  return (
    <section
      aria-label={written.label}
      aria-busy={pending || undefined}
      className="relative pt-2.5 pr-4 pb-[11px] pl-3.5"
      data-slot="written"
      data-written-variant={written.variant}
    >
      {written.variant === "result" && (
        <span
          aria-hidden="true"
          className="absolute right-3.5 top-[13px] h-[7px] w-[7px] rounded-full bg-mint"
          style={{ boxShadow: "0 0 8px color-mix(in srgb, var(--color-mint) 55%, transparent)" }}
        />
      )}
      {label}
      {written.variant === "expected" ? (
        <WrittenText text={written.text} textSize={textSize} loose={loose} />
      ) : written.variant === "result" ? (
        <p className={`${textSize} leading-normal whitespace-pre-line text-slate font-medium`}>
          {written.text}
        </p>
      ) : null}
      {pending ? <Shimmer /> : null}
    </section>
  );
}

const LIST_ITEM = /^\d+\.\s+/;

/** Renders canned expected text: "\n\n" separates paragraphs, "\n" lines,
 *  and consecutive lines starting "1. " become a real ordered list. */
export function WrittenText({
  text,
  textSize,
  loose = false,
}: {
  text: string;
  textSize: string;
  /** Email bodies breathe a little more: 1.55 line height, 6px paragraphs. */
  loose?: boolean;
}) {
  const blocks = text.split("\n\n");
  const pClass = `${textSize} ${loose ? "leading-[1.55]" : "leading-normal"} text-slate`;
  return (
    <div className={`flex flex-col ${loose ? "gap-1.5" : "gap-1"}`}>
      {blocks.map((block, b) => {
        const lines = block.split("\n");
        const out: ReactNode[] = [];
        let plain: string[] = [];
        let items: string[] = [];
        const flushPlain = () => {
          if (!plain.length) return;
          const ls = plain;
          out.push(
            <p key={`p${out.length}`} className={pClass}>
              {ls.map((l, i) => (
                <span key={i}>
                  {i > 0 ? <br /> : null}
                  {l}
                </span>
              ))}
            </p>,
          );
          plain = [];
        };
        const flushItems = () => {
          if (!items.length) return;
          out.push(
            <ol key={`o${out.length}`} className="mt-0.5 flex flex-col">
              {items.map((item, i) => (
                <li key={i} className={`${pClass} relative pl-[18px]`}>
                  <span
                    aria-hidden="true"
                    className="absolute left-0 tabular-nums text-frost-deep"
                  >
                    {i + 1}.
                  </span>
                  {item}
                </li>
              ))}
            </ol>,
          );
          items = [];
        };
        for (const line of lines) {
          if (LIST_ITEM.test(line)) {
            flushPlain();
            items.push(line.replace(LIST_ITEM, ""));
          } else {
            flushItems();
            plain.push(line);
          }
        }
        flushPlain();
        flushItems();
        return <div key={b}>{out}</div>;
      })}
    </div>
  );
}

function AddressStrip() {
  // Flush chrome across both columns: reads as "in an email app, this
  // becomes this". Decorative; the slide's aria-describedby carries the cue.
  return (
    <div
      aria-hidden="true"
      className="flex h-[26px] items-center justify-between gap-2 pr-[5px] pl-3.5"
      style={{
        background: "color-mix(in srgb, var(--color-frost) 7%, transparent)",
        borderBottom: "1px solid var(--border-accent)",
      }}
      data-address-strip
    >
      <span className="flex items-center gap-1.5 text-[10.5px] tracking-[0.01em] text-steel">
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor"
          strokeWidth="1.1" className="text-steel/60">
          <rect x="2" y="4.5" width="6" height="4" rx="1" />
          <path d="M3.4 4.5V3.3a1.6 1.6 0 0 1 3.2 0v1.2" />
        </svg>
        mail.example.com
      </span>
      <span
        className={`inline-flex shrink-0 items-center gap-[5px] whitespace-nowrap rounded-full py-px pr-2 pl-[7px] text-[10px] font-medium tracking-[0.02em] ${accentText}`}
        style={{
          background: "color-mix(in srgb, var(--color-frost) 12%, transparent)",
          border: "1px solid var(--border-accent)",
        }}
      >
        <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor"
          strokeWidth="1.2" strokeLinejoin="round">
          <rect x="1.5" y="2.5" width="9" height="7" rx="1.5" />
          <path d="m2 3.4 4 3 4-3" />
        </svg>
        Email apps only
      </span>
    </div>
  );
}

function Shimmer(): ReactNode {
  const reduce = useReducedMotion();
  return (
    <div aria-hidden="true" className="pointer-events-none absolute inset-0 overflow-hidden">
      <motion.div
        className="absolute inset-y-0 w-2/5"
        style={{
          background: "linear-gradient(90deg, transparent, var(--frost-mesh-1), transparent)",
        }}
        initial={reduce ? { x: "75%" } : undefined}
        animate={reduce ? { x: "75%" } : { x: ["-100%", "260%"] }}
        transition={reduce ? { duration: 0 } : { duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
      />
    </div>
  );
}
