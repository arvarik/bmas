/**
 * Landing Page — / (Server Component)
 *
 * Thin server wrapper that passes safe storage limits to the client.
 */

import {
  STORAGE_ALLOWED_UPLOAD_TYPES,
  STORAGE_ENABLED,
  STORAGE_MAX_UPLOAD_MB,
} from "@/lib/config";
import { LandingPageClient } from "./LandingPageClient";

export default function LandingPage() {
  return (
    <LandingPageClient
      storageEnabled={STORAGE_ENABLED}
      maxUploadMb={STORAGE_MAX_UPLOAD_MB}
      allowedUploadTypes={STORAGE_ALLOWED_UPLOAD_TYPES}
    />
  );
}
