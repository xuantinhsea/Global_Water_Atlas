/**
 * A single-canvas Leaflet layer for cluster and station circles.
 *
 * Leaflet's own markers and vector layers create one DOM node per feature.
 * Even after clustering, a busy viewport can hold several hundred circles that
 * are repainted on every pan; one canvas keeps that to a single node and one
 * draw call per frame.
 */

import L from "leaflet";
import type { ClusterPoint } from "./types";

export interface CanvasPointLayerOptions extends L.LayerOptions {
  colorFor: (point: ClusterPoint) => string;
  isSelected: (key: string) => boolean;
  onPick: (point: ClusterPoint, originalEvent: MouseEvent) => void;
  onHover: (point: ClusterPoint | null) => void;
}

/** Radius in CSS pixels for a cluster of `count` stations. */
function clusterRadius(count: number): number {
  if (count === 1) return 5;
  // sqrt keeps area proportional to count without letting big clusters dominate.
  return Math.min(26, 8 + Math.sqrt(count) * 1.5);
}

export class CanvasPointLayer extends L.Layer {
  declare options: CanvasPointLayerOptions;

  private canvas!: HTMLCanvasElement;
  private context!: CanvasRenderingContext2D;
  private points: ClusterPoint[] = [];
  private frame = 0;
  private hovered: ClusterPoint | null = null;

  constructor(options: CanvasPointLayerOptions) {
    super();
    L.Util.setOptions(this, options);
  }

  onAdd(map: L.Map): this {
    this.canvas = L.DomUtil.create("canvas", "riv-canvas-layer") as HTMLCanvasElement;
    this.canvas.style.position = "absolute";
    this.canvas.style.pointerEvents = "none";
    this.context = this.canvas.getContext("2d")!;

    map.getPanes().overlayPane!.appendChild(this.canvas);
    map.on("moveend zoomend resize viewreset", this.reset, this);
    map.on("zoomanim", this.onZoomAnim, this);
    map.on("click", this.onClick, this);
    map.on("mousemove", this.onMouseMove, this);
    map.on("mouseout", this.onMouseOut, this);

    this.reset();
    return this;
  }

  onRemove(map: L.Map): this {
    map.off("moveend zoomend resize viewreset", this.reset, this);
    map.off("zoomanim", this.onZoomAnim, this);
    map.off("click", this.onClick, this);
    map.off("mousemove", this.onMouseMove, this);
    map.off("mouseout", this.onMouseOut, this);
    this.canvas.remove();
    return this;
  }

  setPoints(points: ClusterPoint[]) {
    this.points = points;
    this.schedule();
  }

  /** Repaint without re-querying — used when the selection changes. */
  refresh() {
    this.schedule();
  }

  private onZoomAnim = (event: L.ZoomAnimEvent) => {
    // Keep the canvas glued to the map during the zoom tween; it is redrawn at
    // the new zoom as soon as the animation settles.
    const map = this._map as L.Map;
    const scale = map.getZoomScale(event.zoom, map.getZoom());
    const offset = map
      // @ts-expect-error _getCenterOffset is internal but is the documented recipe
      ._getCenterOffset(event.center)
      ._multiplyBy(-scale)
      .subtract(
        // @ts-expect-error _getMapPanePos is internal
        map._getMapPanePos(),
      );
    L.DomUtil.setTransform(this.canvas, offset, scale);
  };

  private reset = () => {
    const map = this._map as L.Map;
    const size = map.getSize();
    const ratio = window.devicePixelRatio || 1;
    const topLeft = map.containerPointToLayerPoint([0, 0]);

    L.DomUtil.setTransform(this.canvas, topLeft, 1);
    this.canvas.width = Math.round(size.x * ratio);
    this.canvas.height = Math.round(size.y * ratio);
    this.canvas.style.width = `${size.x}px`;
    this.canvas.style.height = `${size.y}px`;
    this.context.setTransform(ratio, 0, 0, ratio, 0, 0);

    this.schedule();
  };

