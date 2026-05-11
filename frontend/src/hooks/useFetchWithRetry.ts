import { useEffect, type DependencyList } from "react";

interface Args<T> {
  enabled: boolean;
  done: boolean;
  fetcher: () => Promise<Response>;
  onSuccess: (data: T) => void;
  label?: string;
  deps: DependencyList;
}

/**
 * Fetch-with-exponential-backoff effect. Replaces four near-identical copies
 * that previously lived inside Map.tsx for SNOTEL, CAIC, observations, and
 * Strava activities.
 *
 * Retries up to 5 attempts with 2s, 4s, 8s, 16s, 20s backoff. Honors a cancel
 * signal on cleanup so a layer toggled off mid-retry doesn't write to state.
 */
export function useFetchWithRetry<T>({
  enabled,
  done,
  fetcher,
  onSuccess,
  label,
  deps,
}: Args<T>): void {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!enabled || done) return;
    let cancelled = false;
    const tag = label ?? "fetch";
    const load = (attempt: number) => {
      fetcher()
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((data) => {
          if (!cancelled) onSuccess(data as T);
        })
        .catch((err) => {
          if (cancelled) return;
          console.warn(`${tag} fetch failed (attempt ${attempt + 1}):`, err);
          if (attempt < 4) {
            setTimeout(() => load(attempt + 1), Math.min(2000 * 2 ** attempt, 20000));
          }
        });
    };
    load(0);
    return () => {
      cancelled = true;
    };
  }, deps);
}
