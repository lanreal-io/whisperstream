import { createContext } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import type { AgentConversation, AgentName } from "../../lib/socket/agentEvents";
import { agentProviderLabel } from "../../lib/sessionName";
import { MarkdownText } from "../../pages/hud/components/MarkdownText";
import { ReplySentGlyph } from "../../pages/hud/components/ReplyRail";
import { AgentMark } from "../../pages/hud/components/AgentIdentity";

export const AgentConversationContext = createContext<AgentConversation | null>(null);

/** What happened to the user's newest reply: held for the agent's next Stop,
 *  or delivered while the agent works. */
export type AgentReplyStatus = "held" | "sent";

/** Locked copy (copy-review.md). */
export function agentReplyStatusCopy(status: AgentReplyStatus, provider: AgentName): string {
  const agent = agentProviderLabel(provider);
  return status === "held" ? `Sends when ${agent} finishes` : `Sent. Waiting for ${agent}`;
}

/** Locked copy (copy-review.md #3). */
export const AGENT_WORKING_COPY = "Working";

/** Four content-free bars: the agent is busy. Same idiom as the identity
 *  row's speaking cluster, not VoiceBars (which means "you are heard").
 *  Transform only, and only while the row is shown; still under reduced
 *  motion. */
function WorkingBars() {
  const reduced = useReducedMotion();
  return (
    <span aria-hidden="true" className="flex items-center" style={{ gap: 2, height: 14 }}>
      {[0, 1, 2, 3].map((index) => (
        // CSS, not a Framer keyframe loop: the HUD mounts this under
        // AnimatePresence initial={false}, which blocks a Framer mount
        // animation and left the bars frozen.
        <span
          key={index}
          data-working-bar=""
          style={{
            width: 2.5,
            height: 12,
            borderRadius: 2,
            background: "var(--frost-muted)",
            transformOrigin: "center",
            transform: reduced ? "scaleY(0.6)" : undefined,
            animation: reduced
              ? undefined
              : `agent-working-wave 0.9s ${index * 0.11}s ease-in-out infinite both`,
          }}
        />
      ))}
    </span>
  );
}

/** A held reply has not gone anywhere yet, so it gets a waiting mark rather
 *  than the sent check. */
