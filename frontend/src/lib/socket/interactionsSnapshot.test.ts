import { expect, it } from "vitest";
import { parseAppSocketEvent } from "./events";

it("validates the complete replacement before granting any interaction authority", () => {
  const empty = { type: "agent.interactions.snapshot", version: 1, revision: 4, interactions: [] };
  expect(parseAppSocketEvent(empty)).toEqual(empty);
  for (const value of [{ ...empty, revision: -1 }, { ...empty, interactions: [{}] }, { ...empty, session_id: "private" }, { ...empty, version: 2 }]) {
    expect(parseAppSocketEvent(value)).toBeNull();
  }
});
