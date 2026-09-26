/** Typed client for the wateratlas API. */

import type {
  Estimate,
  Job,
  Preview,
  ProvidersResponse,
  StationDetail,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* the error body was not JSON; the status line is all we have */
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  providers: () => request<ProvidersResponse>("/api/providers"),

  station: (key: string) =>
    request<StationDetail>(`/api/station?key=${encodeURIComponent(key)}`),

  preview: (key: string, variable: string, startDate?: string, endDate?: string) => {
    const params = new URLSearchParams({ key, variable });
    if (startDate) params.set("start_date", startDate);
    if (endDate) params.set("end_date", endDate);
    return request<Preview>(`/api/station/preview?${params}`);
  },

  variablesForSelection: (stationKeys: string[]) =>
    request<{ variables: { variable: string; stations: number }[] }>(
      "/api/selection/variables",
      { method: "POST", body: JSON.stringify({ station_keys: stationKeys }) },
    ),

  estimate: (stationKeys: string[], variable: string) =>
    request<Estimate>("/api/selection/estimate", {
      method: "POST",
      body: JSON.stringify({ station_keys: stationKeys, variable }),
    }),

  createDownload: (
    stationKeys: string[],
    variable: string,
    startDate: string | null,
    endDate: string | null,
  ) =>
    request<Job>("/api/downloads", {
      method: "POST",
      body: JSON.stringify({
        station_keys: stationKeys,
        variable,
        start_date: startDate,
        end_date: endDate,
      }),
    }),

  job: (jobId: string) => request<Job>(`/api/downloads/${jobId}`),

  cancelJob: (jobId: string) =>
    request<Job>(`/api/downloads/${jobId}/cancel`, { method: "POST" }),

  archiveUrl: (jobId: string) => `/api/downloads/${jobId}/archive`,

  warmProvider: (country: string) =>
    request<{ country: string; state: string }>(`/api/providers/${country}/warm`, {
      method: "POST",
    }),
};

/**
 * Subscribes to a job's progress stream.
 *
 * Returns an unsubscribe function. The browser reconnects an EventSource on its
 * own, and the server replays from `since`, so a dropped connection does not
 * lose events.
 */
export function subscribeToJob(
  jobId: string,
  onEvent: (event: Record<string, unknown>) => void,
): () => void {
  const source = new EventSource(`/api/downloads/${jobId}/events`);
  source.onmessage = (message) => {
    try {
      onEvent(JSON.parse(message.data));
    } catch {
      /* keep-alive comments are not JSON */
    }
  };
  source.onerror = () => {
    // The job has finished and the server closed the stream, or the connection
    // dropped. Either way the caller polls once more to settle the final state.
    source.close();
    onEvent({ kind: "stream.closed" });
  };
  return () => source.close();
}
