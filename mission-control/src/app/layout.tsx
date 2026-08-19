import type { Metadata } from "next";
import "./globals.css";
import { ClientShell } from "./ClientShell";
import { PROJECT_DESCRIPTION, PROJECT_NAME } from "@/lib/config";

export const dynamic = "force-dynamic";

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
      "Blackboard AI",
      "Classic Multi-Agent Runtime",
      "Durable Blackboard",
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
      icon: [{ url: "/icon.png", type: "image/png" }],
      apple: [
        { url: "/apple-icon.png", sizes: "180x180", type: "image/png" },
      ],
    },
  };
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        <ClientShell>{children}</ClientShell>
      </body>
    </html>
  );
}
