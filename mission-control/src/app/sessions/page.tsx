import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { HermesSessions } from "@/components/features/HermesSessions";

export default function SessionsPage() {
  return (
    <div className="view-container agents-view">
      <div className="view-breadcrumb">
        <Link href="/">
          <ArrowLeft size={14} /> Home
        </Link>
        <span>/</span>
        <span>Hermes sessions</span>
      </div>
      <HermesSessions />
    </div>
  );
}
