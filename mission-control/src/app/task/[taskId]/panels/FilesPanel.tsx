"use client";

import { useParams } from "next/navigation";
import { FilesWorkspace } from "@/components/features/FilesWorkspace";
import { useTaskData } from "../TaskStreamContext";

export function FilesPanel() {
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
