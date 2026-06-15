import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { ClientShell } from "./ClientShell";
import { PROJECT_NAME, PROJECT_DESCRIPTION } from "@/lib/config";

export const dynamic = "force-dynamic";

// ── Font Loading ─────────────────────────────────────────────────────
// next/font/google self-hosts fonts — no external requests at runtime.

const inter = Inter({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  weight: ["400"],
  variable: "--font-jetbrains-mono",
  display: "swap",
});

// ── Metadata ─────────────────────────────────────────────────────────

export async function generateMetadata(): Promise<Metadata> {
  return {
    title: {
      default: `${PROJECT_NAME} — Mission Control`,
      template: `%s | ${PROJECT_NAME}`,
    },
    description: PROJECT_DESCRIPTION,
    keywords: [
      "bMAS",
      "Multi-Agent Swarm",
      "Biomimetic AI",
      "Stigmergy",
      "Decentralized AI Swarm",
      "Autonomous Agents",
      "Blackboard Architecture",
    ],
    authors: [{ name: "bMAS Swarm Development Team" }],
    creator: "bMAS Swarm",
    metadataBase: new URL("https://stigmergic.bmas.ai"),
    alternates: {
      canonical: "/",
    },
    openGraph: {
      title: `${PROJECT_NAME} — Mission Control`,
      description: PROJECT_DESCRIPTION,
      url: "https://stigmergic.bmas.ai",
      siteName: PROJECT_NAME,
      locale: "en_US",
      type: "website",
    },
    twitter: {
      card: "summary_large_image",
      title: `${PROJECT_NAME} — Mission Control`,
      description: PROJECT_DESCRIPTION,
    },
    icons: {
      icon: [
        { url: "/icon.png", type: "image/png" },
      ],
      apple: [
        { url: "/apple-icon.png", sizes: "180x180", type: "image/png" },
      ],
    },
  };
}


// ── Root Layout ──────────────────────────────────────────────────────

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrainsMono.variable}`}
    >
      <body>
        <ClientShell>{children}</ClientShell>
      </body>
    </html>
  );
}
