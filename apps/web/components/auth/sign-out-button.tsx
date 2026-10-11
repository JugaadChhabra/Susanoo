"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { getSupabase } from "@/lib/supabase";

export function SignOutButton() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function signOut() {
    setBusy(true);
    try {
      const { error } = await getSupabase().auth.signOut();
      if (error) throw error;
      router.push("/login");
      router.refresh();
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2">
      {failed && (
        <span role="alert" className="text-xs text-destructive">
          Couldn&apos;t sign out. Try again.
        </span>
      )}
      <Button variant="ghost" size="sm" onClick={signOut} disabled={busy}>
        {busy ? "Signing out…" : "Sign out"}
      </Button>
    </div>
  );
}
