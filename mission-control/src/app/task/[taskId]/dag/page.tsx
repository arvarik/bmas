/**
 * /task/[taskId]/dag → /task/[taskId]?tab=dag
 *
 * Task tabs live on one page. Old deep links keep working.
 */
import { redirect } from "next/navigation";

export default async function LegacyTabRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ taskId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { taskId } = await params;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(await searchParams)) {
    if (typeof value === "string") query.set(key, value);
  }
  query.set("tab", "dag");
  redirect(`/task/${encodeURIComponent(taskId)}?${query.toString()}`);
}
