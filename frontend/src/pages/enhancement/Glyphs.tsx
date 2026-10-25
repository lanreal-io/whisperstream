import { default as brandMarkUrl } from "../../assets/logo.png";
import { providerDataTooltip, providerStaysLocal } from "./providerMetadata";
import { HoverTooltip } from "../../components/HoverTooltip";


/* CloudMiniIcon: 12x12 decorative cloud silhouette used to signal
   cloud / off-device data flow. Single closed path; rounded line joins
   match the rest of the 12px sidebar mini-icon family.
   Accepts standard SVG props via spread so
   the parent can pass aria-hidden="true" to mark it decorative. */
function CloudMiniIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 12 12"
      fill="none"
      width="12"
      height="12"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M2.5 9.5 L9.5 9.5 C 10.6 9.5 11 8.7 11 8 C 11 7.1 10.3 6.4 9.3 6.5 C 9.1 5.1 7.9 4 6.5 4 C 5.3 4 4.3 4.8 4 5.9 C 3.1 5.7 2 6.4 2 7.6 C 2 8.5 2.5 9.5 2.5 9.5 Z" />
    </svg>
  );
}

/* DownloadMiniIcon: 12x12 decorative tray-plus-down-arrow silhouette used to
   signal "fetch the local model to disk." Single closed-shape silhouette
   drawn as one <path> with subpaths so the arrow shaft, chevron, and tray
   share a stroke. Matches the rest of the 12px sidebar mini-icon family
   (stroke 1.5, currentColor, rounded line joins) and lives next to the trash
   glyph + CloudMiniIcon in the Local row of the provider dropdown.
   "Tray + down arrow" chosen over "cloud + down arrow" so the Local row's
   action affordance does not read as a cloud-download semantic. Accepts
   standard SVG props via spread so the parent can pass aria-hidden="true"
   to mark it decorative. */
export function DownloadMiniIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 12 12"
      fill="none"
      width="12"
      height="12"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M6 1.75 L6 7.25 M3.75 5 L6 7.25 L8.25 5 M2.25 7 L2.25 9.75 L9.75 9.75 L9.75 7" />
    </svg>
  );
}

/* ShieldMiniIcon: 12x12 decorative shield silhouette signalling "stays on your
   device." Sibling to CloudMiniIcon (same 1.5 stroke / currentColor / round
   joins); used on the provider selector for managed Local, carrying the
   data-handling tooltip (round 04). Accepts standard SVG props via spread. */
function ShieldMiniIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 12 12"
      fill="none"
      width="12"
      height="12"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      <path d="M6 1.5 L10 3 V6.2 C10 8.6 8.3 10.2 6 10.8 C3.7 10.2 2 8.6 2 6.2 V3 Z" />
    </svg>
  );
}

/* MarginalDot: signal-yellow caution indicator for a local model whose cached
 * GPU benchmark verdict is "marginal" (tiered-benchmark Task 3.1, copy-review.md
 * Locked Copy Decisions "gold indicator"; recolored off the mustard gold onto
 * --color-caution in round 01 Issue 4). The dot itself is aria-hidden
 * (purely visual, color is otherwise color-only); the accessible label rides
 * on a paired `sr-only` span so the meaning never depends on seeing the
 * color. Sits after the model name/badge in DOM order, so it reads as
 * "Stream-1, Runs slowly here" to a screen reader (copy-review.md
 * Accessibility Notes). Rendered via the Dropdown option's `adornment`
 * alongside `StateGlyph`, same as StateGlyph's install-state dot. */
export function MarginalDot() {
  return (
    <>
      <span
        aria-hidden="true"
        className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
        style={{ background: "var(--color-caution)" }}
      />
      <span className="sr-only">Runs slowly here</span>
    </>
  );
}

/* ModelIdentityChip: the local model picker's leading identity seat (design.md
 * "Signature"). The same 18px frost chip (the shared Badge frost recipe: a
 * color-mix tint + rim + caught-light inset) distinguishes the two choices by
 * meaning: the Whisperstream mark means a fixed, trained first-party model
 * (Stream-1 Mini); a pen means you write the prompt (Customizable). Always
 * visible, so rows never need a blank spacer. Purely decorative
 * (aria-hidden): the row label already names the model. `dimmed` softens it on
 * a disabled (too slow) row. */
export function ModelIdentityChip({
  kind,
  dimmed = false,
}: {
  kind: "mark" | "pen";
  dimmed?: boolean;
}) {
  return (
    <span
      className="inline-flex h-[18px] w-[18px] items-center justify-center shrink-0 rounded-[5px]"
      style={{
        background: "color-mix(in srgb, var(--color-frost) 15%, transparent)",
        border: "1px solid color-mix(in srgb, var(--color-frost) 30%, transparent)",
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
        color: "var(--color-frost-deep)",
        opacity: dimmed ? 0.55 : undefined,
      }}
      aria-hidden="true"
      data-model-chip={kind}
    >
      {kind === "mark" ? (
        <img
          src={brandMarkUrl}
          alt=""
          draggable={false}
          className="h-3 w-3 object-contain select-none pointer-events-none"
          style={{ filter: "drop-shadow(0 1px 2px rgba(120, 160, 200, 0.18))" }}
        />
      ) : (
        <svg
          width="11"
          height="11"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M7.6 2.4l2 2L4.3 9.7 2 10l.3-2.3z" />
          <path d="M6.6 3.4l2 2" />
        </svg>
      )}
    </span>
  );
}

/* ProviderDataGlyph: the single source of truth for a provider's data-handling
   indicator. Shield for the managed Local provider, Cloud when the
   transcript goes off-device, carrying providerDataTooltip as a hover bubble.
   Rendered identically by the dropdown trigger AND every dropdown row so the two
   can't drift (they previously did: the trigger had the tooltip, the rows showed a
   bare cloud icon for cloud-only providers and nothing for local / Ollama). When a
   host row uses group-hover for its own affordances, give that row a NAMED group
   (e.g. `group/row`) so this tooltip's own unnamed `group` fires on icon hover
   only, not on whole-row hover. */
export function ProviderDataGlyph({
  value,
  customHost,
  ollamaEndpoint,
  className = "",
}: {
  value: string;
  customHost?: string;
  ollamaEndpoint?: string;
  className?: string;
}) {
  const tooltip = providerDataTooltip(value, customHost, ollamaEndpoint);
  const staysLocal = providerStaysLocal(value, ollamaEndpoint);
  const Icon = staysLocal ? ShieldMiniIcon : CloudMiniIcon;
  return (
    <HoverTooltip
      content={tooltip}
      side="bottom-left"
      width="w-56"
      className={className}
    >
      <Icon
        aria-hidden="true"
        data-provider-icon={staysLocal ? "local" : "remote"}
        className="text-steel/70 cursor-help"
      />
    </HoverTooltip>
  );
}

/* ---------- Plus glyph (add-profile trigger) ---------- */
export const PlusIcon = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M7 3v8M3 7h8" />
  </svg>
);
