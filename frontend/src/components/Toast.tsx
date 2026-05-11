import { useEffect, useState } from "react";
import { Z } from "./Map/zIndex";

type ToastType = "info" | "error" | "success";

interface Toast {
  id: number;
  message: string;
  type: ToastType;
}

const listeners = new Set<(t: Toast) => void>();
let nextId = 1;

export function showToast(message: string, type: ToastType = "info") {
  const t: Toast = { id: nextId++, message, type };
  listeners.forEach((fn) => fn(t));
}

const COLORS: Record<ToastType, { bg: string; fg: string }> = {
  info:    { bg: "rgba(74,144,217,0.95)", fg: "#fff" },
  error:   { bg: "rgba(215,25,28,0.95)",  fg: "#fff" },
  success: { bg: "rgba(26,150,65,0.95)",  fg: "#fff" },
};

const TOAST_TTL_MS = 5000;

export function ToastContainer() {
  const [toasts, setToasts] = useState<Toast[]>([]);

  useEffect(() => {
    const onToast = (t: Toast) => {
      setToasts((prev) => [...prev, t]);
      setTimeout(() => {
        setToasts((prev) => prev.filter((x) => x.id !== t.id));
      }, TOAST_TTL_MS);
    };
    listeners.add(onToast);
    return () => { listeners.delete(onToast); };
  }, []);

  return (
    <div
      aria-live="polite"
      style={{
        position: "fixed",
        bottom: 16,
        right: 16,
        zIndex: Z.TOAST,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        maxWidth: 320,
        pointerEvents: "none",
      }}
    >
      {toasts.map((t) => {
        const c = COLORS[t.type];
        return (
          <div
            key={t.id}
            role={t.type === "error" ? "alert" : "status"}
            onClick={() => setToasts((prev) => prev.filter((x) => x.id !== t.id))}
            style={{
              background: c.bg,
              color: c.fg,
              padding: "10px 14px",
              borderRadius: 6,
              fontSize: 13,
              fontFamily: "ui-sans-serif, system-ui, sans-serif",
              boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
              pointerEvents: "auto",
              cursor: "pointer",
              animation: "whumpf-toast-in 0.2s ease-out",
            }}
          >
            {t.message}
          </div>
        );
      })}
    </div>
  );
}
