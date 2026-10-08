/** Promise-based wrapper around the clustering worker. */

import type { ClusterPoint, Filters, Station } from "./types";

type Resolver = (value: never) => void;

export interface CatalogInfo {
  countries: string[];
  variables: string[];
  total: number;
  offMap: number;
  builtAt: string;
}

export class ClusterClient {
  private worker: Worker;
  private pending = new Map<number, Resolver>();
  private nextId = 1;

  private readyResolve?: (info: CatalogInfo) => void;
  private readyReject?: (error: Error) => void;
  readonly ready: Promise<CatalogInfo>;

  /** Fired whenever the filtered station count changes. */
  onMatchedChange: (matched: number) => void = () => {};

  constructor() {
    this.worker = new Worker(new URL("../cluster.worker.ts", import.meta.url), {
      type: "module",
    });
    this.ready = new Promise<CatalogInfo>((resolve, reject) => {
      this.readyResolve = resolve;
      this.readyReject = reject;
    });
    this.worker.onmessage = (event) => this.receive(event.data);
  }

  private receive(message: Record<string, unknown>) {
    switch (message.type) {
      case "ready":
        this.onMatchedChange(message.matched as number);
        this.readyResolve?.({
          countries: message.countries as string[],
          variables: message.variables as string[],
          total: message.total as number,
          offMap: message.offMap as number,
          builtAt: message.builtAt as string,
        });
        break;
      case "error":
        this.readyReject?.(new Error(message.message as string));
        break;
      case "filtered":
        this.onMatchedChange(message.matched as number);
        break;
      default: {
        const id = message.id as number | undefined;
        if (id === undefined) return;
        const resolve = this.pending.get(id);
        if (resolve) {
          this.pending.delete(id);
          resolve(message as never);
        }
      }
    }
  }

  private ask<T>(payload: Record<string, unknown>): Promise<T> {
    const id = this.nextId++;
    return new Promise<T>((resolve) => {
      this.pending.set(id, resolve as Resolver);
      this.worker.postMessage({ ...payload, id });
    });
  }

  /** `version` is the catalog_version from /api/providers; it makes the payload cacheable. */
  load(filters: Filters, version: string | null = null) {
    this.worker.postMessage({ type: "load", filters, version });
    return this.ready;
  }

  setFilters(filters: Filters) {
    this.worker.postMessage({ type: "filter", filters });
  }

  async clusters(
    bbox: [number, number, number, number],
    zoom: number,
  ): Promise<ClusterPoint[]> {
    const result = await this.ask<{ points: Omit<ClusterPoint, "x" | "y">[] }>({
      type: "clusters",
      bbox,
      zoom,
    });
    // x/y are filled in by the canvas layer once it projects them.
    return result.points as ClusterPoint[];
  }

  async expansionZoom(clusterId: number): Promise<number> {
    const result = await this.ask<{ zoom: number }>({ type: "expand", clusterId });
    return result.zoom;
  }

  async selectInBbox(
    bbox: [number, number, number, number],
    limit = 5000,
  ): Promise<{ keys: string[]; truncated: boolean }> {
    return this.ask({ type: "selectInBbox", bbox, limit });
  }

  async selectInPolygon(
    ring: [number, number][],
    limit = 5000,
  ): Promise<{ keys: string[]; truncated: boolean }> {
    return this.ask({ type: "selectInPolygon", ring, limit });
  }

  async allFilteredKeys(limit = 5000): Promise<{ keys: string[]; truncated: boolean }> {
    return this.ask({ type: "allKeys", limit });
  }

  async list(offset: number, limit: number): Promise<{ stations: Station[]; total: number }> {
    return this.ask({ type: "list", offset, limit });
  }

  dispose() {
    this.worker.terminate();
  }
}
