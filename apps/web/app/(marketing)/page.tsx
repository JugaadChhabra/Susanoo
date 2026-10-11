import { HealthCheck } from "@/components/health-check";

export default function Page() {
  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-12">
      <h1 className="text-3xl font-semibold">Susanoo</h1>
      <p className="mt-2 mb-6 text-muted-foreground">
        Describe a product in plain English. Susanoo builds it, attacks it, fixes it, and proves
        the fix.
      </p>
      <HealthCheck />
    </main>
  );
}
