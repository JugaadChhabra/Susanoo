import type { RunEvent } from "@/types/generated";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type StreamStatus = "connecting" | "live" | "reconnecting" | "closed";

/** Merge new events into a list: drop duplicates by seq, keep seq order. Returns the same array if nothing changed. */
export function mergeEvents(current: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const seen = new Set(current.map((e) => e.seq));
  const fresh: RunEvent[] = [];
  for (const event of incoming) {
    if (!seen.has(event.seq)) {
      seen.add(event.seq);
      fresh.push(event);
    }
  }
  if (fresh.length === 0) return current;
  return [...current, ...fresh].sort((a, b) => a.seq - b.seq);
}

/** Minimal slice of EventSource we use, so tests can pass a fake. */
export interface EventSourceLike {
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close(): void;
}
export type EventSourceFactory = (url: string) => EventSourceLike;

export interface StreamOptions {
  onEvent: (event: RunEvent) => void;
  onStatus?: (status: StreamStatus) => void;
  /** Called once when we give up reconnecting. */
  onGiveUp?: () => void;
  maxRetries?: number;
  baseUrl?: string;
  /** Delay before reopening a dropped connection. */
  retryMs?: number;
  createSource?: EventSourceFactory;
}

export interface Stream {
  close(): void;
}

/**
 * Live stream of GET /runs/{id}/events.
 * On a dropped connection we close and reopen ourselves, passing the last seq we saw
 * (`?last_seq=N`) so the server resumes after it; duplicates are dropped by seq regardless.
 */
export function openRunStream(runId: string, opts: StreamOptions): Stream {
  const {
    onEvent,
    onStatus,
    onGiveUp,
    maxRetries = 6,
    baseUrl = API_URL,
    retryMs = 1500,
    createSource = (url) => new EventSource(url, { withCredentials: true }),
  } = opts;
  let lastSeq = 0;
  let source: EventSourceLike | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let retries = 0;

  const connect = () => {
    const query = lastSeq > 0 ? `?last_seq=${lastSeq}` : "";
    const src = createSource(`${baseUrl}/runs/${encodeURIComponent(runId)}/events${query}`);
    source = src;
    src.onopen = () => {
      retries = 0;
      onStatus?.("live");
    };
    src.onmessage = (msg) => {
      let event: RunEvent;
      try {
        event = JSON.parse(msg.data as string) as RunEvent;
      } catch {
        return; // a malformed frame must not kill the stream
      }
      retries = 0;
      if (event.seq <= lastSeq) return; // dedupe by seq
      lastSeq = event.seq;
      onEvent(event);
      if (event.head === "system" && (event.type === "finished" || event.type === "error")) {
        stop(); // run is over; don't reconnect
      }
    };
    src.onerror = () => {
      src.close();
      if (closed) return;
      if (retries >= maxRetries) {
        stop();
        onGiveUp?.();
        return;
      }
      onStatus?.("reconnecting");
      timer = setTimeout(connect, retryMs * 2 ** retries++); // backoff: 1.5s, 3s, 6s…
    };
  };

  const stop = () => {
    closed = true;
    if (timer) clearTimeout(timer);
    source?.close();
    onStatus?.("closed");
  };

  onStatus?.("connecting");
  connect();
  return { close: stop };
}

/** Replays stored events on a timer (fixture mode and demo replay). */
export function replayEvents(
  events: RunEvent[],
  opts: { onEvent: (event: RunEvent) => void; onDone?: () => void; intervalMs?: number },
): Stream {
  const { onEvent, onDone, intervalMs = 900 } = opts;
  let index = 0;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const tick = () => {
    if (closed) return;
    if (index >= events.length) {
      onDone?.();
      return;
    }
    onEvent(events[index++]);
    timer = setTimeout(tick, intervalMs);
  };
  timer = setTimeout(tick, 0);

  return {
    close() {
      closed = true;
      if (timer) clearTimeout(timer);
    },
  };
}

export async function fetchDemoEvents(runId: string): Promise<RunEvent[]> {
  const response = await fetch(`${API_URL}/demo/${encodeURIComponent(runId)}`);
  if (!response.ok) throw new Error("We couldn't load this demo run.");
  return (await response.json()) as RunEvent[];
}
