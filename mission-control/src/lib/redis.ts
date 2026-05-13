import { createClient, type RedisClientType } from "redis";

const REDIS_URL =
  process.env.REDIS_URL ??
  "redis://:bmas-redis-secret-2026@192.168.4.240:6379";

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
