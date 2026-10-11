import { AgentCard, ReportCard } from "@/components/app/agent-panels";
import { AgentPreview } from "@/components/app/agent-preview";
import { TryIt } from "@/components/app/try-it";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main className="mx-auto w-full max-w-3xl space-y-4 px-4 py-8 sm:py-12">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Your agent</h1>
        <p className="mt-1 text-sm text-muted-foreground">What it does, how well it does it, and a place to try it. Feedback you give here becomes a new test.</p>
      </header>
      <AgentCard runId={id} />
      <ReportCard runId={id} />
      <AgentPreview runId={id} />
      <TryIt runId={id} />
    </main>
  );
}
