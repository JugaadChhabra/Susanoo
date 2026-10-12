import { Suspense } from "react";
import { LoginForm } from "@/components/auth/login-form";

export default function Page() {
  return (
    <div className="app-stamp stamp-palette dots flex min-h-full flex-1 flex-col">
    <main className="mx-auto w-full max-w-sm px-4 py-12">
      <h1 className="text-2xl font-semibold">Log in to Susanoo</h1>
      <p className="mt-2 text-muted-foreground">
        New here? Use either option and we&apos;ll create your account.
      </p>
      <Suspense fallback={<p className="mt-6 text-sm text-muted-foreground">Loading…</p>}>
        <LoginForm />
      </Suspense>
    </main>
    </div>
  );
}
