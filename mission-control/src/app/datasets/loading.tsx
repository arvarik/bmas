import { Skeleton } from "@/components/ui/Skeleton";

export default function DatasetsLoading() {
  return (
    <div className="datasets-page" role="status" aria-label="Loading datasets">
      <Skeleton variant="list" lines={8} />
    </div>
  );
}
