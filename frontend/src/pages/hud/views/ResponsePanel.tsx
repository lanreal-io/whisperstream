import {
  type AgentCompletion,
  type HudSession,
} from "../../../lib/socket/agentEvents";
import {
  type AmbientReplyWindow,
  type AmbientReplyDraft,
  type ReplyNotice,
  type ReplyPhase,
  deriveReplyRailPhase,
} from "../presentation";
import { type MeasuredClaudeCard } from "../geometry";
import { type ConsoleResizeHandlers } from "../useConsoleResize";
import { agentProviderLabel, agentIdentityPhrase } from "../../../lib/sessionName";
import {
  stripSpokenSummaryLine,
  formatReplyTime,
  ClaudeExpandedCard,
} from "./ExpandedResponse";
import { CompactClaudePill } from "./CompactResponse";
import { queueDepthCopy, spineSegmentsFromSpokenSummary } from "../components/AgentIdentity";
import { markdownToPlainText } from "../components/MarkdownText";
import {
  type ReplyShortcuts,
  ReplyHint,
  measurePillHintWidth,
} from "../components/ReplyHint";
import { useMemo } from "react";

export type ReplyState = "idle" | "listening" | "sending" | "sent";

/** The reply row's text and its already-revealed length for one phase. A
 *  capture into a draft that already holds text appends to it (the backend's
 *  `append_dictation`), so live speech shows after the kept text instead of
 *  replacing it: blanking the draft for the capture emptied and shrank the
 *  card, then grew it back when the final landed. */
export function replyRailText(
  phase: string | null,
  draftText: string,
  sentText: string,
  preview: string,
  previewStable: number,
): { text: string; stableCharCount: number; keptText: string } {
  if (phase === "draft" || phase === "sending") {
    return { text: draftText, stableCharCount: 0, keptText: "" };
  }
  if (phase === "sent") return { text: sentText, stableCharCount: 0, keptText: "" };
  if (phase !== "live" && phase !== "processing") {
    return { text: "", stableCharCount: 0, keptText: "" };
  }
  const kept = draftText.trim();
  // Live words reveal on their own; the kept text renders plain before them.
  if (phase === "live") return { text: preview, stableCharCount: previewStable, keptText: kept };
  return {
    text: kept === "" ? preview : preview.trim() === "" ? kept : `${kept} ${preview.trimStart()}`,
    stableCharCount: 0,
    keptText: "",
  };
}

