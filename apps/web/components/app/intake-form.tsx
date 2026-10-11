"use client";

import { useState } from "react";
import { Loader2 } from "lucide-react";
import { capQuestions, initialAnswers, type IntakeQuestion } from "@/lib/intake";
import { cn } from "@/lib/utils";

/** Up to five questions, each with a suggested default and one line on why it matters. */
export function IntakeForm({
  questions,
  onSubmit,
}: {
  questions: IntakeQuestion[];
  onSubmit: (answers: Record<string, string>) => Promise<void>;
}) {
  const shown = capQuestions(questions);
  const [answers, setAnswers] = useState(() => initialAnswers(shown));
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSending(true);
    setError(null);
    try {
      await onSubmit(answers);
    } catch (err) {
      setError(err instanceof Error ? err.message : "We couldn't send your answers.");
      setSending(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-6">
      {shown.map((q, i) => (
        <fieldset key={q.id} className="rounded-xl border border-border p-4">
          <legend className="px-1 text-sm font-medium text-muted-foreground">
            Question {i + 1} of {shown.length}
          </legend>
          <p className="font-medium">{q.text}</p>
          <p className="mt-1 text-sm text-muted-foreground">{q.why}</p>
          {q.options.length > 0 ? (
            <div className="mt-3 space-y-2">
              {q.options.map((opt) => (
                <label
                  key={opt}
                  className={cn(
                    "flex min-h-11 cursor-pointer items-center gap-3 rounded-lg border px-3 py-2 text-sm has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-ring",
                    answers[q.id] === opt ? "border-foreground bg-muted" : "border-border",
                  )}
                >
                  <input
                    type="radio"
                    name={q.id}
                    value={opt}
                    checked={answers[q.id] === opt}
                    onChange={() => setAnswers((a) => ({ ...a, [q.id]: opt }))}
                    className="size-4"
                  />
                  <span className="flex-1">{opt}</span>
                  {opt === q.default && <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">Suggested</span>}
                </label>
              ))}
            </div>
          ) : (
            <label className="mt-3 block text-sm">
              <span className="sr-only">Your answer</span>
              <input
                value={answers[q.id] ?? ""}
                onChange={(e) => setAnswers((a) => ({ ...a, [q.id]: e.target.value }))}
                className="min-h-11 w-full rounded-lg border border-border bg-background px-3"
              />
              {q.default && <span className="mt-1 block text-xs text-muted-foreground">Suggested: {q.default}</span>}
            </label>
          )}
        </fieldset>
      ))}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      <button
        type="submit"
        disabled={sending}
        className="inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60 sm:w-auto"
      >
        {sending && <Loader2 className="size-4 animate-spin" aria-hidden />}
        {sending ? "Sending…" : "Continue with these answers"}
      </button>
    </form>
  );
}
