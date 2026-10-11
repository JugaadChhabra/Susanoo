import { NewProjectForm } from "@/components/app/new-project-form";

export default function Page() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:py-12">
      <h1 className="text-2xl font-semibold tracking-tight">New project</h1>
      <p className="mt-1 mb-6 text-sm text-muted-foreground">
        Describe the product you want in plain English. The code reviewer example is filled in to get you started.
      </p>
      <NewProjectForm />
    </main>
  );
}
