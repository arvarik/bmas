import { TaskDetailClient } from "./TaskDetailClient";

export default async function TaskPage({
  params,
}: {
  params: Promise<{ taskId: string }>;
}) {
  const { taskId } = await params;
  return <TaskDetailClient taskId={taskId} />;
}