export function ResponsePanel({
  completion,
  remainingCount,
  truncated,
  // Vestigial: every call site passes "idle" (superseded by replyPhase /
  // replyDraft). Kept in the prop contract so existing callers/tests don't
  // need a signature change; not read internally.
  replyState: _replyState,
  rms,
  peak,
  speaking,
  spokenSummary = null,
  markerKnown = false,
  replyWindow = null,
  replyDraft = null,
  canReply = true,
  editingText = "",
  onDraftEdit = () => {},
  onEditingTextChange = () => {},
  onEditingKeyDown = () => {},
  editingInputRef,
  expanded = false,
  showSummaryText = false,
  renderingIntent,
  replyPhase = "passive",
  replyPreview = "",
  replyKeptText = "",
  replyStableCharCount = 0,
  sentReply = null,
  sessions = [],
  activeSessionHandle = null,
  onSelectSession = () => {},
  onCardFocusChange = () => {},
  compactConsole = false,
  consoleResize,
  onToggleExpanded,
  replyAutoSubmit = false,
  replyNotice = null,
  onCopyDraft,
  onMeasuredCardSizeChange,
  replyShortcuts = null,
  replyHintRetired = false,
  glanceGrowFromPill = true,
}: {
  completion: AgentCompletion;
  remainingCount: number;
  truncated: boolean;
  replyState: ReplyState;
  rms: number;
  peak: number;
  speaking: boolean;
  spokenSummary?: string | null;
  markerKnown?: boolean;
  replyWindow?: AmbientReplyWindow | null;
  replyDraft?: AmbientReplyDraft | null;
  canReply?: boolean;
  editingText?: string;
  onDraftEdit?: () => void;
  onEditingTextChange?: (text: string) => void;
  onEditingKeyDown?: (event: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  editingInputRef?: React.RefObject<HTMLTextAreaElement | null>;
  expanded?: boolean;
  showSummaryText?: boolean;
  renderingIntent?: "compact" | "summary" | "full";
  replyPhase?: ReplyPhase;
  replyPreview?: string;
  /** The draft's kept text while a capture appends to it. */
  replyKeptText?: string;
  /** Paired with replyPreview for the live rail's ReconciledPreviewText reveal
   *  (Task 1.5's stableCharCount thread, supplied here per its instruction). */
  replyStableCharCount?: number;
  /** The compact pill's 2,200ms sent confirmation (design.md state 6,
   *  decision 25). Fires in both auto-submit modes; the caller shows it on
   *  the reply owner's correlated receipt for the sent draft. */
  sentReply?: { completionId: string; generation: number; text: string } | null;
  /** Expanded only: the managed-session tab strip (Task 4.4). The compact pill
   *  is unchanged by design; tabs exist only in the expanded card. */
  sessions?: HudSession[];
  activeSessionHandle?: string | null;
  onSelectSession?: (handle: string) => void;
  onCardFocusChange?: (focused: boolean) => void;
  /** The console belongs to a CLI-managed session (editable, keyboard-armed
   *  tab switching). Both kinds of console share the centered geometry. */
  compactConsole?: boolean;
  /** Console only: the resize handles' pointer handlers. */
  consoleResize?: ConsoleResizeHandlers;
  /** The glance card's expand and the console's collapse. */
  onToggleExpanded?: () => void;
  /** `claude_reply_auto_submit`. Off means a spoken reply waits as a draft,
   *  so the rail reserves and shows the submit hint for it. */
  replyAutoSubmit?: boolean;
  /** Why the kept draft cannot be sent from here, or that it is held. */
  replyNotice?: ReplyNotice | null;
  /** Copies the kept draft text. */
  onCopyDraft?: () => void;
  /** Expanded only: reports the painted-card rect into the HUD's one
   *  `measuredCardSize` state (decision 17). */
  onMeasuredCardSizeChange?: (measured: MeasuredClaudeCard) => void;
  /** The bound recording shortcuts the resting reply hints name. */
  replyShortcuts?: ReplyShortcuts | null;
  /** After three dictated replies the pill and glance card drop the hint and
   *  keep only the countdown. The console's field keeps it. */
  replyHintRetired?: boolean;
  /** The glance card grows out of the pill only when it first appears, not
   *  when it returns from the console. */
  glanceGrowFromPill?: boolean;
}) {
  const pillHintWidth = useMemo(
    () => measurePillHintWidth(replyShortcuts),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [replyShortcuts?.pushToTalk, replyShortcuts?.toggle, replyShortcuts?.lastOrigin],
  );
  // Until the shortcuts are known the field keeps its locked placeholder and
  // the pill shows no hint, so neither changes wording on first load.
  const fieldHint =
    replyShortcuts === null ? undefined : <ReplyHint shortcuts={replyShortcuts} form="field" />;
  const hasValidSummary =
    completion.kind === "response" &&
    spokenSummary !== null &&
    spokenSummary.trim().length > 0;
  const derivedIntent: "compact" | "summary" | "full" =
    expanded
      ? "full"
      : showSummaryText && hasValidSummary
        ? "summary"
        : "compact";
  const intent = renderingIntent ?? derivedIntent;

  if (intent === "compact") {
    const response = completion.kind === "response" ? completion : null;
    const provider = response?.agent ?? "claude";
    const providerLabel = agentProviderLabel(provider);
    const sourceName = response?.source_name ?? providerLabel;
    // The wire text still carries Claude's authored marker line; the
    // accessible label strips it the same way the expanded body does so a
    // screen reader does not read the raw marker.
    const responseBody = (() => {
      if (response === null) return null;
      const withoutMarker = stripSpokenSummaryLine(response.text);
      return withoutMarker.trim().length > 0 ? withoutMarker : null;
    })();
    const displayText =
      replyDraft !== null
        ? replyDraft.text
        : completion.kind === "overflow"
          ? completion.text
          : markdownToPlainText(
              responseBody ?? spokenSummary ?? `${providerLabel} finished.`,
            );
    const queueCopy = remainingCount > 0 ? queueDepthCopy(remainingCount) : null;
    // The compact pill (decision 19) has no footer, so markerKnown's "no
    // audio" note and the truncated flag no longer have a visual home. They
    // still have to reach the user somehow (decision 37, research.md), so
    // both fold into the accessible label instead of being silently dropped.
    const audioCopy =
      !showSummaryText &&
      response !== null &&
      markerKnown &&
      spokenSummary === null
        ? "No audio for this reply"
        : null;
    const missingSummaryCopy =
      showSummaryText && response !== null && !hasValidSummary
        ? "No summary for this reply"
        : null;
    const truncatedCopy =
      truncated && response !== null ? "Response continues" : null;
    const replyCopy =
      replyWindow === null
        ? null
        : `Reply for ${formatReplyTime(replyWindow.remainingSeconds)}`;
    const summaryPart =
      missingSummaryCopy ??
      (spokenSummary === null ? displayText : `Spoken summary. ${displayText}`);
    const accessibleLabel = [
      agentIdentityPhrase(provider, sourceName),
      summaryPart,
      audioCopy,
      truncatedCopy,
      queueCopy,
      replyCopy,
      replyDraft?.phase === "draft_ready" ? "Press Enter to submit" : null,
      replyDraft?.phase === "draft_ready" ? "Click to edit" : null,
    ]
      .filter((part): part is string => part !== null)
      .join(". ");

    const railPhase = deriveReplyRailPhase(
      replyDraft,
      replyPhase,
      sentReply !== null &&
        sentReply.completionId === completion.completion_id,
    );
    const railRow = replyRailText(
      railPhase, replyDraft?.text ?? replyKeptText, sentReply?.text ?? "", replyPreview, replyStableCharCount,
    );
    const railText = railRow.text;

    return (
      <CompactClaudePill
        provider={provider}
        sourceName={sourceName}
        restingHint={
          canReply && replyShortcuts !== null && !replyHintRetired
            ? <ReplyHint shortcuts={replyShortcuts} form="pill" />
            : undefined
        }
        restingHintWidth={pillHintWidth}
        restingTime={
          canReply && replyWindow !== null
            ? formatReplyTime(replyWindow.remainingSeconds)
            : null
        }
        onRestingClick={canReply ? onDraftEdit : undefined}
        onMeasuredCardSizeChange={onMeasuredCardSizeChange}
        speaking={speaking}
        remainingCount={remainingCount}
        railPhase={railPhase}
        accessibleLabel={accessibleLabel}
        railProps={{
          text: railText,
          stableCharCount: railRow.stableCharCount,
          keptText: railRow.keptText,
          rms,
          peak,
          onEditStart: onDraftEdit,
          canReply,
          editingValue: editingText,
          onEditingChange: onEditingTextChange,
          onEditingKeyDown,
          editingInputRef,
          submitHint: !replyAutoSubmit,
          // The compact rail only opens for speech, a draft or a receipt,
          // so a spoken draft can land whenever it is open.
          reserveHint: !replyAutoSubmit,
          notice: replyNotice,
          agentName: agentProviderLabel(completion.kind === "response" ? completion.agent : "claude"),
          onCopy: onCopyDraft,
        }}
      />
    );
  }

  if (intent === "summary") {
    const summaryProvider =
      completion.kind === "response" ? completion.agent : "claude";
    const summaryProviderLabel = agentProviderLabel(summaryProvider);
    const summarySourceName =
      completion.kind === "response"
        ? (completion.source_name ?? summaryProviderLabel)
        : summaryProviderLabel;
    const summaryBody = spokenSummary?.trim() ?? "";
    const summaryRailPhase = deriveReplyRailPhase(
      replyDraft,
      replyPhase,
      sentReply !== null &&
        sentReply.completionId === completion.completion_id,
    );
    const summaryRailRow = replyRailText(
      summaryRailPhase, replyDraft?.text ?? replyKeptText, sentReply?.text ?? "", replyPreview, replyStableCharCount,
    );
    const summaryRailText = summaryRailRow.text;
    const summaryQueueCopy =
      remainingCount > 0 ? queueDepthCopy(remainingCount) : null;
    const summaryReplyCopy =
      replyWindow === null
        ? null
        : `Reply for ${formatReplyTime(replyWindow.remainingSeconds)}`;
    const summaryAccessibleLabel = [
      agentIdentityPhrase(summaryProvider, summarySourceName),
      `Summary. ${markdownToPlainText(summaryBody)}`,
      summaryQueueCopy,
      summaryReplyCopy,
      replyDraft?.phase === "draft_ready" ? "Press Enter to submit" : null,
      replyDraft?.phase === "draft_ready" ? "Click to edit" : null,
    ]
      .filter((part): part is string => part !== null)
      .join(". ");

    return (
      <ClaudeExpandedCard
        provider={summaryProvider}
        body={summaryBody}
        sourceName={summarySourceName}
        speaking={speaking}
        spineSegments={spineSegmentsFromSpokenSummary(spokenSummary ?? "")}
        remainingCount={remainingCount}
        accessibleLabel={summaryAccessibleLabel}
        railPhase={summaryRailPhase ?? "resting"}
        countdownHint={
          replyWindow === null
            ? null
            : formatReplyTime(replyWindow.remainingSeconds)
        }
        railProps={{
          text: summaryRailText,
          stableCharCount: summaryRailRow.stableCharCount,
          keptText: summaryRailRow.keptText,
          rms,
          peak,
          editingAutoFocus: false,
          onEditStart: onDraftEdit,
          canReply,
          editingValue: editingText,
          onEditingChange: onEditingTextChange,
          onEditingKeyDown,
          editingInputRef,
          submitHint: !replyAutoSubmit,
          reserveHint: !replyAutoSubmit && canReply,
          notice: replyNotice,
          // Retired: the line keeps only the countdown, still clickable.
          placeholder: replyHintRetired ? null : fieldHint,
          agentName: agentProviderLabel(completion.kind === "response" ? completion.agent : "claude"),
          onCopy: onCopyDraft,
        }}
        sessions={sessions}
        activeSessionHandle={activeSessionHandle}
        onSelectSession={onSelectSession}
        onCardFocusChange={onCardFocusChange}
        compactConsole={false}
        summaryMode={true}
        growFromPill={glanceGrowFromPill}
        onToggleExpanded={onToggleExpanded}
        onMeasuredCardSizeChange={onMeasuredCardSizeChange}
      />
    );
  }

  // --- Expanded (design.md state 7 and 8, decisions 13, 20, 21, 22, 26) ---
  // The whole response at natural height. The marker line is stripped the same
  // way the compact accessible label strips it, so Claude's authored spoken
  // summary never shows up twice (decision 21: the strip is dropped whenever
  // the full text is available; its information lives in the spine's
  // segmentation).
  const expandedProvider =
    completion.kind === "response" ? completion.agent : "claude";
  const expandedProviderLabel = agentProviderLabel(expandedProvider);
  const expandedSourceName =
    completion.kind === "response"
      ? (completion.source_name ?? expandedProviderLabel)
      : expandedProviderLabel;
  const expandedBody = (() => {
    const stripped = stripSpokenSummaryLine(completion.text);
    return stripped.trim().length > 0
      ? stripped
      : `${expandedProviderLabel} finished.`;
  })();
  const expandedRailPhase = deriveReplyRailPhase(
    replyDraft,
    replyPhase,
    sentReply !== null && sentReply.completionId === completion.completion_id,
  );
  const expandedRailRow = replyRailText(
    expandedRailPhase, replyDraft?.text ?? replyKeptText, sentReply?.text ?? "", replyPreview, replyStableCharCount,
  );
  const expandedRailText = expandedRailRow.text;
  // spokenSummary / markerKnown / truncated no longer feed the body, but they
  // still have to reach a screen reader (decision 37, research.md), so they
  // keep feeding the accessible label exactly as they do in compact.
  const expandedAudioCopy =
    completion.kind === "response" && markerKnown && spokenSummary === null
      ? "No audio for this reply"
      : null;
  const expandedTruncatedCopy =
    truncated && completion.kind === "response" ? "Response continues" : null;
  const expandedQueueCopy = remainingCount > 0 ? queueDepthCopy(remainingCount) : null;
  const expandedReplyCopy =
    replyWindow === null
      ? null
      : `Reply for ${formatReplyTime(replyWindow.remainingSeconds)}`;
  const expandedAccessibleLabel = [
    agentIdentityPhrase(expandedProvider, expandedSourceName),
    markdownToPlainText(expandedBody),
    expandedAudioCopy,
    expandedTruncatedCopy,
    expandedQueueCopy,
    expandedReplyCopy,
  ]
    .filter((part): part is string => part !== null)
    .join(". ");

  return (
    <ClaudeExpandedCard
      provider={expandedProvider}
      body={expandedBody}
      sourceName={expandedSourceName}
      speaking={speaking}
      spineSegments={spineSegmentsFromSpokenSummary(spokenSummary ?? "")}
      remainingCount={remainingCount}
      accessibleLabel={expandedAccessibleLabel}
      railPhase={expandedRailPhase ?? "resting"}
      countdownHint={
        replyWindow === null
          ? null
          : formatReplyTime(replyWindow.remainingSeconds)
      }
      railProps={{
        text: expandedRailText,
        stableCharCount: expandedRailRow.stableCharCount,
          keptText: expandedRailRow.keptText,
        rms,
        peak,
        editingAutoFocus: compactConsole,
        onEditStart: onDraftEdit,
        canReply,
        editingValue: editingText,
        onEditingChange: onEditingTextChange,
        onEditingKeyDown,
        editingInputRef,
        // Reserved whenever a spoken draft can land here, so its hint never
        // resizes the native window.
        submitHint: !replyAutoSubmit,
        reserveHint: !replyAutoSubmit && canReply,
        notice: replyNotice,
        placeholder: fieldHint,
        agentName: agentProviderLabel(completion.kind === "response" ? completion.agent : "claude"),
          onCopy: onCopyDraft,
      }}
      sessions={sessions}
      activeSessionHandle={activeSessionHandle}
      onSelectSession={onSelectSession}
      onCardFocusChange={onCardFocusChange}
      compactConsole={compactConsole}
      consoleResize={consoleResize}
      onToggleExpanded={onToggleExpanded}
      summaryMode={false}
      onMeasuredCardSizeChange={onMeasuredCardSizeChange}
    />
  );
}
