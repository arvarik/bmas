import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";

const HYDRATION_PAGE_LIMIT = 1_000;
const HYDRATION_PAGE_ATTEMPTS = 2;

async function fetchOptional(path: string): Promise<unknown | null> {
  try {
    const response = await daemonFetch(`${DAEMON_BASE_URL}${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    if (!response.ok) return null;
    return await response.json() as unknown;
  } catch {
    return null;
  }
}

function rowsFromEnvelope(value: unknown, field: string): unknown[] {
  if (!value || typeof value !== "object") {
    throw new Error(`Hydration source ${field} returned an invalid envelope`);
  }
  const rows = (value as Record<string, unknown>)[field];
  if (!Array.isArray(rows)) {
    throw new Error(`Hydration source ${field} returned an invalid collection`);
  }
  return rows;
}

async function fetchTailPage(
  path: string,
  field: string,
  source: string,
): Promise<unknown> {
  const fetchPage = async (page: number): Promise<unknown> => {
    const pagePath = `${path}?limit=${HYDRATION_PAGE_LIMIT}&offset=${page * HYDRATION_PAGE_LIMIT}`;
    let lastError: Error | null = null;
    for (let attempt = 0; attempt < HYDRATION_PAGE_ATTEMPTS; attempt += 1) {
      try {
        const response = await daemonFetch(`${DAEMON_BASE_URL}${pagePath}`, {
          cache: "no-store",
          signal: AbortSignal.timeout(5_000),
        });
        if (!response.ok) {
          throw new Error(`${source} hydration returned HTTP ${response.status}`);
        }
        const value = await response.json() as unknown;
        rowsFromEnvelope(value, field);
        return value;
      } catch (error) {
        lastError = error instanceof Error ? error : new Error(`${source} hydration failed`);
      }
    }
    throw lastError ?? new Error(`${source} hydration failed`);
  };
  let latest = await fetchPage(0);
  if (rowsFromEnvelope(latest, field).length < HYDRATION_PAGE_LIMIT) return latest;

  let lowerPage = 0;
  let upperPage = 1;
  while (upperPage <= 1_048_576) {
    const candidate = await fetchPage(upperPage);
    const count = rowsFromEnvelope(candidate, field).length;
    if (count === 0) break;
    latest = candidate;
    if (count < HYDRATION_PAGE_LIMIT) return candidate;
    lowerPage = upperPage;
    upperPage *= 2;
  }

  while (upperPage - lowerPage > 1) {
    const middlePage = Math.floor((lowerPage + upperPage) / 2);
    const candidate = await fetchPage(middlePage);
    const count = rowsFromEnvelope(candidate, field).length;
    if (count === 0) {
      upperPage = middlePage;
      continue;
    }
    latest = candidate;
    lowerPage = middlePage;
    if (count < HYDRATION_PAGE_LIMIT) return candidate;
  }
  return latest;
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;
  if (!taskId || !/^[a-zA-Z0-9_-]{1,64}$/.test(taskId)) {
    return NextResponse.json({ error: "Invalid task ID" }, { status: 400 });
  }
  const taskPath = `/tasks/${encodeURIComponent(taskId)}`;

  try {
    const [detailResponse, board, turns, cost, logs, traces] = await Promise.all([
      daemonFetch(`${DAEMON_BASE_URL}${taskPath}`, {
        cache: "no-store",
        signal: AbortSignal.timeout(5_000),
      }),
      fetchOptional(`${taskPath}/board`),
      fetchOptional(`${taskPath}/turns`),
      fetchOptional(`${taskPath}/cost`),
      fetchTailPage(`${taskPath}/logs`, "entries", "logs"),
      fetchTailPage(`${taskPath}/trace`, "traces", "traces"),
    ]);
    if (!detailResponse.ok) {
      return NextResponse.json(
        { error: `Daemon returned ${detailResponse.status}` },
        { status: detailResponse.status },
      );
    }
    const detail = await detailResponse.json() as unknown;
    return NextResponse.json({ detail, board, turns, cost, logs, traces });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Daemon unreachable", detail },
      { status: 503 },
    );
  }
}
