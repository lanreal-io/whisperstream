// @vitest-environment node

import { describe, it, expect } from "vitest";
import { formatModelBytes } from "./format";

describe("formatModelBytes", () => {
  const MIB = 1024 ** 2;
  const GIB = 1024 ** 3;

  it("reports whole megabytes below one gigabyte", () => {
    expect(formatModelBytes(937 * MIB)).toBe("937 MB");
    expect(formatModelBytes(600 * MIB)).toBe("600 MB");
  });

  it("switches to one-decimal gigabytes at exactly 1 GiB", () => {
    expect(formatModelBytes(GIB)).toBe("1.0 GB");
    expect(formatModelBytes(GIB - 1)).toBe("1024 MB");
    expect(formatModelBytes(1.53 * GIB)).toBe("1.5 GB");
  });

  it("never rounds a nonzero size down to nothing", () => {
    expect(formatModelBytes(4096)).toBe("1 MB");
  });

  it("treats zero and invalid input as zero rather than throwing", () => {
    expect(formatModelBytes(0)).toBe("0 MB");
    expect(formatModelBytes(-1)).toBe("0 MB");
    expect(formatModelBytes(Number.NaN)).toBe("0 MB");
  });
});
