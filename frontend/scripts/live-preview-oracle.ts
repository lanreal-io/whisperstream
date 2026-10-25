export interface ExactGoldAlignment {
  exactCovered: boolean[];
  exactVisiblePositions: number[];
  alignedVisiblePositions: number[];
  furthestGoldPosition: number;
}

export interface GoldProtectionCandidate {
  exactVisiblePosition: number;
  visibleText: string;
  mutableStartCharCount: number;
}

export function comparableWords(text: string): string[] {
  const normalized = text.trim().replace(/\s+/g, " ");
  if (!normalized) return [];
  return normalized
    .split(" ")
    .map((word) =>
      word
        .normalize("NFKC")
        .toLocaleLowerCase("en-US")
        .replace(/[^\p{L}\p{N}]+/gu, ""),
    )
    .filter(Boolean);
}

export function exactVisibleGoldAlignment(
  goldText: string,
  visibleText: string,
  maximumGoldPosition?: number,
): ExactGoldAlignment {
  const completeGold = comparableWords(goldText);
  const gold = completeGold.slice(
    0,
    maximumGoldPosition === undefined
      ? completeGold.length
      : Math.min(completeGold.length, maximumGoldPosition),
  );
  const visible = comparableWords(visibleText);
  const prefixCosts = Array.from(
    { length: gold.length + 1 },
    () => new Uint16Array(visible.length + 1),
  );
  const prefixExactMatches = Array.from(
    { length: gold.length + 1 },
    () => new Uint16Array(visible.length + 1),
  );
  for (let goldIndex = 0; goldIndex <= gold.length; goldIndex += 1) {
    prefixCosts[goldIndex][0] = goldIndex;
  }
  for (
    let visibleIndex = 0;
    visibleIndex <= visible.length;
    visibleIndex += 1
  ) {
    prefixCosts[0][visibleIndex] = visibleIndex;
  }
  for (let goldIndex = 1; goldIndex <= gold.length; goldIndex += 1) {
    for (
      let visibleIndex = 1;
      visibleIndex <= visible.length;
      visibleIndex += 1
    ) {
      const equal =
        gold[goldIndex - 1] === visible[visibleIndex - 1];
      const candidates = [
        {
          cost: prefixCosts[goldIndex - 1][visibleIndex] + 1,
          exact: prefixExactMatches[goldIndex - 1][visibleIndex],
        },
        {
          cost: prefixCosts[goldIndex][visibleIndex - 1] + 1,
          exact: prefixExactMatches[goldIndex][visibleIndex - 1],
        },
        {
          cost:
            prefixCosts[goldIndex - 1][visibleIndex - 1] +
            (equal ? 0 : 1),
          exact:
            prefixExactMatches[goldIndex - 1][visibleIndex - 1] +
            (equal ? 1 : 0),
        },
      ];
      const minimumCost = Math.min(
        ...candidates.map((candidate) => candidate.cost),
      );
      const maximumExact = Math.max(
        ...candidates
          .filter((candidate) => candidate.cost === minimumCost)
          .map((candidate) => candidate.exact),
      );
      prefixCosts[goldIndex][visibleIndex] = minimumCost;
      prefixExactMatches[goldIndex][visibleIndex] = maximumExact;
    }
  }
  let goldEnd = 0;
  let bestCost = prefixCosts[0][visible.length];
  let bestExact = prefixExactMatches[0][visible.length];
  for (let index = 1; index <= gold.length; index += 1) {
    const candidateCost = prefixCosts[index][visible.length];
    const candidateExact = prefixExactMatches[index][visible.length];
    if (
      candidateCost < bestCost ||
      (candidateCost === bestCost && candidateExact > bestExact)
    ) {
      bestCost = candidateCost;
      bestExact = candidateExact;
      goldEnd = index;
    }
  }

  const suffixCosts = Array.from(
    { length: goldEnd + 1 },
    () => new Uint16Array(visible.length + 1),
  );
  const suffixExactMatches = Array.from(
    { length: goldEnd + 1 },
    () => new Uint16Array(visible.length + 1),
  );
  for (let goldIndex = goldEnd; goldIndex >= 0; goldIndex -= 1) {
    suffixCosts[goldIndex][visible.length] = goldEnd - goldIndex;
  }
  for (
    let visibleIndex = visible.length;
    visibleIndex >= 0;
    visibleIndex -= 1
  ) {
    suffixCosts[goldEnd][visibleIndex] = visible.length - visibleIndex;
  }
  for (let goldIndex = goldEnd - 1; goldIndex >= 0; goldIndex -= 1) {
    for (
      let visibleIndex = visible.length - 1;
      visibleIndex >= 0;
      visibleIndex -= 1
    ) {
      const equal = gold[goldIndex] === visible[visibleIndex];
      const candidates = [
        {
          cost: suffixCosts[goldIndex + 1][visibleIndex] + 1,
          exact: suffixExactMatches[goldIndex + 1][visibleIndex],
        },
        {
          cost: suffixCosts[goldIndex][visibleIndex + 1] + 1,
          exact: suffixExactMatches[goldIndex][visibleIndex + 1],
        },
        {
          cost:
            suffixCosts[goldIndex + 1][visibleIndex + 1] +
            (equal ? 0 : 1),
          exact:
            suffixExactMatches[goldIndex + 1][visibleIndex + 1] +
            (equal ? 1 : 0),
        },
      ];
      const minimumCost = Math.min(
        ...candidates.map((candidate) => candidate.cost),
      );
      const maximumExact = Math.max(
        ...candidates
          .filter((candidate) => candidate.cost === minimumCost)
          .map((candidate) => candidate.exact),
      );
      suffixCosts[goldIndex][visibleIndex] = minimumCost;
      suffixExactMatches[goldIndex][visibleIndex] = maximumExact;
    }
  }

  const exactCovered = new Array<boolean>(gold.length).fill(false);
  const exactVisiblePositions = new Array<number>(gold.length).fill(-1);
  const alignedVisiblePositions = new Array<number>(gold.length).fill(-1);
  let goldIndex = 0;
  let visibleIndex = 0;
  let furthestGoldPosition = 0;
  while (goldIndex < goldEnd && visibleIndex < visible.length) {
    if (
      gold[goldIndex] === visible[visibleIndex] &&
      suffixCosts[goldIndex][visibleIndex] ===
        suffixCosts[goldIndex + 1][visibleIndex + 1] &&
      suffixExactMatches[goldIndex][visibleIndex] ===
        suffixExactMatches[goldIndex + 1][visibleIndex + 1] + 1
    ) {
      exactCovered[goldIndex] = true;
      exactVisiblePositions[goldIndex] = visibleIndex;
      alignedVisiblePositions[goldIndex] = visibleIndex;
      furthestGoldPosition = goldIndex + 1;
      goldIndex += 1;
      visibleIndex += 1;
      continue;
    }
    const substitution =
      suffixCosts[goldIndex + 1][visibleIndex + 1] + 1;
    const insertion = suffixCosts[goldIndex][visibleIndex + 1] + 1;
    const deletion = suffixCosts[goldIndex + 1][visibleIndex] + 1;
    const best = suffixCosts[goldIndex][visibleIndex];
    const bestRemainingExact =
      suffixExactMatches[goldIndex][visibleIndex];
    if (
      substitution === best &&
      suffixExactMatches[goldIndex + 1][visibleIndex + 1] ===
        bestRemainingExact
    ) {
      alignedVisiblePositions[goldIndex] = visibleIndex;
      goldIndex += 1;
      visibleIndex += 1;
    } else if (
      insertion === best &&
      suffixExactMatches[goldIndex][visibleIndex + 1] ===
        bestRemainingExact
    ) {
      visibleIndex += 1;
    } else if (
      deletion === best &&
      suffixExactMatches[goldIndex + 1][visibleIndex] ===
        bestRemainingExact
    ) {
      goldIndex += 1;
    } else {
      throw new Error("invalid exact-gold alignment state");
    }
  }
  return {
    exactCovered,
    exactVisiblePositions,
    alignedVisiblePositions,
    furthestGoldPosition,
  };
}

