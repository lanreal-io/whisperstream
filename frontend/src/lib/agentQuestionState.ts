import type { AgentQuestion, AgentQuestionSnapshotEvent } from "./socket/agentEvents";

export interface AgentQuestionState {
  revision: number;
  active: AgentQuestionSnapshotEvent["active"];
  /** The socket connection the revision belongs to. */
  connection?: number;
}

export type AgentQuestionAction = {
  type: "snapshot";
  event: AgentQuestionSnapshotEvent;
  /** Socket connection generation. Revisions restart with a new backend, so
   *  the first snapshot on a new connection is always taken. */
  connection?: number;
};

export function createAgentQuestionState(): AgentQuestionState {
  return { revision: 0, active: null };
}

export function agentQuestionReducer(
  state: AgentQuestionState,
  action: AgentQuestionAction,
): AgentQuestionState {
  const sameConnection = action.connection === undefined || action.connection === state.connection;
  if (sameConnection && action.event.revision < state.revision) return state;
  return {
    revision: action.event.revision,
    active: action.event.active,
    connection: action.connection ?? state.connection,
  };
}

export function questionIsTerminal(
  phase: NonNullable<AgentQuestionSnapshotEvent["active"]>["phase"],
): boolean {
  return (
    phase === "answered" ||
    phase === "deferred" ||
    phase === "expired" ||
    phase === "held_failed"
  );
}

export function questionCanAnswer(
  active: AgentQuestionSnapshotEvent["active"],
): boolean {
  return active !== null && !questionIsTerminal(active.phase);
}

export function questionAnswerText(
  selectedLabels: string[],
  freeform: string,
): string {
  const typed = freeform.trim();
  return typed || selectedLabels.join(", ");
}

export function exactQuestionAnswers(
  questions: AgentQuestion[],
  selected: Record<string, string[]>,
  freeform: Record<string, string>,
): Record<string, string> | null {
  const answers: Record<string, string> = {};
  for (const question of questions) {
    const answer = questionAnswerText(
      selected[question.question] ?? [],
      freeform[question.question] ?? "",
    );
    if (!answer) return null;
    answers[question.question] = answer;
  }
  return answers;
}

export function toggleQuestionOption(
  question: AgentQuestion,
  previous: string[],
  label: string,
): string[] {
  if (!question.multiSelect) return [label];
  return previous.includes(label)
    ? previous.filter((candidate) => candidate !== label)
    : [...previous, label];
}
