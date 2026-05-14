import { createClient, type RedisClientType } from "redis";
import { REDIS_URL } from "@/lib/config";

/**
 * Singleton Redis client for the Mission Control backend.
 *
 * Usage:
 *   import { getRedis } from "@/lib/redis";
 *   const redis = await getRedis();
 *
 * For blocking operations (e.g. XREAD BLOCK) call `redis.duplicate()`
 * to avoid stalling the shared connection.
 */

let client: RedisClientType | null = null;

export async function getRedis(): Promise<RedisClientType> {
  if (client === null) {
    client = createClient({ url: REDIS_URL }) as RedisClientType;

    client.on("error", (err: Error) => {
      console.error("[redis] connection error:", err.message);
    });

    await client.connect();
  }

  return client;
}
