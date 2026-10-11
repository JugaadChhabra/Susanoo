"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Loader2, RotateCw } from "lucide-react";
import { createProject, listConnectors } from "@/lib/api";
import { connectionLabel, connectionStatusLabel } from "@/lib/connectors";
import { EXAMPLE_SPECS, parseRules, rulesProblem, rulesToText, type ExampleSpec } from "@/lib/projects";
import { useAsync } from "@/lib/use-async";
import { cn } from "@/lib/utils";
import type { ConnectionSummary, ConnectorInfo } from "@/types/generated";

const field = "mt-1 w-full rounded-lg border border-border bg-background p-3 text-sm";

/** Spec, rules as sentences, connector picker, examples, Start. [PRD › Frontend and API › Screens › New project] */
export function NewProjectForm() {
  const router = useRouter();
  const [example, setExample] = useState<ExampleSpec>(EXAMPLE_SPECS[0]);
  const [spec, setSpec] = useState(example.spec);
  const [rules, setRules] = useState(rulesToText(example.rules));
  const [connectors, setConnectors] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pickExample(next: ExampleSpec) {
    const changed = spec !== example.spec || rules !== rulesToText(example.rules);
    if (changed && !window.confirm("Replace what you've written with this example?")) return;
    setExample(next);
    setSpec(next.spec);
    setRules(rulesToText(next.rules));
    setError(null);
  }

  async function start(e: FormEvent) {
    e.preventDefault();
    if (!spec.trim()) {
      setError("Describe what you want to build first.");
      return;
    }
    const parsed = parseRules(rules);
    const problem = rulesProblem(parsed);
    if (problem) {
      setError(problem);
      return;
    }
    setSending(true);
    setError(null);
    try {
      await createProject({ spec: spec.trim(), rules: parsed });
      router.push("/projects");
    } catch (err) {
      setError(err instanceof Error ? err.message : "We couldn't create your project.");
      setSending(false);
    }
  }

  return (
    <form onSubmit={start} className="space-y-6">
      <fieldset>
        <legend className="text-sm font-medium">Start from an example</legend>
        <div className="mt-2 flex flex-wrap gap-2">
          {EXAMPLE_SPECS.map((ex) => (
            <button
              key={ex.title}
              type="button"
              aria-pressed={ex === example}
              onClick={() => pickExample(ex)}
              className={cn(
                "min-h-10 rounded-full border px-4 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
                ex === example ? "border-foreground bg-foreground text-background" : "border-border hover:bg-muted",
              )}
            >
              {ex.title}
            </button>
          ))}
        </div>
      </fieldset>

      <div>
        <label htmlFor="spec" className="text-sm font-medium">
          What should Susanoo build?
        </label>
        <p className="text-sm text-muted-foreground">Describe the product and its agent in plain English.</p>
        <textarea id="spec" value={spec} onChange={(e) => setSpec(e.target.value)} rows={6} required maxLength={20_000} className={field} />
      </div>

      <div>
        <label htmlFor="rules" className="text-sm font-medium">
          Rules
        </label>
        <p className="text-sm text-muted-foreground">
          One sentence per line, for example &ldquo;Never let anyone log customer emails.&rdquo;
        </p>
        <textarea id="rules" value={rules} onChange={(e) => setRules(e.target.value)} rows={6} className={field} />
      </div>

      <ConnectorPicker picked={connectors} onChange={setConnectors} />

      {error && (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={sending}
        className="inline-flex min-h-11 w-full items-center justify-center gap-2 rounded-lg bg-foreground px-5 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-60 sm:w-auto"
      >
        {sending && <Loader2 className="size-4 animate-spin" aria-hidden />} Start
      </button>
    </form>
  );
}

/** The user's real connections to pick from. [PRD › Frontend and API › Screens › New project] */
function ConnectorPicker({ picked, onChange }: { picked: string[]; onChange: (ids: string[]) => void }) {
  const state = useAsync(listConnectors, "connectors");
  const connected =
    state.status === "ready"
      ? state.data.filter((c): c is ConnectorInfo & { connection: ConnectionSummary } => c.connection !== null)
      : [];

  return (
    <fieldset>
      <legend className="text-sm font-medium">Accounts your agent may use (optional)</legend>
      <p className="text-sm text-muted-foreground">Coming soon: your choice here isn&rsquo;t saved with the project yet.</p>
      {state.status === "loading" && (
        <div className="mt-2 h-14 animate-pulse rounded-lg bg-muted" aria-busy="true" aria-label="Loading your connections" />
      )}
      {state.status === "error" && (
        <p role="alert" className="mt-2 flex flex-wrap items-center gap-2 text-sm text-red-600">
          {state.message}
          <button
            type="button"
            onClick={state.retry}
            className="inline-flex min-h-9 items-center gap-1 rounded-lg px-2 text-foreground underline underline-offset-2 hover:bg-muted focus-visible:outline-2 focus-visible:outline-ring"
          >
            <RotateCw className="size-3.5" aria-hidden /> Try again
          </button>
        </p>
      )}
      {state.status === "ready" && connected.length === 0 && (
        <p className="mt-2 rounded-lg border border-dashed border-border p-3 text-sm text-muted-foreground">
          You haven&rsquo;t connected any accounts yet.{" "}
          <Link href="/connectors" className="font-medium text-foreground underline underline-offset-2">
            Connect GitHub
          </Link>{" "}
          so your agent can read real pull requests.
        </p>
      )}
      {connected.length > 0 && (
        <div className="mt-2 space-y-2">
          {connected.map(({ spec, connection }) => (
            <label
                key={connection.id}
                className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm"
              >
                <input
                  type="checkbox"
                  className="mt-0.5 size-4"
                  checked={picked.includes(connection.id)}
                  onChange={(e) =>
                    onChange(e.target.checked ? [...picked, connection.id] : picked.filter((id) => id !== connection.id))
                  }
                />
                <span className="min-w-0">
                  <span className="font-medium break-words">{connectionLabel(spec, connection)}</span>
                  <span className="block text-muted-foreground">
                    {connectionStatusLabel(connection.status)} · {spec.summary}
                  </span>
                </span>
              </label>
          ))}
          <Link href="/connectors" className="inline-block text-sm underline underline-offset-2">
            Manage connections
          </Link>
        </div>
      )}
    </fieldset>
  );
}
