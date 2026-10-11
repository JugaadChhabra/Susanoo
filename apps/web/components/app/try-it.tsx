"use client";

import { useRef, useState } from "react";
import { Loader2, ThumbsDown, ThumbsUp } from "lucide-react";
import { MAX_DIFF_CHARS, toFeedback, toTryInput, type Rating, type TryResult } from "@/lib/agent";
import { submitFeedback, tryAgent } from "@/lib/agent-api";
import { PanelShell } from "./agent-panels";

type Mode = "diff" | "pr";
type FeedbackState = "idle" | "sending" | "sent";

const focus = "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";
const field = `w-full rounded-lg border border-border bg-background p-3 text-sm ${focus}`;

/** Try the agent on a diff or PR, then rate what it said. A thumbs down needs an "it should have..." note. */
export function TryIt({ runId }: { runId: string }) {
  const [mode, setMode] = useState<Mode>("diff");
  const [text, setText] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<TryResult | null>(null);
  const [triedDiff, setTriedDiff] = useState<string | undefined>();
  const [rating, setRating] = useState<Rating | null>(null);
  const [expectation, setExpectation] = useState("");
  const [feedback, setFeedback] = useState<FeedbackState>("idle");
  const [feedbackError, setFeedbackError] = useState<string | null>(null);
  const requestId = useRef(0); // ignore answers to a review the user has already moved past

  const input = toTryInput(mode, text);
  const tooLong = mode === "diff" && text.trim().length > MAX_DIFF_CHARS;

  async function run() {
    if (!input) return;
    const mine = ++requestId.current;
    setRunning(true);
    setTriedDiff(undefined);
    setError(null);
    setResult(null);
    setRating(null);
    setExpectation("");
    setFeedback("idle");
    setFeedbackError(null);
    try {
      const next = await tryAgent(runId, input);
      if (mine !== requestId.current) return;
      setResult(next);
      setTriedDiff(input.kind === "diff" ? input.diff : undefined);
    } catch (e) {
      if (mine === requestId.current) setError(e instanceof Error ? e.message : "We couldn't run your agent.");
    } finally {
      if (mine === requestId.current) setRunning(false);
    }
  }

  async function send(next: Rating) {
    const payload = toFeedback(next, expectation, triedDiff);
    if (!payload) return; // a thumbs down waits for its note
    setFeedback("sending");
    setFeedbackError(null);
    try {
      await submitFeedback(runId, payload);
      setFeedback("sent");
    } catch (e) {
      setFeedbackError(e instanceof Error ? e.message : "We couldn't send your feedback.");
      setFeedback("idle");
    }
  }

  function pick(next: Rating) {
    setRating(next);
    setFeedbackError(null);
    if (next === "up") void send("up");
  }

  return (
    <PanelShell title="Try it">
      <div className="space-y-3">
        <div role="group" aria-label="What to try" className="inline-flex rounded-lg border border-border p-0.5">
          {(["diff", "pr"] as const).map((m) => (
            <button
              key={m}
              type="button"
              aria-pressed={mode === m}
              onClick={() => {
                requestId.current++;
                setRunning(false);
                setResult(null);
                setError(null);
                setMode(m);
                setText("");
              }}
              className={`min-h-9 rounded-md px-3 text-sm ${focus} ${mode === m ? "bg-foreground text-background" : "hover:bg-muted"}`}
            >
              {m === "diff" ? "Paste a diff" : "Pick a PR"}
            </button>
          ))}
        </div>
        {mode === "diff" ? (
          <label className="block text-sm">
            <span className="sr-only">Code change to review</span>
            <textarea value={text} onChange={(e) => setText(e.target.value)} rows={6} placeholder="Paste a code change (a git diff) here" className={`${field} font-mono`} />
          </label>
        ) : (
          <label className="block text-sm">
            <span className="sr-only">Pull request</span>
            <input value={text} onChange={(e) => setText(e.target.value)} placeholder="https://github.com/owner/repo/pull/12 or owner/repo#12" className={`${field} min-h-11`} />
          </label>
        )}
        {tooLong && <p role="alert" className="text-xs text-red-600">That change is too long. Keep it under {MAX_DIFF_CHARS.toLocaleString()} characters.</p>}
        {mode === "pr" && text.trim() !== "" && !input && <p role="alert" className="text-xs text-red-600">Use a GitHub pull request link, or owner/repo#number.</p>}
        <button
          type="button"
          disabled={!input || running}
          onClick={run}
          className={`inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 disabled:opacity-50 sm:w-auto ${focus}`}
        >
          {running && <Loader2 className="size-4 animate-spin" aria-hidden />}
          {running ? "Reviewing…" : "Review this change"}
        </button>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}

        {result && (
          <div className="space-y-3 border-t border-border pt-3" aria-live="polite">
            <p className="text-sm font-medium">{result.summary}</p>
            {result.findings.length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing was flagged.</p>
            ) : (
              <ul className="space-y-2">
                {result.findings.map((f, i) => (
                  <li key={i} className="rounded-lg bg-muted p-3 text-sm">
                    <p>{f.text}</p>
                    <p className="mt-1 text-xs text-muted-foreground">Why: {f.why}</p>
                  </li>
                ))}
              </ul>
            )}

            {feedback === "sent" ? (
              <p role="status" className="text-sm text-muted-foreground">
                {rating === "down" ? "Thanks. We'll turn this into a new test and improve your agent." : "Thanks for the feedback."}
              </p>
            ) : (
              <div className="space-y-2">
                <p className="text-sm">Was this review right?</p>
                <div className="flex gap-2">
                  {([["up", ThumbsUp, "Yes, good review"], ["down", ThumbsDown, "No, it missed something"]] as const).map(([r, Icon, label]) => (
                    <button
                      key={r}
                      type="button"
                      aria-label={label}
                      aria-pressed={rating === r}
                      disabled={feedback === "sending"}
                      onClick={() => pick(r)}
                      className={`inline-flex size-11 items-center justify-center rounded-lg border border-border disabled:opacity-50 ${focus} ${rating === r ? "bg-foreground text-background" : "hover:bg-muted"}`}
                    >
                      <Icon className="size-4" aria-hidden />
                    </button>
                  ))}
                </div>
                {rating === "down" && (
                  <div className="space-y-2">
                    <label className="block text-sm">
                      It should have…
                      <textarea value={expectation} onChange={(e) => setExpectation(e.target.value)} rows={3} className={`${field} mt-1`} placeholder="e.g. flagged the password written to the log" />
                    </label>
                    <button
                      type="button"
                      disabled={!expectation.trim() || feedback === "sending"}
                      onClick={() => void send("down")}
                      className={`inline-flex min-h-11 items-center gap-2 rounded-lg border border-border px-4 text-sm hover:bg-muted disabled:opacity-50 ${focus}`}
                    >
                      {feedback === "sending" && <Loader2 className="size-4 animate-spin" aria-hidden />}
                      Send feedback
                    </button>
                  </div>
                )}
                {feedbackError && <p role="alert" className="text-sm text-red-600">{feedbackError}</p>}
              </div>
            )}
          </div>
        )}
      </div>
    </PanelShell>
  );
}
