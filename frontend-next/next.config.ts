import type { NextConfig } from "next";
import createNextIntlPlugin from "next-intl/plugin";

const isDev = process.env.NODE_ENV !== "production";

const nextConfig: NextConfig = {
  output: "standalone",

  /**
   * In development, the production gateway (https://aitrans.video) is the
   * only practical source of truth for **pricing-tier** endpoints. We
   * rewrite specific local API calls there so PricingGrid + TrialBanner
   * can SSR with real prices when developing the marketing rewrite
   * locally.
   *
   * **DO NOT** reintroduce a blanket ``/api/:path*`` rewrite (Codex 2026-05-27
   * PR #15 P2 fix on commit 9a621859). A blanket rewrite proxies
   * EVERYTHING — including paid-API call sites like
   * ``/api/voice/cosyvoice/clone`` and the per-user policy gate
   * ``/api/voice/cosyvoice/clone-gate`` — to production. In dev that
   * either gives wrong answers (clone-gate reads prod session) or, with
   * the right cookies, fires real paid clones against the prod account.
   * Both violate the "local dev never touches paid external services
   * silently" rule (CLAUDE.md paid-API hard constraint).
   *
   * Allowlist principle: only rewrite endpoints whose dev value MUST
   * come from production AND that are guaranteed safe to read with prod
   * data — pricing tiers, public catalog, public marketing pages. Any
   * authenticated / paid-API / user-mutating endpoint stays local
   * (returns 404 from `next dev` if no local backend is running — that's
   * the right failure mode for development).
   *
   * In production this rewrite is inert — the real Caddy + Gateway handle
   * `/api/*` upstream of Next, so this code path never executes.
   *
   * Guarded by tests/test_phase42_e1_next_config_no_blanket_rewrite.py
   * — any future regression to a blanket `/api/:path*` rewrite (or
   * adding any cosyvoice / clone path to the rewrite list) will fail
   * the static guard.
   */
  async rewrites() {
    if (!isDev) return []
    return [
      // Pricing / marketing SSR fetch — safe to read prod values
      {
        source: "/api/plans",
        destination: "https://aitrans.video/api/plans",
      },
    ]
  },
};

// next-intl plugin：注入 src/i18n/request.ts 作为 getRequestConfig 来源。
// 只包裹导出，不触碰 output:'standalone' / rewrites()（no-blanket-rewrite guard 由
// tests/test_phase42_e1_next_config_no_blanket_rewrite.py 守护）。
const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

export default withNextIntl(nextConfig);
