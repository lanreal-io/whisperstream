import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EnhancePreviewCarousel, type IntroCard } from "./EnhancePreviewCarousel";
import examples from "./enhanceExamples.json";

let reduceMotion = false;
vi.mock("framer-motion", async (importOriginal) => {
  const actual = await importOriginal<typeof import("framer-motion")>();
  return { ...actual, useReducedMotion: () => reduceMotion };
});

const cards = examples.intro as IntroCard[];

afterEach(() => {
  cleanup();
  reduceMotion = false;
});

function region() {
  return screen.getByRole("region", { name: "Enhancement examples" });
}
function currentIndex() {
  return Number(region().getAttribute("data-carousel-index"));
}
function fill() {
  return region().querySelector<HTMLElement>("[data-dwell-fill]")!;
}
function dot(i: number) {
  return screen.getByRole("button", { name: `Show ${cards[i].title}` });
}

describe("EnhancePreviewCarousel", () => {
  it("labels the carousel, slides, controls and the email cue", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    expect(region()).toHaveAttribute("aria-roledescription", "carousel");
    const slides = region().querySelectorAll('[role="group"]');
    expect(slides).toHaveLength(3);
    cards.forEach((card, i) => {
      expect(slides[i]).toHaveAttribute("aria-roledescription", "slide");
      expect(slides[i]).toHaveAttribute("aria-label", `${i + 1} of 3: ${card.title}`);
    });
    // Only the current slide is exposed.
    expect(screen.getByRole("group", { name: `1 of 3: ${cards[0].title}` })).toBeInTheDocument();
    expect(slides[1]).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("button", { name: "Pause examples" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous example" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next example" })).toBeInTheDocument();
    expect(dot(0)).toHaveAttribute("aria-current", "true");
    expect(dot(1)).not.toHaveAttribute("aria-current");

    const email = slides[2] as HTMLElement;
    const descId = email.getAttribute("aria-describedby")!;
    expect(document.getElementById(descId)).toHaveTextContent(
      "This happens only when you dictate into an email app or website.",
    );
    expect(email.querySelector("[data-address-strip]")).toHaveTextContent("mail.example.com");
    expect(email.querySelector("[data-address-strip]")).toHaveTextContent("Email apps only");
  });

  it("renders the three example shapes: struck words, a real list, email paragraphs", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    expect(document.querySelector('[data-slide="correction"] del')).toHaveTextContent(cards[0].dropped!);
    const items = document.querySelectorAll('[data-slide="list"] ol li');
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("My passport.");
    const emailParas = document.querySelectorAll('[data-slide="email"] [data-slot="written"] p');
    expect(emailParas).toHaveLength(4);
    expect(emailParas[0]).toHaveTextContent("Hey Jordan,");
    expect(emailParas[3].querySelector("br")).not.toBeNull();
  });

  it("autoplays on the dot fill with the per-card dwell, and wraps", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    expect(region()).toHaveAttribute("data-carousel-playing", "true");
    expect(fill().style.animation).toContain("5000ms");
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(1);
    expect(fill().style.animation).toContain("6500ms");
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(2);
    expect(fill().style.animation).toContain("8000ms");
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(0);
  });

  it("pauses on hover, focus and a hidden window", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    expect(fill().style.animationPlayState).toBe("running");
    fireEvent.pointerEnter(region());
    expect(fill().style.animationPlayState).toBe("paused");
    fireEvent.pointerLeave(region());
    expect(fill().style.animationPlayState).toBe("running");

    act(() => screen.getByRole("button", { name: "Next example" }).focus());
    expect(fill().style.animationPlayState).toBe("paused");
    act(() => (document.activeElement as HTMLElement).blur());

    const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fill().style.animationPlayState).toBe("paused");
    hidden.mockReturnValue(false);
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(fill().style.animationPlayState).toBe("running");
    hidden.mockRestore();
  });

  it("arrows, dots and arrow keys navigate and stop autoplay for good", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    fireEvent.click(screen.getByRole("button", { name: "Next example" }));
    expect(currentIndex()).toBe(1);
    expect(region()).not.toHaveAttribute("data-carousel-playing");
    expect(fill().style.animation).toBe("none");
    expect(screen.getByRole("button", { name: "Play examples" })).toBeInTheDocument();
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Previous example" }));
    expect(currentIndex()).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Previous example" }));
    expect(currentIndex()).toBe(2);
    fireEvent.click(dot(1));
    expect(currentIndex()).toBe(1);
    expect(dot(1)).toHaveAttribute("aria-current", "true");
    fireEvent.keyDown(dot(1), { key: "ArrowRight" });
    expect(currentIndex()).toBe(2);
    fireEvent.keyDown(dot(2), { key: "ArrowLeft" });
    expect(currentIndex()).toBe(1);

    // Play restarts autoplay from the current card.
    fireEvent.click(screen.getByRole("button", { name: "Play examples" }));
    expect(region()).toHaveAttribute("data-carousel-playing", "true");
    expect(fill().style.animation).toContain("6500ms");
  });

  it("Pause and Play toggle autoplay", () => {
    render(<EnhancePreviewCarousel cards={cards} />);
    fireEvent.click(screen.getByRole("button", { name: "Pause examples" }));
    expect(region()).not.toHaveAttribute("data-carousel-playing");
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Play examples" }));
    expect(region()).toHaveAttribute("data-carousel-playing", "true");
  });

  it("reduced motion disables autoplay, the fill and the pause button", () => {
    reduceMotion = true;
    render(<EnhancePreviewCarousel cards={cards} />);
    expect(region()).not.toHaveAttribute("data-carousel-playing");
    expect(fill().style.animation).toBe("none");
    expect(screen.queryByRole("button", { name: /examples$/ })).toBeNull();
    fireEvent.animationEnd(fill());
    expect(currentIndex()).toBe(0);
    const slide = screen.getByRole("group", { name: `1 of 3: ${cards[0].title}` });
    expect(slide.style.transition).toBe("none");
    fireEvent.click(screen.getByRole("button", { name: "Next example" }));
    expect(currentIndex()).toBe(1);
  });
});
