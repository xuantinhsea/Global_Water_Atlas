import { useMemo } from "react";
import type { Preview } from "../lib/types";
import { formatNumber, variableUnit } from "../lib/palette";

interface Props {
  preview: Preview;
}

const WIDTH = 520;
const HEIGHT = 168;
const PAD = { top: 12, right: 10, bottom: 22, left: 46 };

/**
 * A hydrograph drawn to one scale: the min/max band behind the mean line, with
 * axis labels that name values the series actually reaches.
 */
export function Hydrograph({ preview }: Props) {
  const unit = variableUnit(preview.variable);

  const geometry = useMemo(() => {
    const { t, mean, min, max } = preview.series;
    if (!t.length) return null;

    const innerWidth = WIDTH - PAD.left - PAD.right;
    const innerHeight = HEIGHT - PAD.top - PAD.bottom;

    const lowest = Math.min(...min);
    const highest = Math.max(...max);
    const span = highest - lowest || 1;
    // A little headroom so the peak is not welded to the top edge — but never
    // below zero for a quantity that cannot be negative. Water temperature is
    // the one variable here that legitimately goes below zero.
    const padded = lowest - span * 0.05;
    const yMin = lowest >= 0 && padded < 0 ? 0 : padded;
    const yMax = highest + span * 0.08;

    const x = (i: number) => PAD.left + (i / Math.max(1, t.length - 1)) * innerWidth;
    const y = (value: number) =>
      PAD.top + innerHeight - ((value - yMin) / (yMax - yMin)) * innerHeight;

    const meanPath = mean.map((value, i) => `${i ? "L" : "M"}${x(i)},${y(value)}`).join("");
    // The band runs forward along the maxima and back along the minima.
    const upper = max.map((value, i) => `${i ? "L" : "M"}${x(i)},${y(value)}`).join("");
    let lower = "";
    for (let i = min.length - 1; i >= 0; i -= 1) lower += `L${x(i)},${y(min[i])}`;
    const bandPath = `${upper}${lower}Z`;

    const ticks = [yMin, (yMin + yMax) / 2, yMax].map((value) => ({ value, y: y(value) }));
    const dateTicks = [0, Math.floor(t.length / 2), t.length - 1].map((i) => ({
      label: t[i]?.slice(0, 7) ?? "",
      x: x(i),
    }));

    return { meanPath, bandPath, ticks, dateTicks };
  }, [preview]);

  if (preview.status !== "ok" || !geometry) {
    return (
      <div className="riv-chart-empty">
        {preview.message ?? "No data for this range."}
      </div>
    );
  }

  return (
    <figure className="riv-chart">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`${preview.variable} from ${preview.first_date} to ${preview.last_date}`}
        preserveAspectRatio="none"
      >
        {geometry.ticks.map((tick) => (
          <g key={tick.value}>
            <line
              x1={PAD.left}
              x2={WIDTH - PAD.right}
              y1={tick.y}
              y2={tick.y}
              className="riv-grid"
            />
            <text x={PAD.left - 6} y={tick.y + 3} className="riv-axis" textAnchor="end">
              {formatNumber(tick.value, 2)}
            </text>
          </g>
        ))}

        <path d={geometry.bandPath} className="riv-band" />
        <path d={geometry.meanPath} className="riv-line" />

        {geometry.dateTicks.map((tick, i) => (
          <text
            key={`${tick.label}-${i}`}
            x={tick.x}
            y={HEIGHT - 6}
            className="riv-axis"
            textAnchor={i === 0 ? "start" : i === 2 ? "end" : "middle"}
          >
            {tick.label}
          </text>
        ))}
      </svg>

      <figcaption>
        {preview.rows?.toLocaleString()} values, {preview.first_date} to {preview.last_date}
        {preview.series.bucket_days && preview.series.bucket_days > 1
          ? ` · shown in ${preview.series.bucket_days}-value buckets (min–max band)`
          : ""}
        {preview.stats && (
          <>
            {" · "}min {formatNumber(preview.stats.min, 2)} · mean{" "}
            {formatNumber(preview.stats.mean, 2)} · max {formatNumber(preview.stats.max, 2)} {unit}
          </>
        )}
      </figcaption>
    </figure>
  );
}