function HeldGlyph() {
  return (
    <svg width="13" height="13" viewBox="0 0 14 14" aria-hidden="true" className="shrink-0">
      <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="1.4" />
      <path d="M7 4.4V7l1.8 1.2" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ReplyStatusLine({ status, provider }: { status: AgentReplyStatus; provider: AgentName }) {
  return (
    <div
      role="status"
      data-testid="agent-reply-status"
      data-status={status}
      className="flex items-center"
      style={{
        gap: 6,
        marginTop: 8,
        fontFamily: "var(--font-agent)",
        fontSize: 11.5,
        fontWeight: 500,
        color: "var(--hud-accent-text, var(--color-steel))",
      }}
    >
      {status === "held" ? <HeldGlyph /> : <ReplySentGlyph size={13} />}
      <span>{agentReplyStatusCopy(status, provider)}</span>
    </div>
  );
}

/** Locked by design.md (Variant B): the newest pane's label. */
export const AGENT_LATEST_TURN_COPY = "Latest";

/** Variant B, frost panes (design.md, accepted 2026-10-22). Every turn is a
 *  frosted pane with the HUD's specular rim; your replies are tinted and
 *  inset 56px; the newest turn, of either role, carries the frost spine, a
 *  cool wash, an accent edge and "Latest". */
const PANE_RADIUS = 14;
const USER_PANE_INSET = 56;
const TURN_GAP = 14;

function TurnPane({
  role,
  provider,
  latest,
  text,
  replyStatus,
}: {
  role: "user" | "assistant";
  provider: AgentName;
  latest: boolean;
  text: string;
  replyStatus: AgentReplyStatus | null;
}) {
  const user = role === "user";
  return (
    <>
      {latest && (
        <span
          aria-hidden="true"
          data-testid="agent-turn-spine"
          style={{
            position: "absolute",
            left: 0,
            top: 0,
            bottom: 0,
            width: 2,
            background: "var(--hud-frost-deep, var(--color-frost-deep))",
          }}
        />
      )}
      <div
        className="flex items-center"
        style={{
          gap: 6,
          marginBottom: 6,
          fontFamily: "var(--font-agent)",
          fontSize: 11,
          fontWeight: 500,
          color: latest ? "var(--hud-accent-text, var(--color-steel))" : "var(--color-steel)",
        }}
      >
        {!user && (
          <span aria-hidden="true" className="flex shrink-0">
            <AgentMark provider={provider} size={14} />
          </span>
        )}
        <span>{user ? "You" : agentProviderLabel(provider)}</span>
        {latest && (
          <span
            data-testid="agent-turn-latest"
            style={{
              marginLeft: "auto",
              fontSize: 10,
              fontWeight: 600,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              color: "var(--hud-accent-text, var(--color-steel))",
            }}
          >
            {AGENT_LATEST_TURN_COPY}
          </span>
        )}
      </div>
      <MarkdownText text={text} />
      {user && replyStatus !== null && (
        <ReplyStatusLine status={replyStatus} provider={provider} />
      )}
    </>
  );
}

export function AgentTimeline({
  conversation,
  provider,
  needsInput = false,
  replyStatus = null,
}: {
  conversation: AgentConversation;
  provider: AgentName;
  needsInput?: boolean;
  /** Shown under the newest turn when that turn is the user's reply. */
  replyStatus?: AgentReplyStatus | null;
}) {
  const reduced = useReducedMotion();
  const newestIndex = conversation.turns.length - 1;
  const working = conversation.activity === "working" && !needsInput;
  return (
    <div
      className="flex flex-col"
      style={{ gap: TURN_GAP, paddingBottom: 4 }}
      data-testid="agent-timeline"
    >
      {conversation.turns.length === 0 && (
        <p className="text-[12px] text-steel">Replies appear here when your agent finishes.</p>
      )}
      <AnimatePresence initial={false}>
        {conversation.turns.map((turn, index) => {
          const latest = index === newestIndex;
          const user = turn.role === "user";
          return (
            <motion.article
              key={turn.id}
              data-agent-turn={turn.role}
              data-newest-turn={latest ? "true" : undefined}
              // A new pane fades in; its height is geometry, so it does not
              // animate.
              initial={reduced ? false : { opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ duration: reduced ? 0 : 0.15 }}
              style={{
                position: "relative",
                overflow: "hidden",
                marginLeft: user ? USER_PANE_INSET : 0,
                borderRadius: PANE_RADIUS,
                padding: "11px 14px 12px",
                border: `1px solid ${latest ? "var(--border-accent)" : "var(--border-hairline)"}`,
                background: latest
                  ? "linear-gradient(100deg, var(--hud-wash), transparent 70%), var(--hud-pane)"
                  : user
                    ? "var(--hud-user-tint)"
                    : "var(--hud-pane)",
                boxShadow: "inset 0 1px 0 var(--hud-pane-rim)",
              }}
            >
              <TurnPane
                role={turn.role}
                provider={provider}
                latest={latest}
                text={turn.text}
                replyStatus={latest ? replyStatus : null}
              />
            </motion.article>
          );
        })}
      </AnimatePresence>
      {working && (
        <div
          role="status"
          data-testid="agent-working-row"
          className="flex flex-col"
          style={{
            gap: 6,
            padding: "10px 12px",
            borderRadius: PANE_RADIUS,
            border: "1px dashed var(--border-accent)",
            background: "var(--hud-pane-recessed, var(--surface-control))",
          }}
        >
          <div
            className="flex items-center"
            style={{
              gap: 8,
              fontFamily: "var(--font-agent)",
              fontSize: 12,
              fontWeight: 500,
              color: "var(--hud-accent-text, var(--color-steel))",
            }}
          >
            <WorkingBars />
            <span>{AGENT_WORKING_COPY}</span>
          </div>
        </div>
      )}
    </div>
  );
}
