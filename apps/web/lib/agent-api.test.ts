import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { isTryUnavailable } from "./agent-api";

describe("isTryUnavailable", () => {
  it("is true only when the API has no such route", () => {
    expect(isTryUnavailable(new ApiError(404, "not_found", "Not Found"))).toBe(true);
    expect(isTryUnavailable(new ApiError(405, "http_error", "Method Not Allowed"))).toBe(true);
    expect(isTryUnavailable(new ApiError(404, "not_found", "We couldn't find that run."))).toBe(false);
    expect(isTryUnavailable(new ApiError(503, "http_error", "Try again in a minute."))).toBe(false);
  });
});
