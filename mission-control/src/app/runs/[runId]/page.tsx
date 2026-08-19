import { Suspense } from "react";
import { RunDetailClient } from "./RunDetailClient";

export default async function RunDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return <Suspense fallback={<div className="page-loading">Loading run…</div>}><RunDetailClient runId={runId} /></Suspense>;
}
