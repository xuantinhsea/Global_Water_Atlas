/** Shared types for the atlas. Mirrors the payloads in wateratlas/api.py. */

/** One station as it arrives in the compact map payload. */
export type StationRow = [
  gaugeId: string,
  lat: number,
  lon: number,
  countryIndex: number,
  variableMask: number,
  stationName: string | null,
  area: number | null,
];

export interface MapPayload {
  version: number;
  built_at: string;
  countries: string[];
  variables: string[];
  fields: string[];
  station_count: number;
  off_map_count: number;
  stations: StationRow[];
}

/** A station flattened for display. `key` is `${country}:${gaugeId}`. */
export interface Station {
  key: string;
  gaugeId: string;
  country: string;
  name: string | null;
  lat: number;
  lon: number;
  area: number | null;
  variables: string[];
}

export interface Provider {
  key: string;
  label: string;
  country_name: string;
  source_url: string;
  stations: number;
  variables: string[];
  per_station_availability: boolean;
  needs_credentials: string[];
  missing_credentials: string[];
  usable: boolean;
  bulk_first_use: string | null;
  cache_warm: boolean | null;
  throttle_note: string | null;
  seconds_per_station: number;
}

export interface ProvidersResponse {
  catalog_built: boolean;
  totals: { stations: number; mappable: number; off_map: number; named: number };
  variables: string[];
  providers: Provider[];
  availability: Record<string, number>;
}

export interface StationDetail {
  station_key: string;
  country: string;
  gauge_id: string;
  station_name: string | null;
  river: string | null;
  latitude: number | null;
  longitude: number | null;
  has_coords: boolean;
  altitude: number | null;
  area: number | null;
  variables: string[];
  extra: Record<string, string>;
  provider: {
    key: string;
    label: string;
    country_name: string;
    source_url: string;
    missing_credentials: string[];
    bulk_first_use: string | null;
    cache_warm: boolean | null;
    throttle_note: string | null;
  } | null;
  observed: Record<
    string,
    { state: string; rows: number; first_date: string | null; last_date: string | null }
  >;
}

export interface PreviewSeries {
  t: string[];
  mean: number[];
  min: number[];
  max: number[];
  bucket_days?: number;
}

export interface Preview {
  station_key: string;
  variable: string;
  status: "ok" | "empty" | "failed" | "blocked" | "unsupported";
  message?: string;
  rows?: number;
  first_date?: string | null;
  last_date?: string | null;
  stats?: { min: number; max: number; mean: number };
  series: PreviewSeries;
}

export interface EstimateBreakdown {
  country: string;
  provider: string;
  stations: number;
  supported: boolean;
  missing_credentials: string[];
  bulk_first_use: string | null;
  throttle_note: string | null;
}

export interface Estimate {
  stations: number;
  downloadable: number;
  unsupported: number;
  blocked: number;
  estimated_seconds: number;
  breakdown: EstimateBreakdown[];
}

export type TaskState =
  | "queued"
  | "running"
  | "done"
  | "empty"
  | "unsupported"
  | "failed"
  | "cancelled"
  | "blocked";

export interface JobTask {
  station_key: string;
  country: string;
  gauge_id: string;
  station_name: string | null;
  state: TaskState;
  rows: number;
  first_date: string | null;
  last_date: string | null;
  message: string | null;
  path: string | null;
  seconds: number;
}

export interface JobProgress {
  total: number;
  finished: number;
  counts: Partial<Record<TaskState, number>>;
  state: string;
}

export interface Job {
  id: string;
  variable: string;
  start_date: string | null;
  end_date: string | null;
  created_at: string;
  finished_at: string | null;
  state: string;
  progress: JobProgress;
  has_archive: boolean;
  unknown_keys: string[];
  tasks?: JobTask[];
}

/** Everything the sidebar can narrow the catalog by. */
export interface Filters {
  countries: string[];
  variables: string[];
  text: string;
  minArea: number | null;
  maxArea: number | null;
}

export const EMPTY_FILTERS: Filters = {
  countries: [],
  variables: [],
  text: "",
  minArea: null,
  maxArea: null,
};

/** Cluster or single station, as returned by the worker. */
export interface ClusterPoint {
  x: number;
  y: number;
  lat: number;
  lon: number;
  count: number;
  clusterId: number | null;
  station: Station | null;
  /** Dominant country in the cluster, used to colour it. */
  country: string;
}
