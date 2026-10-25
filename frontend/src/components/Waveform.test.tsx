import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AudioLevelsContext, VoiceBars } from "./Waveform";
vi.mock("framer-motion", () => ({ motion: { div: ({ animate }: { animate: { height: number } }) => <div data-height={animate.height} /> }, useReducedMotion: () => false }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

it("samples the latest ref at the existing cadence without rerendering its coordinator", () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  const levels = { current: { rms: 1, peak: 1 } };
  let coordinatorRenders = 0;
  function Coordinator() {
    coordinatorRenders += 1;
    return <AudioLevelsContext.Provider value={levels}><VoiceBars state="recording" rms={0} peak={0} /></AudioLevelsContext.Provider>;
  }
  const { container } = render(<Coordinator />);
  act(() => vi.advanceTimersByTime(120));
  expect(container.querySelectorAll('[data-height="10"]')).toHaveLength(7);
  levels.current.rms = 0;
  levels.current.peak = 0;
  act(() => vi.advanceTimersByTime(120));
  expect(container.querySelectorAll('[data-height="2"]')).toHaveLength(7);
  expect(coordinatorRenders).toBe(1);
});

it("keeps deterministic fixture props when the live level context is disabled", () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  const { container } = render(<AudioLevelsContext.Provider value={null}><VoiceBars state="recording" rms={1} peak={1} /></AudioLevelsContext.Provider>);
  act(() => vi.advanceTimersByTime(120));
  expect(container.querySelectorAll('[data-height="10"]')).toHaveLength(7);
});

it("runs the same per-bar roll at rail size: five bars up to 12px", () => {
  vi.useFakeTimers();
  vi.spyOn(Math, "random").mockReturnValue(0.5);
  const { container } = render(
    <AudioLevelsContext.Provider value={null}>
      <VoiceBars state="recording" size="rail" rms={1} peak={1} testId="rail" />
    </AudioLevelsContext.Provider>,
  );
  const group = container.querySelector('[data-testid="rail"]') as HTMLElement;
  expect(group.style.height).toBe("12px");
  expect(group.style.gap).toBe("2px");
  // Dots in silence before the first tick.
  expect(container.querySelectorAll('[data-height="2"]')).toHaveLength(5);
  act(() => vi.advanceTimersByTime(120));
  // 2 + 0.5 * (12 - 2) at full level.
  expect(container.querySelectorAll('[data-height="7"]')).toHaveLength(5);
});

it("rolls each rail bar independently instead of scaling them together", () => {
  vi.useFakeTimers();
  const rolls = [0.1, 0.9, 0.4, 1, 0];
  let call = 0;
  vi.spyOn(Math, "random").mockImplementation(() => rolls[call++ % rolls.length]);
  const { container } = render(
    <AudioLevelsContext.Provider value={null}>
      <VoiceBars state="recording" size="rail" rms={1} peak={1} />
    </AudioLevelsContext.Provider>,
  );
  act(() => vi.advanceTimersByTime(120));
  const heights = Array.from(container.querySelectorAll("[data-height]")).map(
    (bar) => Number(bar.getAttribute("data-height")),
  );
  expect(heights).toEqual([3, 11, 6, 12, 2]);
});
