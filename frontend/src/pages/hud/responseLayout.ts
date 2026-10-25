

// --- The expanded Claude card (Task 2.2, design.md states 7 and 8) ---
// Layout order is FIXED and load-bearing: identity, body well, divider
// (console only), rail, footer. The body well is the ONLY elastic element: the rail and the footer
// are measured first and always get their space, and the well takes whatever
// the card's ceiling leaves after them. That ordering is what makes the reply
// rail structurally impossible to obscure (feedback item 3) — it is not solved
// with padding or a z-index, and re-ordering these rows re-opens the defect.
//
// Nothing is ever cut and body type never shrinks to fit (owner resolution 1):
// the card takes its natural height, and scrolling happens only past the
// ceiling.
export const CLAUDE_CARD_PAD_TOP = 20;
export const CLAUDE_CARD_PAD_X = 22;
export const CLAUDE_CARD_PAD_BOTTOM = 16;
export const CLAUDE_CARD_GAP_IDENTITY_BODY = 16;
export const CLAUDE_CARD_GAP_BODY_DIVIDER = 18;
export const CLAUDE_CARD_DIVIDER_H = 1;
export const CLAUDE_CARD_GAP_DIVIDER_RAIL = 14;
/** The glance card has no divider (owner, 2026-10-24): the gap and the
 *  dimmer hint line already separate the agent's words from the reply row. */
export const CLAUDE_CARD_GAP_BODY_RAIL = 12;
export const CLAUDE_CARD_GAP_RAIL_FOOTER = 10;
/** The spine's 2px rule plus its 12px gap (design decision 10). */
export const CLAUDE_BODY_GUTTER = 14;
/** Bottom-fade height, reused from ReadingWell's behavior (not its component:
 *  the spine has to live inside the well's scrolled content box). */
export const CLAUDE_WELL_FADE_H = 24;
/** The well never collapses to nothing, even at the ceiling with a four-line
 *  reply field open. */
export const CLAUDE_WELL_MIN_H = 44;
/** Content enters behind the geometry so it never paints outside the growing
 *  region (design decision 28). */
export const CLAUDE_CARD_CONTENT_DELAY_S = 0.06;
/** Top fade once the well is scrolled away from its start (agent-speak-fixes
 *  polish audit), slightly taller than the bottom fade it pairs with. */
export const CLAUDE_WELL_TOP_FADE_H = 28;
/** The timeline's right inset clear of the scrollbar, and the breathing room
 *  kept above the newest turn when the well scrolls to it. */
export const CLAUDE_TIMELINE_SCROLL_GAP = 6;
