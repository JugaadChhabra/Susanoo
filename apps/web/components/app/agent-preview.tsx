"use client";

import { useState } from "react";
import { Code, ExternalLink } from "lucide-react";
import { resolvePreviewUrl, safePreviewUrl } from "@/lib/agent";
import { getAgentRunInfo } from "@/lib/agent-api";
import { useAsync } from "@/lib/use-async";
import { PanelError, PanelShell } from "./agent-panels";

const linkClass =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** Live preview of the generated app plus the repo link. The iframe is sandboxed so the app can't touch ours. */
export function AgentPreview({ runId }: { runId: string }) {
  const state = useAsync(() => getAgentRunInfo(runId), runId);
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null);

  return (
    <PanelShell title="Live preview">
      {state.status === "loading" && <div className="h-64 animate-pulse rounded-xl bg-muted" aria-busy="true" aria-label="Loading the preview" />}
      {state.status === "error" && <PanelError message={state.message} onRetry={state.retry} />}
      {state.status === "ready" && (() => {
        const previewUrl = resolvePreviewUrl(runId, state.data);
        const repoUrl = safePreviewUrl(state.data.repo_url);
        return (
          <div className="space-y-3">
            {previewUrl ? (
              <>
                <div className="relative">
                  {loadedUrl !== previewUrl && <div className="absolute inset-0 animate-pulse rounded-xl bg-muted" aria-hidden />}
                  <iframe
                    key={previewUrl}
                    src={previewUrl}
                    title="Live preview of your agent"
                    sandbox="allow-scripts allow-forms"
                    referrerPolicy="no-referrer"
                    loading="lazy"
                    onLoad={() => setLoadedUrl(previewUrl)}
                    className="relative h-80 w-full rounded-xl border border-border bg-background sm:h-96"
                  />
                </div>
                <p className="text-xs text-muted-foreground">If the preview stays blank, the app may still be starting. Open it in its own tab instead.</p>
              </>
            ) : (
              <p className="rounded-xl border border-dashed border-border p-4 text-sm text-muted-foreground">
                The preview isn&apos;t ready yet. It appears once your agent has been built and started.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {previewUrl && (
                <a href={previewUrl} target="_blank" rel="noopener noreferrer" className={linkClass}>
                  <ExternalLink className="size-4" aria-hidden /> Open in a new tab
                </a>
              )}
              {repoUrl ? (
                <a href={repoUrl} target="_blank" rel="noopener noreferrer" className={linkClass}>
                  <Code className="size-4" aria-hidden /> View the code
                </a>
              ) : (
                <span className="self-center text-sm text-muted-foreground">The code link appears once the agent is saved.</span>
              )}
            </div>
          </div>
        );
      })()}
    </PanelShell>
  );
}
