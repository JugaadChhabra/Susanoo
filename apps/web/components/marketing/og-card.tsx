import { ImageResponse } from "next/og";

export const OG_ALT = "Susanoo: AI agents are a many-headed mess. Susanoo tames them.";
export const OG_SIZE = { width: 1200, height: 630 };

/** The social preview card shared by the Open Graph and X images. */
export function ogCard(): ImageResponse {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          padding: 72,
          background: "#eef1f4",
          color: "#14202b",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 16, fontSize: 36, fontWeight: 700 }}>
          <div style={{ display: "flex", gap: 6 }}>
            {Array.from({ length: 8 }, (_, i) => (
              <div key={i} style={{ width: 8, height: 36, borderRadius: 4, background: i === 7 ? "#0e7c86" : "#2f4a63" }} />
            ))}
          </div>
          Susanoo
        </div>
        <div style={{ display: "flex", fontSize: 76, fontWeight: 700, lineHeight: 1.05, letterSpacing: -2, maxWidth: 1000 }}>
          AI agents are a many-headed mess. Susanoo tames them.
        </div>
        <div style={{ display: "flex", fontSize: 30, color: "#2f4a63" }}>
          Build an agent in plain English. Test it, fix it, prove it.
        </div>
      </div>
    ),
    OG_SIZE,
  );
}
