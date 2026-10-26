import { describe, expect, it } from "vitest";
import {
  STATIC_WINDOW_ROOT_PATHS,
  findUnregisteredStaticWindowPaths,
  resolveWindowRoot,
} from "./windowRoots";

describe("resolveWindowRoot", () => {
  it.each([
    ["/", "app"],
    ["/hud", "hud"],
    ["/recording-control", "recording-control"],
    ["/snap-guide", "snap-guide"],
    ["/popup/review", "review-popup"],
    ["/popup/update", "update-popup"],
    ["/popup/eula", "eula-popup"],
    ["/popup/error", "error-popup"],
  ] as const)("maps %s to %s", (pathname, expected) => {
    expect(resolveWindowRoot(pathname, false)).toBe(expected);
  });

  it("keeps the dialog showcase development-only", () => {
    expect(resolveWindowRoot("/debug/dialogs", true)).toBe("dialog-showcase");
    expect(resolveWindowRoot("/debug/dialogs", false)).toBe("app");
  });

  it("keeps the UI QA showcase development-only", () => {
    expect(resolveWindowRoot("/debug/ui-qa", true)).toBe("ui-qa-showcase");
    expect(resolveWindowRoot("/debug/ui-qa", false)).toBe("app");
  });

  it("keeps the demo stage development-only", () => {
    expect(resolveWindowRoot("/debug/demo", true)).toBe("demo-stage");
    expect(resolveWindowRoot("/debug/demo", false)).toBe("app");
  });

  it("falls back to the settings app for unknown paths", () => {
    expect(resolveWindowRoot("/future-window", false)).toBe("app");
  });

  it("recognizes the complete current static window registry", () => {
    expect(findUnregisteredStaticWindowPaths(STATIC_WINDOW_ROOT_PATHS)).toEqual(
      [],
    );
  });

  it("rejects a future static window without an explicit root", () => {
    expect(
      findUnregisteredStaticWindowPaths([
        ...STATIC_WINDOW_ROOT_PATHS,
        "/future-window",
      ]),
    ).toEqual(["/future-window"]);
  });
});
