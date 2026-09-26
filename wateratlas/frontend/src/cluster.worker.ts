/**
 * Station clustering, off the main thread.
 *
 * 73,000 Leaflet markers would create 73,000 DOM nodes and Leaflet.markercluster
 * struggles well below that count. Instead this worker owns the whole station
 * index, builds a supercluster index over whatever the current filters allow,
 * and answers viewport queries. The main thread only ever receives the few
 * hundred circles it actually has to paint.
 *
 * It also owns filtering, so panning and zooming never round-trip to the server.
 */

import Supercluster from "supercluster";
import type { Filters, MapPayload, StationRow } from "./lib/types";

type Point = GeoJSON.Feature<GeoJSON.Point, { i: number }>;

let payload: MapPayload | null = null;
let allPoints: Point[] = [];
let index: Supercluster<{ i: number }> | null = null;
let filteredIndices: Int32Array = new Int32Array(0);

/** Bit positions must match `registry.all_variables()` on the server. */
function variableMaskFor(variables: string[]): number {
  if (!payload) return 0;
  let mask = 0;
  for (const variable of variables) {
    const position = payload.variables.indexOf(variable);
    if (position >= 0) mask |= 1 << position;
  }
  return mask;
}

function buildIndex(filters: Filters) {
  if (!payload) return;

  const wantedCountries = new Set(
    filters.countries.map((key) => payload!.countries.indexOf(key)).filter((i) => i >= 0),
  );
  const wantedMask = variableMaskFor(filters.variables);
  const needle = filters.text.trim().toLowerCase();

  const kept: Point[] = [];
  const keptIndices: number[] = [];

  for (let i = 0; i < payload.stations.length; i += 1) {
    const row = payload.stations[i];
    if (wantedCountries.size && !wantedCountries.has(row[3])) continue;
    // A station matches if it declares any of the requested variables.
    if (wantedMask && (row[4] & wantedMask) === 0) continue;
    if (filters.minArea !== null && (row[6] === null || row[6] < filters.minArea)) continue;
    if (filters.maxArea !== null && (row[6] === null || row[6] > filters.maxArea)) continue;
    if (needle) {
      const name = row[5];
      const matches =
        row[0].toLowerCase().includes(needle) ||
        (name !== null && name.toLowerCase().includes(needle));
      if (!matches) continue;
    }
    kept.push(allPoints[i]);
    keptIndices.push(i);
  }

  filteredIndices = Int32Array.from(keptIndices);
  index = new Supercluster<{ i: number }>({
    // Supercluster measures its radius in tile units with `extent` per tile
    // (512 by default), while Leaflet paints 256 px tiles. So one Leaflet pixel
    // is two units here, and 120 gives the ~60 px grouping the map wants.
    radius: 120,
    maxZoom: 13,
    minPoints: 3,
  });
  index.load(kept as never);
}

function stationAt(i: number) {
  const row: StationRow = payload!.stations[i];
  const country = payload!.countries[row[3]];
  const variables: string[] = [];
  for (let bit = 0; bit < payload!.variables.length; bit += 1) {
    if (row[4] & (1 << bit)) variables.push(payload!.variables[bit]);
  }
  return {
    key: `${country}:${row[0]}`,
    gaugeId: row[0],
    country,
    name: row[5],
    lat: row[1],
    lon: row[2],
    area: row[6],
    variables,
  };
}

/** Which country dominates a cluster, so its circle can take that colour. */
function dominantCountry(clusterId: number): string {
  if (!index || !payload) return "";
  const sample = index.getLeaves(clusterId, 24, 0) as Point[];
  const tally = new Map<number, number>();
  for (const leaf of sample) {
    const countryIndex = payload.stations[leaf.properties.i][3];
    tally.set(countryIndex, (tally.get(countryIndex) ?? 0) + 1);
  }
  let best = -1;
  let bestCount = -1;
  for (const [countryIndex, count] of tally) {
    if (count > bestCount) {
      best = countryIndex;
      bestCount = count;
    }
  }
  return best >= 0 ? payload.countries[best] : "";
}

