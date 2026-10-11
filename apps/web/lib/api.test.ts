import { afterEach, describe, expect, it, vi } from "vitest";
import { isPublicApiPath } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("isPublicApiPath", () => {
  it("treats /health and /demo/* as public", () => {
    expect(isPublicApiPath("/health")).toBe(true);
    expect(isPublicApiPath("/demo/abc")).toBe(true);
    expect(isPublicApiPath("/projects")).toBe(false);
    expect(isPublicApiPath("/me")).toBe(false);
  });
});
