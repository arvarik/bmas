/**
 * Landing Page — / (Server Component)
 *
 * Thin server wrapper that passes PROJECT_NAME to the client component.
 * PROJECT_NAME is loaded from bmas.yaml via readFileSync — it can only
 * be accessed in server components.
 */

import {
  PROJECT_NAME,
  STORAGE_ALLOWED_UPLOAD_TYPES,
  STORAGE_ENABLED,
  STORAGE_MAX_UPLOAD_MB,
} from "@/lib/config";
import { LandingPageClient } from "./LandingPageClient";

export default function LandingPage() {
  return (
    <LandingPageClient
      projectName={PROJECT_NAME}
      storageEnabled={STORAGE_ENABLED}
      maxUploadMb={STORAGE_MAX_UPLOAD_MB}
      allowedUploadTypes={STORAGE_ALLOWED_UPLOAD_TYPES}
    />
  );
}
