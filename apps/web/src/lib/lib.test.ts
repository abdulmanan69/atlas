import { formatPercent, formatRelative } from "@/lib/format";
import { initials, safeNext } from "@/lib/utils";

describe("formatPercent", () => {
  it("distinguishes not-computable from zero", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(0.5)).toBe("50%");
  });
});

describe("formatRelative", () => {
  it("formats past and future offsets", () => {
    const now = Date.parse("2026-01-10T12:00:00Z");
    expect(formatRelative("2026-01-10T11:59:50Z", now)).toBe("just now");
    expect(formatRelative("2026-01-09T12:00:00Z", now)).toBe("yesterday");
    expect(formatRelative("2026-01-12T12:00:00Z", now)).toBe("in 2 days");
  });
});

describe("safeNext", () => {
  it("allows same-site paths only", () => {
    expect(safeNext("/leads?stage=1")).toBe("/leads?stage=1");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext(null)).toBe("/");
  });
});

describe("initials", () => {
  it("uses first and last name", () => {
    expect(initials("Sara Ahmed Khan")).toBe("SK");
    expect(initials("  ")).toBe("?");
  });
});
