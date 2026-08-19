import { AGENT_HOSTS } from "@/lib/config";

const AGENT_TIMEOUT_MS = 8_000;

export class HermesAgentRequestError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown = null,
  ) {
    super(message);
    this.name = "HermesAgentRequestError";
  }
}

export function configuredAgentRoles(): string[] {
  return Object.keys(AGENT_HOSTS);
}

export function getAgentBaseUrl(node: string): string | null {
  if (!Object.prototype.hasOwnProperty.call(AGENT_HOSTS, node)) return null;
  return AGENT_HOSTS[node] ?? null;
}

function agentHeaders(headers?: HeadersInit): Headers {
  const result = new Headers(headers);
  const key = process.env.BMAS_EXECUTE_KEY;
  if (key) result.set("Authorization", `Bearer ${key}`);
  return result;
}

export async function requestHermesAgent(
  node: string,
  path: string,
  init: RequestInit = {},
): Promise<unknown> {
  const baseUrl = getAgentBaseUrl(node);
  if (!baseUrl) {
    throw new HermesAgentRequestError(
      `Unknown agent node '${node}'`,
      400,
      { expected: configuredAgentRoles() },
    );
  }

  let response: Response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      ...init,
      cache: "no-store",
      headers: agentHeaders(init.headers),
      signal: init.signal ?? AbortSignal.timeout(AGENT_TIMEOUT_MS),
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Unknown upstream error";
    throw new HermesAgentRequestError(
      `Agent node '${node}' is unavailable: ${detail}`,
      503,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new HermesAgentRequestError(
      `Agent node '${node}' returned HTTP ${response.status}`,
      response.status,
      body,
    );
  }
  return body;
}

export function hermesAgentErrorResponse(error: unknown): Response {
  if (error instanceof HermesAgentRequestError) {
    return Response.json(
      {
        error: error.message,
        ...(error.body == null ? {} : { upstream: error.body }),
      },
      { status: error.status, headers: { "Cache-Control": "no-store" } },
    );
  }
  return Response.json(
    { error: "Hermes agent request failed" },
    { status: 500, headers: { "Cache-Control": "no-store" } },
  );
}

export function dataList(value: unknown): Record<string, unknown>[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return [];
  const data = (value as Record<string, unknown>).data;
  return Array.isArray(data)
    ? data.filter(
        (item): item is Record<string, unknown> =>
          typeof item === "object" && item !== null && !Array.isArray(item),
      )
    : [];
}
