import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";
import { safeNext } from "@/lib/auth";

/** Magic links and GitHub OAuth both land here with `?code=`; we swap it for a session cookie. */
export async function GET(request: NextRequest) {
  const { searchParams, origin } = request.nextUrl;
  const code = searchParams.get("code");
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  const next = safeNext(searchParams.get("next"));
  const failed = NextResponse.redirect(`${origin}/login?error=callback&next=${encodeURIComponent(next)}`);
  if (!code || !url || !key) return failed;

  const jar = await cookies();
  const supabase = createServerClient(url, key, {
    cookies: {
      getAll: () => jar.getAll(),
      setAll: (all) => all.forEach(({ name, value, options }) => jar.set(name, value, options)),
    },
  });
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) return failed;
  return NextResponse.redirect(`${origin}${next}`);
}
