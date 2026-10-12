"use client";

import { useState } from "react";
import { Code, ExternalLink, Moon, RotateCw } from "lucide-react";
import { PREVIEW_ASLEEP, probePreview, resolvePreviewUrl, safePreviewUrl } from "@/lib/agent";
import { getAgentRunInfo } from "@/lib/agent-api";
import { useAsync } from "@/lib/use-async";
import { PanelError, PanelShell } from "./agent-panels";

const linkClass =
  "inline-flex min-h-10 items-center gap-1.5 rounded-lg border border-border px-3 text-sm hover:bg-muted focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";

/** Live preview of the generated app plus the repo link. The iframe is sandboxed so the app can't touch ours. */
export function AgentPreview({ runId }: { runId: string }) {
  const state = useAsync(() => getAgentRunInfo(runId), runId);

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
              <PreviewFrame key={previewUrl} url={previewUrl} />
            ) : (
              <p className="rounded-xl border border-dashed border-border p-4 text-sm text-muted-foreground">
                The preview isn&apos;t ready yet. It appears once your agent has been built and started.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
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

/** Checks the preview answers before framing it, so a stopped app reads as asleep rather than a blank frame. */
function PreviewFrame({ url }: { url: string }) {
  const health = useAsync(() => probePreview(url), url);
  const [loaded, setLoaded] = useState(false);

  if (health.status === "loading") {
    return <div className="h-64 animate-pulse rounded-xl bg-muted" aria-busy="true" aria-label="Checking whether your agent is awake" />;
  }
  if (health.status === "ready" && health.data === "asleep") {
    return (
      <div role="status" className="rounded-xl border border-dashed border-border p-4">
        <p className="flex items-center gap-2 font-medium">
          <Moon className="size-4" aria-hidden /> {PREVIEW_ASLEEP}
        </p>
        <p className="mt-1 text-sm text-muted-foreground">
          Previews go to sleep after a while with no visitors. Your agent, its report card and its code are all still here.
        </p>
        <p className="mt-2 text-sm break-all text-muted-foreground">Its address was {url}</p>
        <button type="button" onClick={health.retry} className={`mt-3 ${linkClass}`}>
          <RotateCw className="size-4" aria-hidden /> Check again
        </button>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="relative">
        {!loaded && <div className="absolute inset-0 animate-pulse rounded-xl bg-muted" aria-hidden />}
        <iframe
          src={url}
          title="Live preview of your agent"
          sandbox="allow-scripts allow-forms"
          referrerPolicy="no-referrer"
          loading="lazy"
          onLoad={() => setLoaded(true)}
          className="relative h-80 w-full rounded-xl border border-border bg-background sm:h-96"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <a href={url} target="_blank" rel="noopener noreferrer" className={linkClass}>
          <ExternalLink className="size-4" aria-hidden /> Open in a new tab
        </a>
        <span className="text-sm break-all text-muted-foreground">{url}</span>
      </div>
    </div>
  );
}
