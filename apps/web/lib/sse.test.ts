import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { RunEvent } from "@/types/generated";
import { mergeEvents, openRunStream, replayEvents, type EventSourceLike } from "./sse";

const ev = (seq: number, over: Partial<RunEvent> = {}): RunEvent => ({
  run_id: "r1",
  seq,
  head: "planner",
  type: "progress",
  message: `m${seq}`,
  payload: {},
  trace_url: null,
  at: "2026-10-11T00:00:00Z",
  ...over,
});

class FakeSource implements EventSourceLike {
  onopen: ((e: Event) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;
  closed = false;
  constructor(public url: string) {}
  close() {
    this.closed = true;
  }
  send(e: RunEvent) {
    this.onmessage?.({ data: JSON.stringify(e) } as MessageEvent);
  }
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("mergeEvents", () => {
  it("dedupes by seq and sorts", () => {
    const merged = mergeEvents([ev(1), ev(3)], [ev(2), ev(3), ev(4)]);
    expect(merged.map((e) => e.seq)).toEqual([1, 2, 3, 4]);
  });
  it("returns the same array when nothing is new", () => {
    const base = [ev(1)];
    expect(mergeEvents(base, [ev(1)])).toBe(base);
  });
});

describe("openRunStream", () => {
  it("resumes from the last seq after a drop and ignores replayed events", () => {
    const sources: FakeSource[] = [];
    const got: number[] = [];
    openRunStream("r1", {
      baseUrl: "http://x",
      retryMs: 100,
      onEvent: (e) => got.push(e.seq),
      createSource: (url) => {
        const s = new FakeSource(url);
        sources.push(s);
        return s;
      },
    });
    sources[0].send(ev(1));
    sources[0].send(ev(2));
    sources[0].onerror?.(new Event("error"));
    expect(sources[0].closed).toBe(true);
    vi.advanceTimersByTime(100);
    expect(sources[1].url).toBe("http://x/runs/r1/events?last_seq=2");
    sources[1].send(ev(2)); // server replays one we already have
    sources[1].send(ev(3));
    expect(got).toEqual([1, 2, 3]);
  });

  it("stops reconnecting once the run finishes", () => {
    const sources: FakeSource[] = [];
    openRunStream("r1", {
      retryMs: 100,
      onEvent: () => {},
      createSource: (url) => {
        const s = new FakeSource(url);
        sources.push(s);
        return s;
      },
    });
    sources[0].send(ev(1, { head: "system", type: "finished" }));
    sources[0].onerror?.(new Event("error"));
    vi.advanceTimersByTime(1000);
    expect(sources).toHaveLength(1);
  });

  it("close() cancels a pending reconnect", () => {
    const sources: FakeSource[] = [];
    const stream = openRunStream("r1", {
      retryMs: 100,
      onEvent: () => {},
      createSource: (url) => {
        const s = new FakeSource(url);
        sources.push(s);
        return s;
      },
    });
    sources[0].onerror?.(new Event("error"));
    stream.close();
    vi.advanceTimersByTime(1000);
    expect(sources).toHaveLength(1);
  });
});

describe("openRunStream giving up", () => {
  it("backs off then reports giving up after maxRetries", () => {
    const sources: FakeSource[] = [];
    const onGiveUp = vi.fn();
    openRunStream("r1", {
      retryMs: 100,
      maxRetries: 2,
      onGiveUp,
      onEvent: () => {},
      createSource: (url) => {
        const s = new FakeSource(url);
        sources.push(s);
        return s;
      },
    });
    sources[0].onerror?.(new Event("error"));
    vi.advanceTimersByTime(100);
    sources[1].onerror?.(new Event("error"));
    vi.advanceTimersByTime(199);
    expect(sources).toHaveLength(2);
    vi.advanceTimersByTime(1);
    sources[2].onerror?.(new Event("error"));
    expect(onGiveUp).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(10000);
    expect(sources).toHaveLength(3);
  });
});

describe("replayEvents", () => {
  it("emits events in order on a timer, then calls onDone", () => {
    const got: number[] = [];
    const onDone = vi.fn();
    replayEvents([ev(1), ev(2), ev(3)], { onEvent: (e) => got.push(e.seq), onDone, intervalMs: 50 });
    vi.advanceTimersByTime(0);
    expect(got).toEqual([1]);
    vi.advanceTimersByTime(100);
    expect(got).toEqual([1, 2, 3]);
    vi.advanceTimersByTime(50);
    expect(onDone).toHaveBeenCalledOnce();
  });
});
