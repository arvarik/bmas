import { readFileSync } from "node:fs";
import { join } from "node:path";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

interface RequestStub {
  url: string;
  method: string;
  mode: string;
  headers: { get: (name: string) => string | null };
}

interface FetchEventStub {
  request: RequestStub;
  respondWith: (response: Promise<unknown>) => void;
}

const serviceWorker = readFileSync(join(process.cwd(), "public", "sw.js"), "utf8");

function createFetchListener() {
  const listeners = new Map<string, (event: FetchEventStub) => void>();
  const caches = {
    match: vi.fn().mockResolvedValue({ cached: true }),
    open: vi.fn().mockResolvedValue({
      addAll: vi.fn(),
      put: vi.fn(),
    }),
    keys: vi.fn().mockResolvedValue([]),
    delete: vi.fn(),
  };
  const worker = {
    location: { origin: "https://mission.test" },
    clients: { claim: vi.fn() },
    skipWaiting: vi.fn(),
    addEventListener: (
      type: string,
      listener: (event: FetchEventStub) => void,
    ) => listeners.set(type, listener),
  };

  runInNewContext(serviceWorker, {
    self: worker,
    caches,
    URL,
    fetch: vi.fn(),
  });

  const listener = listeners.get("fetch");
  if (!listener) throw new Error("The service worker did not register a fetch listener.");
  return { listener, caches };
}

function request(
  pathname: string,
  options: Partial<Pick<RequestStub, "method" | "mode">> & { accept?: string } = {},
): RequestStub {
  return {
    url: pathname.startsWith("http") ? pathname : `https://mission.test${pathname}`,
    method: options.method ?? "GET",
    mode: options.mode ?? "cors",
    headers: {
      get: (name) => name.toLowerCase() === "accept" ? options.accept ?? null : null,
    },
  };
}

function dispatch(listener: (event: FetchEventStub) => void, input: RequestStub) {
  const respondWith = vi.fn();
  listener({ request: input, respondWith });
  return respondWith;
}

describe("service worker cache policy", () => {
  it.each([
    request("/api/tasks"),
    request("/", { mode: "navigate" }),
    request("/settings", { accept: "text/html" }),
    request("/icon.png", { method: "POST" }),
    request("/arbitrary.json"),
    request("https://external.test/icon.png"),
  ])("keeps ineligible request $url on the network", (input) => {
    const { listener } = createFetchListener();
    expect(dispatch(listener, input)).not.toHaveBeenCalled();
  });

  it.each([
    request("/icon.png"),
    request("/_next/static/chunks/app.js"),
  ])("uses the cache for eligible request $url", async (input) => {
    const { listener, caches } = createFetchListener();
    const respondWith = dispatch(listener, input);
    expect(respondWith).toHaveBeenCalledOnce();
    await respondWith.mock.calls[0]?.[0];
    expect(caches.match).toHaveBeenCalledOnce();
  });
});
