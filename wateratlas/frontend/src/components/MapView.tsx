import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

import { CanvasPointLayer } from "../lib/canvasLayer";
import type { ClusterClient } from "../lib/clusterClient";
import { colorForCountry } from "../lib/palette";
import type { ClusterPoint, Station } from "../lib/types";

/** Tile sources. Each carries the attribution its licence requires. */
const BASEMAPS = {
  streets: {
    label: "Streets",
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  },
  terrain: {
    label: "Terrain",
    url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    attribution:
      'Map data &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ' +
      'style &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
    maxZoom: 17,
  },
  imagery: {
    label: "Imagery",
    url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    maxZoom: 18,
  },
} as const;

type BasemapKey = keyof typeof BASEMAPS;
type SelectMode = "none" | "box" | "lasso";

/** Southeast Asia, from the Andaman Sea to Papua and from the Luzon Strait to Timor. */
const SEA_BOUNDS = L.latLngBounds([-11.5, 92], [24.5, 142]);
/** `#sea` in the address opens the map on Southeast Asia, so the view can be shared as a link. */
const SEA_HASH = "#sea";

interface Props {
  client: ClusterClient | null;
  filtersVersion: number;
  selection: Set<string>;
  onPickStation: (station: Station) => void;
  onSelectKeys: (keys: string[], mode: "add" | "replace") => void;
  onSelectionTruncated: (limit: number) => void;
  /** Stations still loading into the worker: how many, or null once they are on the map. */
  loadingCount: number | null;
  /** Why the station catalog could not load, if it could not. */
  loadError: string | null;
}

