"use client";

import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { safeNext } from "@/lib/auth";
import { getSupabase, supabaseConfigured } from "@/lib/supabase";

type Status = "idle" | "sending" | "sent" | "error";

function callbackUrl(next: string) {
  return `${window.location.origin}/auth/callback?next=${encodeURIComponent(next)}`;
}

export function LoginForm() {
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const [email, setEmail] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [message, setMessage] = useState(
    params.get("error") ? "That sign-in link didn't work. Please try again." : "",
  );

  // Coming back from GitHub with the Back button restores this page mid-"sending".
  useEffect(() => {
    const reset = (e: PageTransitionEvent) => e.persisted && setStatus("idle");
    window.addEventListener("pageshow", reset);
    return () => window.removeEventListener("pageshow", reset);
  }, []);

  if (!supabaseConfigured()) {
    return (
      <p role="alert" className="mt-6 text-sm text-destructive">
        Sign-in isn&apos;t set up on this site yet.
      </p>
    );
  }

  async function sendLink(e: React.FormEvent) {
    e.preventDefault();
    setStatus("sending");
    setMessage("");
    const { error } = await getSupabase()
      .auth.signInWithOtp({ email: email.trim(), options: { emailRedirectTo: callbackUrl(next) } })
      .catch(() => ({ error: true }));
    if (error) {
      setStatus("error");
      setMessage("We couldn't send the link. Check the email address and try again.");
    } else {
      setStatus("sent");
    }
  }

  async function github() {
    setStatus("sending");
    setMessage("");
    const { error } = await getSupabase()
      .auth.signInWithOAuth({ provider: "github", options: { redirectTo: callbackUrl(next) } })
      .catch(() => ({ error: true }));
    if (error) {
      setStatus("error");
      setMessage("We couldn't start the GitHub sign-in. Please try again.");
    }
  }

  const busy = status === "sending";

  if (status === "sent") {
    return (
      <p role="status" className="mt-6 text-sm">
        Check your inbox: we sent a sign-in link to <strong>{email}</strong>.
      </p>
    );
  }

  return (
    <div className="mt-6 space-y-4">
      <form onSubmit={sendLink} className="space-y-3">
        <label htmlFor="email" className="block text-sm font-medium">
          Email
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={busy}
          placeholder="you@example.com"
          className="h-10 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
        />
        <Button type="submit" disabled={busy || !email.trim()} className="w-full">
          {busy ? "Sending…" : "Email me a sign-in link"}
        </Button>
      </form>
      <div className="text-center text-xs text-muted-foreground">or</div>
      <Button type="button" variant="outline" onClick={github} disabled={busy} className="w-full">
        Continue with GitHub
      </Button>
      {message && (
        <p role="alert" className="text-sm text-destructive">
          {message}
        </p>
      )}
    </div>
  );
}
