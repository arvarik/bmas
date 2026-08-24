"use client";

/**
 * BackLink — the one back affordance for every detail page.
 *
 * A detail page always links up to its parent collection with a plain
 * href: predictable, sharable, and correct on deep links and refreshes
 * (unlike history.back(), which can leave the app). Same icon, same
 * size, same muted style, and a 36px tap target everywhere.
 */

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

export function BackLink({ href, label }: { href: string; label: string }) {
  return (
    <Link href={href} className="back-link">
      <ArrowLeft size={14} aria-hidden="true" />
      <span>{label}</span>
    </Link>
  );
}

export default BackLink;
