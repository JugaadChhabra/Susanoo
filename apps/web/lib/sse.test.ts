import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import type { RunEvent } from "@/types/generated";
import { mergeEvents, openRunStream, replayEvents, SseParser } from "./sse";

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

/** One fake HTTP connection to the stream: the test pushes SSE text, then ends or drops it. */
class FakeConnection {
  private controller!: ReadableStreamDefaultController<Uint8Array>;
  readonly response: Response;
  constructor(public url: string, status = 200) {
    const body = new ReadableStream<Uint8Array>({ start: (c) => void (this.controller = c) });
    this.response = new Response(status === 200 ? body : null, { status });
  }
  write(text: string) {
    this.controller.enqueue(new TextEncoder().encode(text));
  }
  send(e: RunEvent) {
    this.write(`id: ${e.seq}\ndata: ${JSON.stringify(e)}\n\n`);
  }
  end() {
    this.controller.close();
  }
  drop() {
    this.controller.error(new TypeError("network error"));
  }
}

function fakeServer(statuses: number[] = []) {
  const connections: FakeConnection[] = [];
  const fetcher = async (url: string) => {
    const conn = new FakeConnection(url, statuses[connections.length] ?? 200);
    connections.push(conn);
    return conn.response;
  };
  return { connections, fetcher };
}

/** Let pending promise callbacks (fetch, reads) run. */
const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve();
};

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

describe("SseParser", () => {
  it("joins data lines, keeps the id, skips comments and handles split chunks", () => {
    const parser = new SseParser();
    expect(parser.push("retry: 3000\n\n: keep-alive\n\nid: 4\nda")).toEqual([]);
    expect(parser.push("ta: {\"a\":\r\ndata: 1}\r\n\r\n")).toEqual([{ id: "4", data: '{"a":\n1}' }]);
  });
  it("doesn't read a CRLF split across chunks as two line endings", () => {
    const parser = new SseParser();
    expect(parser.push("id: 5\r")).toEqual([]);
    expect(parser.push("\ndata: x\r")).toEqual([]);
    expect(parser.push("\n\r\n")).toEqual([{ id: "5", data: "x" }]);
  });
});

describe("openRunStream", () => {
  it("resumes from the last seq after a drop and ignores replayed events", async () => {
    const { connections, fetcher } = fakeServer();
    const got: number[] = [];
    const statuses: string[] = [];
    openRunStream("r1", { baseUrl: "http://x", retryMs: 100, fetcher, onEvent: (e) => got.push(e.seq), onStatus: (s) => statuses.push(s) });
    await flush();
    expect(connections[0].url).toBe("http://x/runs/r1/events");
    connections[0].send(ev(1));
    connections[0].send(ev(2));
    await flush();
    connections[0].drop();
    await flush();
    expect(statuses).toEqual(["connecting", "live", "reconnecting"]);
    await vi.advanceTimersByTimeAsync(100);
    expect(connections[1].url).toBe("http://x/runs/r1/events?after=2");
    connections[1].send(ev(2)); // server replays one we already have
    connections[1].send(ev(3));
    await flush();
    expect(got).toEqual([1, 2, 3]);
  });

  it("stops once the run finishes", async () => {
    const { connections, fetcher } = fakeServer();
    openRunStream("r1", { retryMs: 100, fetcher, onEvent: () => {} });
    await flush();
    connections[0].send(ev(1, { head: "system", type: "finished" }));
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    expect(connections).toHaveLength(1);
  });

  it("asks whether the run is over when the server ends the stream, and reconnects if not", async () => {
    const { connections, fetcher } = fakeServer();
    const isRunOver = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    openRunStream("r1", { retryMs: 100, fetcher, isRunOver, onEvent: () => {} });
    await flush();
    connections[0].send(ev(1));
    connections[0].end();
    await flush();
    await vi.advanceTimersByTimeAsync(100);
    expect(connections).toHaveLength(2);
    expect(connections[1].url).toContain("?after=1");
    connections[1].end();
    await flush();
    await vi.advanceTimersByTimeAsync(1000);
    expect(connections).toHaveLength(2);
    expect(isRunOver).toHaveBeenCalledTimes(2);
  });

  it("gives up at once with a plain message on 401 or 404", async () => {
    for (const [status, words] of [[401, "log in again"], [404, "couldn't find this run"]] as const) {
      const { connections, fetcher } = fakeServer([status]);
      const onGiveUp = vi.fn();
      openRunStream("r1", { retryMs: 100, fetcher, onGiveUp, onEvent: () => {} });
      await flush();
      await vi.advanceTimersByTimeAsync(1000);
      expect(connections).toHaveLength(1);
      expect(onGiveUp).toHaveBeenCalledWith(expect.stringContaining(words));
    }
  });

  it("close() cancels a pending reconnect", async () => {
    const { connections, fetcher } = fakeServer([503]);
    const stream = openRunStream("r1", { retryMs: 100, fetcher, onEvent: () => {} });
    await flush();
    stream.close();
    await vi.advanceTimersByTimeAsync(1000);
    expect(connections).toHaveLength(1);
  });
});

describe("openRunStream giving up", () => {
  it("backs off then reports giving up after maxRetries", async () => {
    const { connections, fetcher } = fakeServer([503, 503, 503]);
    const onGiveUp = vi.fn();
    openRunStream("r1", { retryMs: 100, maxRetries: 2, onGiveUp, fetcher, onEvent: () => {} });
    await flush();
    await vi.advanceTimersByTimeAsync(100);
    expect(connections).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(199);
    expect(connections).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(connections).toHaveLength(3);
    await flush();
    expect(onGiveUp).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(10000);
    expect(connections).toHaveLength(3);
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
