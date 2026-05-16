/**
 * Landing Page — / (Server Component)
 *
 * Thin server wrapper that passes PROJECT_NAME to the client component.
 * PROJECT_NAME is loaded from bmas.yaml via readFileSync — it can only
 * be accessed in server components.
 */

import { PROJECT_NAME } from "@/lib/config";
import { LandingPageClient } from "./LandingPageClient";

export default function LandingPage() {
  return <LandingPageClient projectName={PROJECT_NAME} />;
}
