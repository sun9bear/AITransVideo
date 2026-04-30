import type { NextConfig } from "next";

const isDev = process.env.NODE_ENV !== "production";

const nextConfig: NextConfig = {
  output: "standalone",

  /**
   * In development, the production gateway (https://aitrans.video) is the
   * only practical source of truth for `/api/plans`. We rewrite local API
   * calls there so PricingGrid + TrialBanner can SSR with real prices when
   * developing the marketing rewrite locally.
   *
   * In production this rewrite is inert — the real Caddy + Gateway handle
   * `/api/*` upstream of Next, so this code path never executes.
   */
  async rewrites() {
    if (!isDev) return []
    return [
      {
        source: "/api/:path*",
        destination: "https://aitrans.video/api/:path*",
      },
    ]
  },
};

export default nextConfig;
