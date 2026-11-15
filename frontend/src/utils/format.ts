const ACRONYMS = new Set(["api", "cpu", "db"]);

/**
 * Turn an enum-style API value such as ``cpu_resource_saturation`` into a
 * sentence-case label. Known acronyms stay upper case.
 */
export function labelize(value: string): string {
  const words = value.split("_").map((word, index) => {
    if (ACRONYMS.has(word)) {
      return word.toUpperCase();
    }
    return index === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word;
  });
  return words.join(" ");
}

/** Describe how many items are shown out of a total, e.g. "Showing 3 of 12 incidents". */
export function showingLabel(shown: number, total: number, noun: string): string {
  if (shown === total) {
    return `${total} ${noun}${total === 1 ? "" : "s"}`;
  }
  return `Showing ${shown} of ${total} ${noun}s`;
}
