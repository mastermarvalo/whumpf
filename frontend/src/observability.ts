// Observability shim: opt-in via env vars, no-op when unset.
//
//   VITE_SENTRY_DSN        — turns on Sentry error capture
//   VITE_PLAUSIBLE_DOMAIN  — turns on Plausible analytics
//   VITE_PLAUSIBLE_SRC     — override the script URL when self-hosting
//
// Sentry runs in error-only mode (tracesSampleRate=0) to keep bundle + traffic
// minimal. Plausible loads via a script tag we inject at startup — easier than
// teaching Vite to conditionally rewrite index.html.

import * as Sentry from "@sentry/react";

const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN as string | undefined;
const PLAUSIBLE_DOMAIN = import.meta.env.VITE_PLAUSIBLE_DOMAIN as string | undefined;
const PLAUSIBLE_SRC = (import.meta.env.VITE_PLAUSIBLE_SRC as string | undefined)
  ?? "https://plausible.io/js/script.js";

declare global {
  interface Window {
    plausible?: (
      event: string,
      opts?: { props?: Record<string, unknown> },
    ) => void;
  }
}

export function initObservability(): void {
  if (SENTRY_DSN) {
    Sentry.init({
      dsn: SENTRY_DSN,
      tracesSampleRate: 0,
    });
  }
  if (PLAUSIBLE_DOMAIN) {
    const s = document.createElement("script");
    s.defer = true;
    s.setAttribute("data-domain", PLAUSIBLE_DOMAIN);
    s.src = PLAUSIBLE_SRC;
    document.head.appendChild(s);
  }
}

/** Best-effort error capture. Falls back to console when Sentry is off. */
export function captureError(error: unknown, context?: Record<string, unknown>): void {
  if (SENTRY_DSN) {
    Sentry.captureException(error, { extra: context });
  } else {
    console.error("[whumpf]", error, context);
  }
}

/**
 * Fire a Plausible custom event. Safe to call even when analytics isn't
 * loaded — the call is a no-op until window.plausible exists.
 */
export function track(event: string, props?: Record<string, unknown>): void {
  window.plausible?.(event, props ? { props } : undefined);
}
