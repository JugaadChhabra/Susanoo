import type { Metadata } from "next";
import {
  Dela_Gothic_One,
  Geist,
  Geist_Mono,
  IBM_Plex_Mono,
  IBM_Plex_Sans_Condensed,
  Shippori_Mincho_B1,
} from "next/font/google";
import { SITE_DESCRIPTION, SITE_TITLE, siteUrl } from "@/lib/marketing";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

// Stamp design system fonts: headlines, Japanese seals, body text, labels.
const dela = Dela_Gothic_One({ weight: "400", subsets: ["latin"], variable: "--font-dela", display: "swap" });
const shippori = Shippori_Mincho_B1({
  weight: ["600", "800"],
  subsets: ["latin"],
  variable: "--font-shippori",
  display: "swap",
  preload: false,
});
const plexCondensed = IBM_Plex_Sans_Condensed({
  weight: ["400", "500", "600"],
  subsets: ["latin"],
  variable: "--font-plex-condensed",
  display: "swap",
});
const plexMono = IBM_Plex_Mono({ weight: ["400", "500"], subsets: ["latin"], variable: "--font-plex-mono", display: "swap" });

export const metadata: Metadata = {
  metadataBase: siteUrl({
    NEXT_PUBLIC_SITE_URL: process.env.NEXT_PUBLIC_SITE_URL,
    VERCEL_PROJECT_PRODUCTION_URL: process.env.VERCEL_PROJECT_PRODUCTION_URL,
  }),
  title: SITE_TITLE,
  description: SITE_DESCRIPTION,
  applicationName: "Susanoo",
  openGraph: { type: "website", siteName: "Susanoo", title: SITE_TITLE, description: SITE_DESCRIPTION },
  twitter: { card: "summary_large_image", title: SITE_TITLE, description: SITE_DESCRIPTION },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} ${dela.variable} ${shippori.variable} ${plexCondensed.variable} ${plexMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
