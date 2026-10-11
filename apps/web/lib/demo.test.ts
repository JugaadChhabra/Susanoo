import { afterEach, describe, expect, it, vi } from "vitest";
import type { RunEvent } from "@/types/generated";
import { demoSource } from "./demo";
import { fetchDemoEvents } from "./sse";

afterEach(() => vi.unstubAllGlobals());

describe("demoSource", () => {
  it("replays the named saved run", () => {
    expect(demoSource(" abc ", true)).toEqual({ kind: "demo", runId: "abc" });
  });
  it("falls back to the fixture in development and to nothing in production", () => {
    expect(demoSource(undefined, false)).toEqual({ kind: "fixture" });
    expect(demoSource("", true)).toBeNull();
  });
});

describe("fetchDemoEvents", () => {
  it("calls GET /demo/{id} without credentials or auth headers", async () => {
    const events: RunEvent[] = [];
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => events });
    vi.stubGlobal("fetch", fetchMock);
    await expect(fetchDemoEvents("r 1")).resolves.toBe(events);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0];
    const headers = new Headers(init?.headers);
    expect(url).toMatch(/\/demo\/r%201$/);
    expect(headers.has("authorization")).toBe(false);
    expect(init?.credentials ?? "omit").toBe("omit");
  });
  it("throws a plain-English error when the run is missing", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    await expect(fetchDemoEvents("nope")).rejects.toThrow("couldn't load");
  });
});
