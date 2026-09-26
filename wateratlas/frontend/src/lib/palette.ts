/**
 * Categorical colours for the providers, plus display helpers.
 *
 * Two dozen categories is past the point where anyone can read hue alone, so the
 * colours are a cross-reference to the legend rather than a standalone encoding
 * — every circle is also identified on hover and in the detail panel.
 *
 * Values are chosen to stay legible on street, terrain and satellite tiles.
 */

export const COUNTRY_COLORS: Record<string, string> = {
  usa: "#1f6f8b",
  canada: "#d94f3d",
  brazil: "#2e8b57",
  australia: "#e08a1e",
  france: "#7a5fb0",
  uk_ea: "#c2437f",
  uk_nrfa: "#8a6d3b",
  norway: "#3b8fd4",
  spain: "#b8332a",
  poland: "#4aa08c",
  czech: "#9a6fd4",
  japan: "#d4613b",
  chile: "#5b8c2a",
  portugal: "#1f8a99",
  slovenia: "#c9932b",
  southafrica: "#6b7fd4",
  lithuania: "#a8477a",
  germany_berlin: "#4d6b7a",
  thailand: "#c4562f",
  thailand_rain: "#7f9c3a",
  mrc: "#2f7d7a",
  // Amber, to stand apart from USGS teal and Canada red where they share a coast.
  noaa_tides: "#e8a317",
  philippines: "#b0478f",
  pagasa_dams: "#7b5ea8",
  pagasa_stations: "#3f7fbf",
};

const FALLBACK = "#6b7f88";

export function colorForCountry(country: string): string {
  return COUNTRY_COLORS[country] ?? FALLBACK;
}

/** `discharge_daily_mean` -> `Discharge · daily mean`. */
export function variableLabel(variable: string): string {
  const parts = variable.split("_");
  const quantity = parts[0].replace(/-/g, " ");
  const rest = parts.slice(1).join(" ");
  const label = quantity.charAt(0).toUpperCase() + quantity.slice(1);
  return rest ? `${label} · ${rest}` : label;
}

/** SI unit for a variable, matching what the fetchers convert to. */
export function variableUnit(variable: string): string {
  if (variable.startsWith("discharge")) return "m³/s";
  if (variable.startsWith("stage")) return "m";
  if (variable.startsWith("water-temperature")) return "°C";
  // Covers both the catchment average and single-gauge precipitation.
  if (variable.includes("precipitation")) return "mm";
  return "";
}

export function formatNumber(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (Math.abs(value) >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return value.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function formatCount(value: number): string {
  return value.toLocaleString();
}

/** "6 min 20 s" — job estimates are never precise enough for seconds alone. */
export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    const rest = Math.round(seconds % 60);
    return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}
