import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    const origin = process.env.PLATFORM_API_ORIGIN ?? "http://127.0.0.1:8000";
    return [
      { source: "/api/v1/:path*", destination: `${origin}/api/v1/:path*` },
    ];
  },
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  // Pricing moved onto the landing page; keep old /pricing links working.
  async redirects() {
    return [{ source: "/pricing", destination: "/#pricing", permanent: true }];
  },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
