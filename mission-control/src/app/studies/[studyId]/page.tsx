import { StudyDetailClient } from "./StudyDetailClient";

export default async function StudyDetailPage({ params }: { params: Promise<{ studyId: string }> }) {
  const { studyId } = await params;
  return <StudyDetailClient studyId={studyId} />;
}
