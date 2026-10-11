import { ogCard, OG_ALT, OG_SIZE } from "@/components/marketing/og-card";

export const alt = OG_ALT;
export const size = OG_SIZE;
export const contentType = "image/png";

export default function Image() {
  return ogCard();
}
