import { createBrowserClient } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

let client: SupabaseClient | null = null;

/** True when the public Supabase settings are present. Login shows a plain message when they aren't. */
export const supabaseConfigured = () =>
  Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY);

/** Browser-side Supabase client (session lives in cookies so the proxy can see it). */
export function getSupabase(): SupabaseClient {
  client ??= createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
  );
  return client;
}

/** The logged-in user's access token for the API, or null when logged out or not configured. */
export async function getAccessToken(): Promise<string | null> {
  if (typeof window === "undefined" || !supabaseConfigured()) return null;
  const { data } = await getSupabase().auth.getSession();
  return data.session?.access_token ?? null;
}
