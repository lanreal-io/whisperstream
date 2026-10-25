import { useEffect } from "react";

export function SnapGuide() {
  useEffect(() => {
    document.documentElement.style.setProperty(
      "background",
      "transparent",
      "important",
    );
    document.body.style.setProperty("background", "transparent", "important");
  }, []);

  return (
    <div
      aria-hidden="true"
      className="h-full w-full"
      style={{
        background: "color-mix(in srgb, var(--color-frost) 35%, transparent)",
      }}
    />
  );
}
