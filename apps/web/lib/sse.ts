import { getAccessToken } from "@/lib/supabase";
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

/** One SSE message: its `id:` (if any) and its joined `data:` lines. */
export interface SseMessage {
  id: string | null;
  data: string;
}

/**
 * Incremental parser for a text/event-stream body. Feed it decoded chunks as they arrive; it returns
 * every message completed so far. Comments (`: keep-alive`) and `retry:` lines are ignored.
 */
export class SseParser {
  private buffer = "";
  private data: string[] = [];
  private id: string | null = null;

  push(chunk: string): SseMessage[] {
    this.buffer += chunk;
    const out: SseMessage[] = [];
    let newline: number;
    while ((newline = this.buffer.search(/\r\n|\r|\n/)) !== -1) {
      // A "\r" ending the chunk may be the first half of "\r\n": wait for the next chunk to know.
      if (newline === this.buffer.length - 1 && this.buffer.endsWith("\r")) break;
      const line = this.buffer.slice(0, newline);
      const width = this.buffer.startsWith("\r\n", newline) ? 2 : 1;
      this.buffer = this.buffer.slice(newline + width);
      if (line === "") {
        if (this.data.length > 0) out.push({ id: this.id, data: this.data.join("\n") });
        this.data = [];
        this.id = null;
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "data") this.data.push(value);
      else if (field === "id") this.id = value;
    }
    return out;
  }
}

/** Opens one HTTP connection to the stream. Tests pass a fake; the default sends the user's bearer token. */
export type StreamFetcher = (url: string, signal: AbortSignal) => Promise<Response>;

const defaultFetcher: StreamFetcher = async (url, signal) => {
  const token = await getAccessToken();
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (token) headers.Authorization = `Bearer ${token}`;
  return fetch(url, { headers, signal, cache: "no-store" });
};

export interface StreamOptions {
  onEvent: (event: RunEvent) => void;
  onStatus?: (status: StreamStatus) => void;
  /** Called once when we stop trying, with a plain-English reason. */
  onGiveUp?: (message: string) => void;
  /** Asked when the server ends the stream cleanly: true means the run is over, so don't reconnect. */
  isRunOver?: () => Promise<boolean>;
  maxRetries?: number;
  baseUrl?: string;
  /** Delay before reopening a dropped connection. */
  retryMs?: number;
  fetcher?: StreamFetcher;
}

export interface Stream {
  close(): void;
}

const LOST = "We lost the connection to this run and couldn't get it back. Please try again in a minute.";
const NOT_FOUND = "We couldn't find this run. It may belong to another account.";
const SIGNED_OUT = "Your session has ended. Please log in again to keep watching this run.";

/**
 * Live stream of GET /runs/{id}/events. The API needs a bearer token, which EventSource can't send, so we
 * read the SSE body with fetch. On a dropped connection we reopen with `?after=<last seq>` so the server
 * resumes right after the last event we saw; duplicates are dropped by seq regardless.
 */
export function openRunStream(runId: string, opts: StreamOptions): Stream {
  const {
    onEvent,
    onStatus,
    onGiveUp,
    isRunOver,
    maxRetries = 6,
    baseUrl = API_URL,
    retryMs = 1500,
    fetcher = defaultFetcher,
  } = opts;
  let lastSeq = 0;
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let retries = 0;

  const giveUp = (message: string) => {
    stop();
    onGiveUp?.(message);
  };

  const retry = () => {
    if (closed) return;
    if (retries >= maxRetries) return giveUp(LOST);
    onStatus?.("reconnecting");
    timer = setTimeout(() => void connect(), retryMs * 2 ** retries++); // backoff: 1.5s, 3s, 6s…
  };

  const handle = (msg: SseMessage) => {
    let event: RunEvent;
    try {
      event = JSON.parse(msg.data) as RunEvent;
    } catch {
      return; // a malformed frame must not kill the stream
    }
    retries = 0;
    if (typeof event.seq !== "number" || event.seq <= lastSeq) return; // dedupe by seq
    lastSeq = event.seq;
    onEvent(event);
    if (event.head === "system" && (event.type === "finished" || event.type === "error")) stop(); // run is over
  };

  const connect = async () => {
    if (closed) return;
    const mine = new AbortController();
    controller = mine;
    const query = lastSeq > 0 ? `?after=${lastSeq}` : "";
    let response: Response;
    try {
      response = await fetcher(`${baseUrl}/runs/${encodeURIComponent(runId)}/events${query}`, mine.signal);
    } catch {
      return retry(); // network error, or our own abort (retry() is a no-op once closed)
    }
    if (closed) return;
    if (response.status === 401) return giveUp(SIGNED_OUT);
    if (response.status === 404) return giveUp(NOT_FOUND);
    if (!response.ok || !response.body) return retry();
    retries = 0;
    onStatus?.("live");
    const parser = new SseParser();
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        for (const msg of parser.push(value)) {
          handle(msg);
          if (closed) return;
        }
      }
    } catch {
      return retry(); // the connection dropped mid-stream
    }
    if (closed) return;
    // The server ends the stream once the run is over and fully sent, or after an hour (then we reconnect).
    let over = false;
    try {
      over = isRunOver ? await isRunOver() : false;
    } catch {
      over = false;
    }
    if (over) stop();
    else retry();
  };

  const stop = () => {
    if (closed) return;
    closed = true;
    if (timer) clearTimeout(timer);
    controller?.abort();
    onStatus?.("closed");
  };

  onStatus?.("connecting");
  void connect();
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
    const event = events[index++];
    onEvent(event);
    // Linger on an approval request so a viewer can read the card before the replay answers it.
    timer = setTimeout(tick, event.type === "approval_needed" ? intervalMs * 4 : intervalMs);
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
