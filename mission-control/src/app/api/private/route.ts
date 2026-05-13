import { NextResponse } from "next/server";
import { getRedis } from "@/lib/redis";

/** Structured representation of a single private blackboard entry. */
interface PrivateEntry {
  key: string;
  type: string;
  value: string | string[] | Record<string, string> | null;
}

export async function GET(): Promise<NextResponse> {
  try {
    const redis = await getRedis();
    const entries: PrivateEntry[] = [];

    // Use SCAN to iterate without blocking Redis.
    let cursor: string = "0";
    const allKeys: string[] = [];

    do {
      const result = await redis.scan(cursor, {
        MATCH: "bmas:private:*",
        COUNT: 100,
      });

      cursor = String(result.cursor);
      allKeys.push(...result.keys);
    } while (cursor !== "0");

    // Sort for deterministic output.
    allKeys.sort();

    // Resolve each key's type and value.
    for (const key of allKeys) {
      const keyType = await redis.type(key);
      let value: PrivateEntry["value"] = null;

      switch (keyType) {
        case "string":
          value = await redis.get(key);
          break;
        case "list":
          value = await redis.lRange(key, 0, -1);
          break;
        case "set":
          value = await redis.sMembers(key);
          break;
        case "hash":
          value = await redis.hGetAll(key);
          break;
        case "zset":
          // Return sorted set as an array of "member:score" strings.
          value = (await redis.zRangeWithScores(key, 0, -1)).map(
            (entry) => `${entry.value}:${entry.score}`,
          );
          break;
        default:
          // 'none' or unsupported type — skip.
          continue;
      }

      entries.push({ key, type: keyType, value });
    }

    return NextResponse.json({ entries });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown Redis error";
    return NextResponse.json(
      { error: "Failed to read private blackboard", detail: message },
      { status: 500 },
    );
  }
}