export function additivePrefixDuplicatePositions(
  goldText: string,
  visibleText: string,
): number[] {
  const gold = comparableWords(goldText);
  const visible = comparableWords(visibleText);
  const alignment = exactVisibleGoldAlignment(goldText, visibleText);
  const alignedVisible = new Set(
    alignment.alignedVisiblePositions.filter(
      (position) => position >= 0,
    ),
  );
  const duplicates = new Set<number>();

  for (
    let goldIndex = 0;
    goldIndex < alignment.exactVisiblePositions.length;
    goldIndex += 1
  ) {
    const completedPosition =
      alignment.exactVisiblePositions[goldIndex];
    const prefixPosition = completedPosition - 1;
    if (
      completedPosition < 0 ||
      prefixPosition < 0 ||
      alignedVisible.has(prefixPosition)
    ) {
      continue;
    }
    const prefix = visible[prefixPosition];
    const completed = gold[goldIndex];
    if (
      prefix.length < completed.length &&
      completed.startsWith(prefix)
    ) {
      duplicates.add(prefixPosition + 1);
    }
  }

  return [...duplicates].sort((left, right) => left - right);
}

export function wordsBeforeMutableBoundary(
  visibleText: string,
  mutableStartCharCount: number,
): number {
  const prefix = Array.from(visibleText)
    .slice(0, Math.max(0, Math.trunc(mutableStartCharCount)))
    .join("");
  return comparableWords(prefix).length;
}

export function shouldProtectGoldAnchor({
  exactVisiblePosition,
  visibleText,
  mutableStartCharCount,
}: GoldProtectionCandidate): boolean {
  return (
    exactVisiblePosition >= 0 &&
    exactVisiblePosition <
      wordsBeforeMutableBoundary(visibleText, mutableStartCharCount)
  );
}

export function missingExactGoldPositions(
  alignment: ExactGoldAlignment,
  goldPositionLimit: number,
  eligible?: boolean[],
): number[] {
  const positions: number[] = [];
  for (let index = 0; index < goldPositionLimit; index += 1) {
    if (
      (eligible === undefined || eligible[index]) &&
      !alignment.exactCovered[index]
    ) {
      positions.push(index + 1);
    }
  }
  return positions;
}
