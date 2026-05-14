import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { AppShell } from "./AppShell";
import { PROJECT_NAME, PROJECT_DESCRIPTION } from "@/lib/config";

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
    title: `Mission Control — ${PROJECT_NAME}`,
    description: PROJECT_DESCRIPTION,
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
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