export function MapView({
  client,
  filtersVersion,
  selection,
  onPickStation,
  onSelectKeys,
  onSelectionTruncated,
  loadingCount,
  loadError,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layerRef = useRef<CanvasPointLayer | null>(null);
  const tileRef = useRef<L.TileLayer | null>(null);
  const selectionRef = useRef(selection);
  const queryToken = useRef(0);

  const [basemap, setBasemap] = useState<BasemapKey>("streets");
  const [selectMode, setSelectMode] = useState<SelectMode>("none");
  const [hovered, setHovered] = useState<ClusterPoint | null>(null);
  const [busy, setBusy] = useState(false);

  selectionRef.current = selection;

  // -- map setup ---------------------------------------------------------------

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = L.map(containerRef.current, {
      // The catalog spans every inhabited continent, so the opening frame is the
      // whole world rather than a region that would hide most of it.
      center: [24, 6],
      zoom: 2,
      minZoom: 2,
      worldCopyJump: true,
      preferCanvas: true,
      zoomControl: false,
    });
    // Top right: the selection cart sits bottom right and would cover it there.
    L.control.zoom({ position: "topright" }).addTo(map);
    L.control.scale({ position: "bottomleft", imperial: false }).addTo(map);
    if (window.location.hash === SEA_HASH) map.fitBounds(SEA_BOUNDS);
    mapRef.current = map;

    // Opening the station panel narrows the map column. Leaflet caches its own
    // size, so it has to be told, or half the tiles never load.
    const observer = new ResizeObserver(() => map.invalidateSize());
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // -- basemap -----------------------------------------------------------------

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    tileRef.current?.remove();
    const source = BASEMAPS[basemap];
    tileRef.current = L.tileLayer(source.url, {
      attribution: source.attribution,
      maxZoom: source.maxZoom,
    }).addTo(map);
    tileRef.current.bringToBack();
  }, [basemap]);

  // -- point layer and viewport queries ----------------------------------------

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !client) return;

    const refresh = async () => {
      const bounds = map.getBounds();
      const token = ++queryToken.current;
      const points = await client.clusters(
        [
          bounds.getWest(),
          Math.max(-85, bounds.getSouth()),
          bounds.getEast(),
          Math.min(85, bounds.getNorth()),
        ],
        map.getZoom(),
      );
      // A later pan may have landed while this query was in flight.
      if (token !== queryToken.current) return;
      layerRef.current?.setPoints(points);
    };

    const layer = new CanvasPointLayer({
      colorFor: (point) => colorForCountry(point.country),
      isSelected: (key) => selectionRef.current.has(key),
      onHover: setHovered,
      onPick: (point, originalEvent) => {
        if (point.station) {
          if (originalEvent.shiftKey || originalEvent.metaKey || originalEvent.ctrlKey) {
            onSelectKeys([point.station.key], "add");
          } else {
            onPickStation(point.station);
          }
          return;
        }
        if (point.clusterId !== null) {
          client.expansionZoom(point.clusterId).then((zoom) => {
            map.setView([point.lat, point.lon], Math.min(zoom, map.getMaxZoom() ?? 19));
          });
        }
      },
    });
    layer.addTo(map);
    layerRef.current = layer;

    map.on("moveend zoomend", refresh);
    refresh();

    return () => {
      map.off("moveend zoomend", refresh);
      layer.remove();
      layerRef.current = null;
    };
  }, [client, onPickStation, onSelectKeys]);

  // Re-query when filters change; repaint when the selection changes.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !client) return;
    map.fire("moveend");
  }, [client, filtersVersion]);

  useEffect(() => {
    layerRef.current?.refresh();
  }, [selection]);

  // -- box and lasso selection --------------------------------------------------

  useEffect(() => {
    const map = mapRef.current;
    const container = containerRef.current;
    if (!map || !client || !container || selectMode === "none") return;

    const overlay = document.createElement("div");
    overlay.className = "riv-select-overlay";
    container.appendChild(overlay);

    const rubberBand = document.createElement("div");
    rubberBand.className = "riv-rubber-band";
    rubberBand.hidden = true;
    overlay.appendChild(rubberBand);

    const lassoCanvas = document.createElement("canvas");
    lassoCanvas.className = "riv-lasso-canvas";
    overlay.appendChild(lassoCanvas);
    const sizeCanvas = () => {
      const size = map.getSize();
      const ratio = window.devicePixelRatio || 1;
      lassoCanvas.width = size.x * ratio;
      lassoCanvas.height = size.y * ratio;
      lassoCanvas.style.width = `${size.x}px`;
      lassoCanvas.style.height = `${size.y}px`;
      lassoCanvas.getContext("2d")?.setTransform(ratio, 0, 0, ratio, 0, 0);
    };
    sizeCanvas();
    map.on("resize", sizeCanvas);

    let origin: { x: number; y: number } | null = null;
    let path: { x: number; y: number }[] = [];

    const localPoint = (event: MouseEvent) => {
      const rect = container.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    };

    const onDown = (event: MouseEvent) => {
      if (event.button !== 0) return;
      event.preventDefault();
      origin = localPoint(event);
      path = [origin];
      if (selectMode === "box") {
        rubberBand.hidden = false;
        rubberBand.style.left = `${origin.x}px`;
        rubberBand.style.top = `${origin.y}px`;
        rubberBand.style.width = "0px";
        rubberBand.style.height = "0px";
      }
    };

    const onMove = (event: MouseEvent) => {
      if (!origin) return;
      const point = localPoint(event);
      if (selectMode === "box") {
        rubberBand.style.left = `${Math.min(origin.x, point.x)}px`;
        rubberBand.style.top = `${Math.min(origin.y, point.y)}px`;
        rubberBand.style.width = `${Math.abs(point.x - origin.x)}px`;
        rubberBand.style.height = `${Math.abs(point.y - origin.y)}px`;
      } else {
        path.push(point);
        const context = lassoCanvas.getContext("2d")!;
        const size = map.getSize();
        context.clearRect(0, 0, size.x, size.y);
        context.beginPath();
        context.moveTo(path[0].x, path[0].y);
        for (const node of path.slice(1)) context.lineTo(node.x, node.y);
        context.closePath();
        context.fillStyle = "rgba(20,101,127,0.12)";
        context.fill();
        context.strokeStyle = "#14657f";
        context.lineWidth = 2;
        context.setLineDash([5, 4]);
        context.stroke();
      }
    };

    const onUp = async (event: MouseEvent) => {
      if (!origin) return;
      const end = localPoint(event);
      const additive = event.shiftKey;
      const limit = 5000;
      setBusy(true);
      try {
        let result: { keys: string[]; truncated: boolean };
        if (selectMode === "box") {
          const a = map.containerPointToLatLng([origin.x, origin.y]);
          const b = map.containerPointToLatLng([end.x, end.y]);
          result = await client.selectInBbox(
            [
              Math.min(a.lng, b.lng),
              Math.min(a.lat, b.lat),
              Math.max(a.lng, b.lng),
              Math.max(a.lat, b.lat),
            ],
            limit,
          );
        } else {
          if (path.length < 3) return;
          const ring = path.map((node) => {
            const latLng = map.containerPointToLatLng([node.x, node.y]);
            return [latLng.lng, latLng.lat] as [number, number];
          });
          result = await client.selectInPolygon(ring, limit);
        }
        onSelectKeys(result.keys, additive ? "add" : "replace");
        if (result.truncated) onSelectionTruncated(limit);
      } finally {
        setBusy(false);
        origin = null;
        path = [];
        rubberBand.hidden = true;
        const size = map.getSize();
        lassoCanvas.getContext("2d")?.clearRect(0, 0, size.x, size.y);
      }
    };

    overlay.addEventListener("mousedown", onDown);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);

    return () => {
      map.off("resize", sizeCanvas);
      overlay.removeEventListener("mousedown", onDown);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      overlay.remove();
    };
  }, [client, selectMode, onSelectKeys, onSelectionTruncated]);

  // Escape leaves selection mode — the usual way out of a modal tool.
  useEffect(() => {
    if (selectMode === "none") return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectMode("none");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectMode]);

  const showRegion = (region: "world" | "sea") => {
    const map = mapRef.current;
    if (!map) return;
    if (region === "sea") {
      map.fitBounds(SEA_BOUNDS);
      window.history.replaceState(null, "", SEA_HASH);
    } else {
      map.setView([24, 6], 2);
      if (window.location.hash === SEA_HASH) {
        window.history.replaceState(null, "", window.location.pathname + window.location.search);
      }
    }
  };

  const selectVisible = async () => {
    const map = mapRef.current;
    if (!map || !client) return;
    const bounds = map.getBounds();
    setBusy(true);
    try {
      const result = await client.selectInBbox(
        [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()],
        5000,
      );
      onSelectKeys(result.keys, "add");
      if (result.truncated) onSelectionTruncated(5000);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="riv-map-wrap">
      <div ref={containerRef} className="riv-map" />

      <div className="riv-map-toolbar">
        <div className="riv-segmented" role="group" aria-label="Region">
          <button type="button" onClick={() => showRegion("world")} title="Show the whole world">
            World
          </button>
          <button
            type="button"
            onClick={() => showRegion("sea")}
            title="Zoom to Southeast Asia (shareable as #sea)"
          >
            SE Asia
          </button>
        </div>

        <div className="riv-segmented" role="group" aria-label="Basemap">
          {(Object.keys(BASEMAPS) as BasemapKey[]).map((key) => (
            <button
              key={key}
              type="button"
              className={basemap === key ? "is-active" : ""}
              onClick={() => setBasemap(key)}
            >
              {BASEMAPS[key].label}
            </button>
          ))}
        </div>

        <div className="riv-segmented" role="group" aria-label="Selection tool">
          <button
            type="button"
            className={selectMode === "box" ? "is-active" : ""}
            onClick={() => setSelectMode(selectMode === "box" ? "none" : "box")}
            title="Drag a rectangle to select stations"
          >
            Box
          </button>
          <button
            type="button"
            className={selectMode === "lasso" ? "is-active" : ""}
            onClick={() => setSelectMode(selectMode === "lasso" ? "none" : "lasso")}
            title="Draw a freehand shape to select stations"
          >
            Lasso
          </button>
          <button type="button" onClick={selectVisible} title="Select every station in view">
            In view
          </button>
        </div>
      </div>

      {selectMode !== "none" && (
        <div className="riv-map-hint">
          {selectMode === "box" ? "Drag a rectangle" : "Draw around the stations"} — hold Shift to
          add to the selection, Escape to stop.
        </div>
      )}

      {busy && <div className="riv-map-busy">Selecting…</div>}

      {loadError ? (
        <div className="riv-map-loading riv-map-loading-error" role="alert">
          <strong>The stations could not load</strong>
          <span>{loadError}</span>
        </div>
      ) : (
        loadingCount !== null && (
          <div className="riv-map-loading" role="status" aria-live="polite">
            <strong>
              Loading {loadingCount > 0 ? loadingCount.toLocaleString() : ""} stations…
            </strong>
            <span className="riv-loading-bar" aria-hidden="true" />
          </div>
        )
      )}

      {hovered && (
        <div className="riv-map-readout">
          {hovered.station ? (
            <>
              <strong>{hovered.station.name ?? hovered.station.gaugeId}</strong>
              <span className="riv-mono">{hovered.station.key}</span>
            </>
          ) : (
            <>
              <strong>{hovered.count.toLocaleString()} stations</strong>
              <span>click to zoom in</span>
            </>
          )}
        </div>
      )}
    </div>
  );
}
