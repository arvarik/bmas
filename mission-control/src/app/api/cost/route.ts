import { NextResponse } from "next/server";
import { getRedis } from "@/lib/redis";

const COST_KEY = "bmas:metrics:cost";
const TOKENS_KEY = "bmas:metrics:tokens";

/** Shape of the cost/token response. */
interface CostMetrics {
  cost: Record<string, string>;
  tokens: Record<string, string>;
}

export async function GET(): Promise<NextResponse> {
  try {
    const redis = await getRedis();

    const [cost, tokens] = await Promise.all([
      redis.hGetAll(COST_KEY),
      redis.hGetAll(TOKENS_KEY),
    ]);

    const metrics: CostMetrics = { cost, tokens };
    return NextResponse.json(metrics);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown Redis error";
    return NextResponse.json(
      { error: "Failed to read cost metrics", detail: message },
      { status: 500 },
    );
  }
}
