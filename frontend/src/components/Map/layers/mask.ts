import maplibregl from "maplibre-gl";
import { CO_MASK_GEOJSON } from "../constants";

export function addColoradoMask(map: maplibregl.Map) {
  if (map.getSource("co-mask")) return;
  map.addSource("co-mask", { type: "geojson", data: CO_MASK_GEOJSON as any });
  map.addLayer({ id: "co-mask-fill", type: "fill", source: "co-mask",
    paint: { "fill-color": "#000000", "fill-opacity": 1 },
  });
}

export function setMaskVisibility(map: maplibregl.Map | null, visible: boolean) {
  if (!map || !map.getLayer("co-mask-fill")) return;
  map.setLayoutProperty("co-mask-fill", "visibility", visible ? "visible" : "none");
}
