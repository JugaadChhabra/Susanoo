import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, isPublicApiPath, isStepMovedOn, STEP_MOVED_ON } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("isPublicApiPath", () => {
  it("treats /health and /demo/* as public", () => {
    expect(isPublicApiPath("/health")).toBe(true);
    expect(isPublicApiPath("/demo/abc")).toBe(true);
    expect(isPublicApiPath("/projects")).toBe(false);
    expect(isPublicApiPath("/me")).toBe(false);
  });
});

describe("isStepMovedOn", () => {
  it("is true only for a 409 from the API", () => {
    expect(isStepMovedOn(new ApiError(409, "http_error", "This question was already answered or has moved on; please reload."))).toBe(true);
    expect(isStepMovedOn(new ApiError(422, "validation_error", "Bad answer."))).toBe(false);
    expect(isStepMovedOn(new Error("boom"))).toBe(false);
  });
  it("says so in plain English", () => expect(STEP_MOVED_ON).toMatch(/^This step has moved on/));
});
