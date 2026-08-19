import type { Metadata } from "next";
import "./globals.css";
import { ClientShell } from "./ClientShell";
import { PROJECT_DESCRIPTION } from "@/lib/config";

const PRODUCT_NAME = "Stigmergic";

export const dynamic = "force-dynamic";

export async function generateMetadata(): Promise<Metadata> {
  return {
    title: {
      default: `${PRODUCT_NAME} — Mission Control`,
      template: `%s | ${PRODUCT_NAME}`,
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
    authors: [{ name: "Stigmergic Development Team" }],
    creator: PRODUCT_NAME,
    metadataBase: new URL("https://stigmergic.bmas.ai"),
    alternates: {
      canonical: "/",
    },
    openGraph: {
      title: `${PRODUCT_NAME} — Mission Control`,
      description: PROJECT_DESCRIPTION,
      url: "https://stigmergic.bmas.ai",
      siteName: PRODUCT_NAME,
      locale: "en_US",
      type: "website",
    },
    twitter: {
      card: "summary_large_image",
      title: `${PRODUCT_NAME} — Mission Control`,
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