  private schedule() {
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.draw();
    });
  }

  /** Projects every point into current container pixels, then paints. */
  private draw() {
    if (!this._map) return;
    const map = this._map as L.Map;
    const size = map.getSize();
    const context = this.context;

    context.clearRect(0, 0, size.x, size.y);
    context.lineWidth = 1.25;

    for (const point of this.points) {
      const projected = map.latLngToContainerPoint([point.lat, point.lon]);
      point.x = projected.x;
      point.y = projected.y;
    }

    // Clusters first, so single stations and selections sit on top.
    const clusters = this.points.filter((point) => point.count > 1);
    const singles = this.points.filter((point) => point.count === 1);

    for (const point of clusters) {
      const radius = clusterRadius(point.count);
      context.beginPath();
      context.arc(point.x, point.y, radius, 0, Math.PI * 2);
      context.fillStyle = this.options.colorFor(point);
      context.globalAlpha = 0.72;
      context.fill();
      context.globalAlpha = 1;
      context.strokeStyle = "rgba(255,255,255,0.85)";
      context.stroke();

      // Label every cluster — an unlabelled circle is indistinguishable from a
      // single station. The type shrinks with the circle rather than dropping out.
      const fontSize = radius >= 16 ? 12 : radius >= 11 ? 10 : 9;
      context.fillStyle = "#fff";
      context.font = `600 ${fontSize}px "IBM Plex Mono", ui-monospace, monospace`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(formatCount(point.count), point.x, point.y);
    }

    for (const point of singles) {
      const selected = point.station ? this.options.isSelected(point.station.key) : false;
      const radius = selected ? 7 : 5;
      context.beginPath();
      context.arc(point.x, point.y, radius, 0, Math.PI * 2);
      context.fillStyle = selected ? "#ffffff" : this.options.colorFor(point);
      context.fill();
      context.lineWidth = selected ? 3 : 1.25;
      context.strokeStyle = selected
        ? this.options.colorFor(point)
        : "rgba(255,255,255,0.9)";
      context.stroke();
      context.lineWidth = 1.25;
    }

    if (this.hovered) {
      context.beginPath();
      context.arc(
        this.hovered.x,
        this.hovered.y,
        clusterRadius(this.hovered.count) + 4,
        0,
        Math.PI * 2,
      );
      context.strokeStyle = "rgba(255,255,255,0.95)";
      context.lineWidth = 2;
      context.stroke();
    }
  }

  /** Nearest point within its own radius, or null. */
  private hitTest(container: L.Point): ClusterPoint | null {
    let best: ClusterPoint | null = null;
    let bestDistance = Infinity;
    for (const point of this.points) {
      const radius = clusterRadius(point.count) + 4;
      const dx = point.x - container.x;
      const dy = point.y - container.y;
      const distance = dx * dx + dy * dy;
      if (distance <= radius * radius && distance < bestDistance) {
        best = point;
        bestDistance = distance;
      }
    }
    return best;
  }

  private onClick = (event: L.LeafletMouseEvent) => {
    const hit = this.hitTest(event.containerPoint);
    if (hit) this.options.onPick(hit, event.originalEvent);
  };

  private onMouseMove = (event: L.LeafletMouseEvent) => {
    const hit = this.hitTest(event.containerPoint);
    if (hit !== this.hovered) {
      this.hovered = hit;
      const container = (this._map as L.Map).getContainer();
      container.style.cursor = hit ? "pointer" : "";
      this.options.onHover(hit);
      this.schedule();
    }
  };

  private onMouseOut = () => {
    if (this.hovered) {
      this.hovered = null;
      this.options.onHover(null);
      this.schedule();
    }
  };
}

function formatCount(count: number): string {
  if (count >= 10000) return `${Math.round(count / 1000)}k`;
  if (count >= 1000) return `${(count / 1000).toFixed(1)}k`;
  return String(count);
}