/** Ray-casting point-in-polygon on a [lon, lat] ring. */
function pointInRing(lon: number, lat: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

self.onmessage = async (event: MessageEvent) => {
  const message = event.data;

  switch (message.type) {
    case "load": {
      const response = await fetch("/api/stations/map");
      if (!response.ok) {
        (self as unknown as Worker).postMessage({
          type: "error",
          message: `Could not load the station catalog (${response.status}). Run: wateratlas build-catalog`,
        });
        return;
      }
      payload = (await response.json()) as MapPayload;
      allPoints = payload.stations.map((row, i) => ({
        type: "Feature" as const,
        properties: { i },
        geometry: { type: "Point" as const, coordinates: [row[2], row[1]] },
      }));
      buildIndex(message.filters);
      (self as unknown as Worker).postMessage({
        type: "ready",
        countries: payload.countries,
        variables: payload.variables,
        total: payload.station_count,
        offMap: payload.off_map_count,
        builtAt: payload.built_at,
        matched: filteredIndices.length,
      });
      break;
    }

    case "filter": {
      buildIndex(message.filters);
      (self as unknown as Worker).postMessage({
        type: "filtered",
        matched: filteredIndices.length,
      });
      break;
    }

    case "clusters": {
      if (!index || !payload) return;
      const raw = index.getClusters(message.bbox, Math.round(message.zoom));
      const points = raw.map((feature) => {
        const properties = feature.properties as Record<string, unknown>;
        const isCluster = Boolean(properties.cluster);
        const [lon, lat] = feature.geometry.coordinates as [number, number];
        if (isCluster) {
          const clusterId = properties.cluster_id as number;
          return {
            lat,
            lon,
            count: properties.point_count as number,
            clusterId,
            station: null,
            country: dominantCountry(clusterId),
          };
        }
        const station = stationAt((properties as { i: number }).i);
        return { lat, lon, count: 1, clusterId: null, station, country: station.country };
      });
      (self as unknown as Worker).postMessage({
        type: "clusters",
        id: message.id,
        points,
      });
      break;
    }

    case "expand": {
      // Where a cluster stops being a cluster, so a click can zoom to exactly there.
      if (!index) return;
      (self as unknown as Worker).postMessage({
        type: "expand",
        id: message.id,
        zoom: index.getClusterExpansionZoom(message.clusterId),
      });
      break;
    }

    case "selectInBbox": {
      // Box select. Reads the filtered set directly rather than walking the
      // cluster tree, so it is exact at every zoom level.
      if (!payload) return;
      const [west, south, east, north] = message.bbox as number[];
      const keys: string[] = [];
      for (const i of filteredIndices) {
        const row = payload.stations[i];
        if (row[1] < south || row[1] > north) continue;
        if (west <= east ? row[2] < west || row[2] > east : row[2] < west && row[2] > east) continue;
        keys.push(`${payload.countries[row[3]]}:${row[0]}`);
        if (keys.length >= message.limit) break;
      }
      (self as unknown as Worker).postMessage({
        type: "selection",
        id: message.id,
        keys,
        truncated: keys.length >= message.limit,
      });
      break;
    }

    case "selectInPolygon": {
      // Freehand lasso. The ring arrives as [lon, lat] pairs in map order.
      if (!payload) return;
      const ring = message.ring as [number, number][];
      const keys: string[] = [];
      let west = Infinity;
      let east = -Infinity;
      let south = Infinity;
      let north = -Infinity;
      for (const [lon, lat] of ring) {
        west = Math.min(west, lon);
        east = Math.max(east, lon);
        south = Math.min(south, lat);
        north = Math.max(north, lat);
      }
      for (const i of filteredIndices) {
        const row = payload.stations[i];
        // Cheap bounding-box reject before the ray cast.
        if (row[1] < south || row[1] > north || row[2] < west || row[2] > east) continue;
        if (!pointInRing(row[2], row[1], ring)) continue;
        keys.push(`${payload.countries[row[3]]}:${row[0]}`);
        if (keys.length >= message.limit) break;
      }
      (self as unknown as Worker).postMessage({
        type: "selection",
        id: message.id,
        keys,
        truncated: keys.length >= message.limit,
      });
      break;
    }

    case "list": {
      // A page of the filtered set, for the results table.
      if (!payload) return;
      const offset = message.offset ?? 0;
      const limit = message.limit ?? 100;
      const slice: ReturnType<typeof stationAt>[] = [];
      for (let i = offset; i < Math.min(offset + limit, filteredIndices.length); i += 1) {
        slice.push(stationAt(filteredIndices[i]));
      }
      (self as unknown as Worker).postMessage({
        type: "list",
        id: message.id,
        stations: slice,
        total: filteredIndices.length,
      });
      break;
    }

    case "allKeys": {
      if (!payload) return;
      const keys: string[] = [];
      for (const i of filteredIndices) {
        const row = payload.stations[i];
        keys.push(`${payload.countries[row[3]]}:${row[0]}`);
        if (keys.length >= message.limit) break;
      }
      (self as unknown as Worker).postMessage({
        type: "selection",
        id: message.id,
        keys,
        truncated: keys.length >= message.limit,
      });
      break;
    }
  }
};
