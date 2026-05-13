import React, { useRef, useState, useCallback } from "react";
import type { CSSProperties, PointerEvent } from "react";

export function DragHandle({ mobile, handleProps, theme }: {
  mobile: boolean;
  handleProps: { onPointerDown: (e: PointerEvent<HTMLElement>) => void; style: CSSProperties };
  theme: { divider: string };
}) {
  if (mobile) return null;
  return React.createElement("div", {
    ...handleProps,
    style: { ...handleProps.style, display: "flex", justifyContent: "center", padding: "2px 0 7px" },
  }, React.createElement("div", {
    style: { width: 32, height: 3, borderRadius: 2, background: theme.divider, opacity: 0.6 },
  }));
}

interface DragResult {
  panelRef: React.MutableRefObject<HTMLDivElement | null>;
  handleProps: {
    onPointerDown: (e: PointerEvent<HTMLElement>) => void;
    style: CSSProperties;
  };
  panelEventProps: {
    onPointerMove: (e: PointerEvent<HTMLDivElement>) => void;
    onPointerUp: () => void;
  };
  dragStyle: CSSProperties;
}

// Attach panelRef to the outer panel div, handleProps to the drag-grip element,
// panelEventProps to the outer panel div, and merge dragStyle into the panel style.
// On mobile the hook is a no-op (panels are bottom sheets, not draggable).
export function useDraggable(mobile: boolean): DragResult {
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const drag = useRef<{ sx: number; sy: number; ot: number; ol: number } | null>(null);

  const onPointerDown = useCallback((e: PointerEvent<HTMLElement>) => {
    if (mobile || e.button !== 0) return;
    const el = panelRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    drag.current = { sx: e.clientX, sy: e.clientY, ot: rect.top, ol: rect.left };
    el.setPointerCapture(e.pointerId);
    e.preventDefault();
  }, [mobile]);

  const onPointerMove = useCallback((e: PointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    setPos({ top: d.ot + e.clientY - d.sy, left: d.ol + e.clientX - d.sx });
  }, []);

  const onPointerUp = useCallback(() => { drag.current = null; }, []);

  const dragStyle: CSSProperties = pos && !mobile
    ? { top: pos.top, left: pos.left, bottom: "auto", right: "auto", transform: "none" }
    : {};

  return {
    panelRef,
    handleProps: { onPointerDown, style: mobile ? {} : { cursor: "grab", userSelect: "none" } },
    panelEventProps: { onPointerMove, onPointerUp },
    dragStyle,
  };
}
