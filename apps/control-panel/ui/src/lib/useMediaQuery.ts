import { useEffect, useState } from "react";

/** Below this the console stacks: rail above, stage below, no drag divider —
 *  the same 820px line the hand-written page drew. */
export const NARROW_QUERY = "(max-width: 820px)";

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(
    () => typeof matchMedia !== "undefined" && matchMedia(query).matches,
  );
  useEffect(() => {
    const list = matchMedia(query);
    const update = () => setMatches(list.matches);
    update();
    list.addEventListener("change", update);
    return () => list.removeEventListener("change", update);
  }, [query]);
  return matches;
}
