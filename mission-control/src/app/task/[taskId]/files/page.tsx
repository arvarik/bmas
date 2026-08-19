"use client";

import { useParams } from "next/navigation";
import { FilesWorkspace } from "@/components/features/FilesWorkspace";
import { useTaskData } from "../TaskStreamContext";

export default function FilesPage() {
  const { taskId } = useParams();
  const { liveArtifacts, liveFiles } = useTaskData();
  return (
    <FilesWorkspace
      taskId={taskId as string}
      liveFiles={liveFiles}
      liveArtifacts={liveArtifacts}
    />
  );
}
