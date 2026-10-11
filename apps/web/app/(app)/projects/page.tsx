import Link from "next/link";
import { Plus } from "lucide-react";
import { ProjectList } from "@/components/app/project-list";

export default function Page() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12">
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Your projects</h1>
          <p className="mt-1 text-sm text-muted-foreground">Each project is an agent Susanoo builds, tests and fixes for you.</p>
        </div>
        <Link
          href="/projects/new"
          className="inline-flex min-h-11 items-center gap-2 rounded-lg bg-foreground px-4 text-sm font-medium text-background hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
        >
          <Plus className="size-4" aria-hidden /> New project
        </Link>
      </div>
      <ProjectList />
    </main>
  );
}
