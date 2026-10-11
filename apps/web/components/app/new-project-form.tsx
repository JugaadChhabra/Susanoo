"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { Loader2 } from "lucide-react";
import { createProject } from "@/lib/api";
import { EXAMPLE_SPECS, parseRules, rulesToText, type ExampleSpec } from "@/lib/projects";
import { cn } from "@/lib/utils";

// Live connectors per [PRD › Connectors and API budget]. Stubbed: picking one isn't saved yet.
const CONNECTORS = [
  { id: "github", name: "GitHub", what: "Lets your agent read pull requests." },
  { id: "anthropic", name: "Anthropic", what: "Use your own Claude key instead of Susanoo's daily allowance." },
];

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
    setSending(true);
    setError(null);
    try {
      await createProject({ spec: spec.trim(), rules: parseRules(rules) });
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
        <textarea id="spec" value={spec} onChange={(e) => setSpec(e.target.value)} rows={6} required className={field} />
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

      <fieldset>
        <legend className="text-sm font-medium">Connect your accounts (optional)</legend>
        <p className="text-sm text-muted-foreground">Coming soon: your choice here isn&rsquo;t saved yet.</p>
        <div className="mt-2 space-y-2">
          {CONNECTORS.map((c) => (
            <label key={c.id} className="flex min-h-11 cursor-pointer items-start gap-3 rounded-lg border border-border p-3 text-sm">
              <input
                type="checkbox"
                className="mt-0.5 size-4"
                checked={connectors.includes(c.id)}
                onChange={(e) =>
                  setConnectors((prev) => (e.target.checked ? [...prev, c.id] : prev.filter((id) => id !== c.id)))
                }
              />
              <span>
                <span className="font-medium">{c.name}</span>
                <span className="block text-muted-foreground">{c.what}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>

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
