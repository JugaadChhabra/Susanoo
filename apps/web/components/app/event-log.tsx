"use client";

import { memo, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Check, ChevronDown, CircleHelp, ExternalLink, GitCommit, Hand, Play, Activity } from "lucide-react";
import type { RunEvent } from "@/types/generated";
import { cn } from "@/lib/utils";
import { HEADS, commitLinks, findRepoUrl, safeHref } from "@/lib/run-state";

const HEAD_LABEL: Record<string, string> = {
  system: "Susanoo",
  ...Object.fromEntries(HEADS.map((h) => [h.id, h.label])),
};

const TYPE_STYLE: Record<RunEvent["type"], { icon: typeof Check; dot: string }> = {
  started: { icon: Play, dot: "bg-indigo-500/20 text-indigo-300" },
  progress: { icon: Activity, dot: "bg-slate-700/60 text-slate-300" },
  finished: { icon: Check, dot: "bg-emerald-500/20 text-emerald-300" },
  error: { icon: AlertTriangle, dot: "bg-red-500/20 text-red-300" },
  question_needed: { icon: CircleHelp, dot: "bg-sky-500/20 text-sky-300" },
  approval_needed: { icon: Hand, dot: "bg-amber-500/20 text-amber-300" },
  metrics: { icon: Activity, dot: "bg-violet-500/20 text-violet-300" },
};

function formatTime(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

const EventRow = memo(function EventRow({ event, repo }: { event: RunEvent; repo: string | null }) {
  const [open, setOpen] = useState(false);
  const { icon: Icon, dot } = TYPE_STYLE[event.type];
  const trace = safeHref(event.trace_url);
  const commits = commitLinks(event, repo);
  const hasDetails = Object.keys(event.payload).length > 0;

  return (
    <li className="flex gap-3 px-4 py-3 animate-in fade-in slide-in-from-bottom-1 duration-300">
      <span className={cn("mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full", dot)}>
        <Icon className="size-3.5" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-xs font-semibold text-slate-200">{HEAD_LABEL[event.head] ?? event.head}</span>
          <time dateTime={event.at} className="text-[11px] text-slate-500">
            {formatTime(event.at)}
          </time>
        </div>
        <p className={cn("mt-0.5 break-words text-sm", event.type === "error" ? "text-red-200" : "text-slate-300")}>
          {event.message}
        </p>
        {(trace || commits.length > 0 || hasDetails) && (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
            {trace && (
              <a
                href={trace}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-indigo-300 hover:text-indigo-200 focus-visible:outline-2 focus-visible:outline-indigo-400"
              >
                <ExternalLink className="size-3" aria-hidden /> View trace
              </a>
            )}
            {commits.map((c) => (
              <a
                key={c.href}
                href={c.href}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-emerald-300 hover:text-emerald-200 focus-visible:outline-2 focus-visible:outline-emerald-400"
              >
                <GitCommit className="size-3" aria-hidden /> {c.label}
              </a>
            ))}
            {hasDetails && (
              <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                className="inline-flex items-center gap-1 text-slate-400 hover:text-slate-200 focus-visible:outline-2 focus-visible:outline-indigo-400"
              >
                <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} aria-hidden /> Details
              </button>
            )}
          </div>
        )}
        {open && (
          <pre className="mt-2 max-h-56 overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-400">
            {JSON.stringify({ seq: event.seq, type: event.type, payload: event.payload }, null, 2)}
          </pre>
        )}
      </div>
    </li>
  );
});

export interface EventLogProps {
  events: RunEvent[];
  loading: boolean;
  error: string | null;
  onRetry?: () => void;
}

export function EventLog({ events, loading, error, onRetry }: EventLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const repo = useMemo(() => findRepoUrl(events), [events]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [events.length]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
  };

  if (error) {
    return (
      <div role="alert" className="flex flex-col items-start gap-3 p-6 text-sm text-red-200">
        <p>{error}</p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="rounded-md bg-red-500/20 px-3 py-1.5 font-medium hover:bg-red-500/30 focus-visible:outline-2 focus-visible:outline-red-400"
          >
            Try again
          </button>
        )}
      </div>
    );
  }

  if (events.length === 0) {
    return loading ? (
      <div className="space-y-4 p-4" aria-busy="true" aria-label="Loading run activity">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex gap-3">
            <div className="size-6 animate-pulse rounded-full bg-slate-800" />
            <div className="flex-1 space-y-2">
              <div className="h-3 w-24 animate-pulse rounded bg-slate-800" />
              <div className="h-3 w-3/4 animate-pulse rounded bg-slate-800" />
            </div>
          </div>
        ))}
      </div>
    ) : (
      <p className="p-6 text-sm text-slate-400">Nothing has happened yet. Updates will show up here as soon as the run starts.</p>
    );
  }

  return (
    <div ref={scrollRef} onScroll={onScroll} className="max-h-[28rem] overflow-y-auto" role="log" aria-live="polite">
      <ul className="divide-y divide-slate-800/70">
        {events.map((e) => (
          <EventRow key={e.seq} event={e} repo={repo} />
        ))}
      </ul>
    </div>
  );
}
