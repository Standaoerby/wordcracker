"""Motion verbs lexicon — E2 (R-22 P2) — semantic-class filter for
«глаголы движения» queries.

Closed-list lexicon of English motion verbs derived from VerbNet/FrameNet
motion-class (run, walk, ride, sail, hasten, depart, arrive, ...).
Includes archaic forms (hath, hither) common in 19th-c. fiction.

Inflected forms included where they don't conflict with non-motion meanings:
  - went, gone, going (from «go»)
  - came, coming (from «come»)
  Not included: «set» (off / out / down — too polysemous)

When the v1 `top_ngrams_by_author(pos_filter=['VERB'])` returns the top-N
verbs by affinity, we intersect with this lexicon to surface only motion
verbs. This is closed-list — by design, NOT comprehensive — it prefers
precision over recall.

Extensible: add new entries when probe finds missing verbs. Don't add
verbs that are heavily polysemous (set / take / have / give / make).
"""
from __future__ import annotations

# Frozen set for O(1) membership check.
# Includes inflected forms because v1 returns raw tokens, not lemmas.
MOTION_VERBS: frozenset[str] = frozenset({
    # Walking / on foot
    "walk", "walks", "walked", "walking",
    "step", "steps", "stepped", "stepping",
    "stride", "strides", "strode", "striding",
    "tread", "treads", "trod", "treading",
    "march", "marches", "marched", "marching",
    "pace", "paces", "paced", "pacing",
    "stroll", "strolls", "strolled", "strolling",
    "wander", "wanders", "wandered", "wandering",
    "roam", "roams", "roamed", "roaming",
    "saunter", "saunters", "sauntered", "sauntering",
    "amble", "ambles", "ambled", "ambling",
    "trudge", "trudges", "trudged", "trudging",
    "limp", "limps", "limped", "limping",
    "stagger", "staggers", "staggered", "staggering",
    "stumble", "stumbles", "stumbled", "stumbling",

    # Running / fast
    "run", "runs", "ran", "running",
    "sprint", "sprints", "sprinted", "sprinting",
    "dash", "dashes", "dashed", "dashing",
    "rush", "rushes", "rushed", "rushing",
    "hurry", "hurries", "hurried", "hurrying",
    "hasten", "hastens", "hastened", "hastening",
    "race", "races", "raced", "racing",
    "bolt", "bolts", "bolted", "bolting",
    "flee", "flees", "fled", "fleeing",
    "scurry", "scurries", "scurried", "scurrying",
    "scamper", "scampers", "scampered", "scampering",
    "scuttle", "scuttles", "scuttled", "scuttling",

    # Riding / driving
    "ride", "rides", "rode", "riding",
    "drive", "drives", "drove", "driving", "driven",
    "gallop", "gallops", "galloped", "galloping",
    "trot", "trots", "trotted", "trotting",
    "canter", "canters", "cantered", "cantering",

    # Sailing / aquatic
    "sail", "sails", "sailed", "sailing",
    "row", "rows", "rowed", "rowing",
    "swim", "swims", "swam", "swimming",
    "wade", "wades", "waded", "wading",
    "float", "floats", "floated", "floating",
    "drift", "drifts", "drifted", "drifting",
    "paddle", "paddles", "paddled", "paddling",

    # Flying / aerial
    "fly", "flies", "flew", "flying", "flown",
    "soar", "soars", "soared", "soaring",
    "glide", "glides", "glided", "gliding",
    "swoop", "swoops", "swooped", "swooping",
    "hover", "hovers", "hovered", "hovering",

    # Climbing / vertical
    "climb", "climbs", "climbed", "climbing",
    "ascend", "ascends", "ascended", "ascending",
    "descend", "descends", "descended", "descending",
    "mount", "mounts", "mounted", "mounting",
    "scale", "scales", "scaled", "scaling",

    # Jumping / leaping
    "jump", "jumps", "jumped", "jumping",
    "leap", "leaps", "leapt", "leaped", "leaping",
    "spring", "springs", "sprang", "sprung", "springing",
    "bound", "bounds", "bounded", "bounding",
    "vault", "vaults", "vaulted", "vaulting",
    "skip", "skips", "skipped", "skipping",
    "hop", "hops", "hopped", "hopping",

    # Falling / dropping
    "fall", "falls", "fell", "fallen", "falling",
    "drop", "drops", "dropped", "dropping",
    "plunge", "plunges", "plunged", "plunging",
    "tumble", "tumbles", "tumbled", "tumbling",

    # Coming / arriving / departing
    "come", "comes", "came", "coming",
    "go", "goes", "went", "gone", "going",
    "arrive", "arrives", "arrived", "arriving",
    "depart", "departs", "departed", "departing",
    "leave", "leaves", "left", "leaving",
    "enter", "enters", "entered", "entering",
    "exit", "exits", "exited", "exiting",
    "return", "returns", "returned", "returning",
    "approach", "approaches", "approached", "approaching",
    "retreat", "retreats", "retreated", "retreating",
    "withdraw", "withdraws", "withdrew", "withdrawing",
    "advance", "advances", "advanced", "advancing",

    # Pursuit / chasing
    "chase", "chases", "chased", "chasing",
    "pursue", "pursues", "pursued", "pursuing",
    "follow", "follows", "followed", "following",
    "track", "tracks", "tracked", "tracking",

    # Crawling / slow / stealth
    "crawl", "crawls", "crawled", "crawling",
    "creep", "creeps", "crept", "creeping",
    "slither", "slithers", "slithered", "slithering",
    "sneak", "sneaks", "sneaked", "snuck", "sneaking",
    "slip", "slips", "slipped", "slipping",
    "slink", "slinks", "slunk", "slinking",

    # Travel / journey (general)
    "travel", "travels", "travelled", "traveled", "travelling", "traveling",
    "journey", "journeys", "journeyed", "journeying",
    "wander", "wanders", "wandered", "wandering",
    "venture", "ventures", "ventured", "venturing",
    "roam", "roams", "roamed", "roaming",

    # Spinning / turning (motion of body)
    "turn", "turns", "turned", "turning",
    "spin", "spins", "spun", "spinning",
    "swing", "swings", "swung", "swinging",
    "lean", "leans", "leaned", "leant", "leaning",
})


def filter_motion_verbs(rows: list[dict], *, word_key: str = "ngram") -> list[dict]:
    """Filter a list of ngram-rows to only motion verbs.

    Args:
      rows      — list of dicts as returned by top_ngrams_by_author. Each
                  row has at minimum a token field (default «ngram»).
      word_key  — name of the token field in each row. v1 uses «ngram» for
                  unigrams, also accepts «word»/«lemma»/«token» as fallback.

    Returns a new list (does not mutate input). Preserves order.
    """
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        # Try multiple possible field names for the token
        tok = (r.get(word_key)
               or r.get("word")
               or r.get("lemma")
               or r.get("token") or "")
        if not isinstance(tok, str):
            continue
        if tok.lower() in MOTION_VERBS:
            out.append(r)
    return out


def count_motion_verbs_in_author(author_regex: str,
                                  year_from: int | None = None,
                                  year_to: int | None = None,
                                  country: str | None = None,
                                  top: int = 30) -> list[dict]:
    """Direct corpus-scan fallback for `semantic_class=motion`.

    Why this exists (Phase 3 W-9 — Stan 2026-05-22):
        v1 `top_ngrams_by_author` ranks by author-affinity. For prolific
        authors like Dickens, the top-N by affinity are dialogue tags
        («said», «replied», «cried», «inquired») — those are author-
        characteristic but NOT motion. Intersecting with MOTION_VERBS
        gives an empty list. Stan saw «глаголы движения у Диккенса» →
        «не нашлось униграммы при текущих фильтрах».

    What this does:
        Scan the author's per-book counts files, sum counts for tokens
        in MOTION_VERBS, return top-N by raw count. We don't rank by
        affinity — the LEXICON already constrains semantics; the
        ranking just orders by usage.

    Returns:
        List of {"ngram": str, "count": int} sorted by count desc.
        Empty list if no books match or the corpus paths aren't
        available (dev workstations without /workspace/spgc/).
    """
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _REPO = _Path(__file__).resolve().parents[3]
        if str(_REPO) not in _sys.path:
            _sys.path.insert(0, str(_REPO))
        from scripts.rag_tools import _select_books, _counts_path
    except (ImportError, AttributeError):
        return []

    try:
        sel = _select_books(author_regex, year_from=year_from,
                            year_to=year_to, country=country)
    except Exception:
        return []
    if sel is None or not len(sel):
        return []

    counts: dict[str, int] = {v: 0 for v in MOTION_VERBS}
    used = 0
    for pg in sel["id"]:
        f = _counts_path(pg)
        if not f.exists():
            continue
        used += 1
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 2:
                        continue
                    tok = parts[0].lower()
                    if tok in counts:
                        try:
                            counts[tok] += int(parts[1])
                        except ValueError:
                            continue
        except OSError:
            continue
    if not used:
        return []
    ranked = sorted(((tok, c) for tok, c in counts.items() if c > 0),
                    key=lambda x: x[1], reverse=True)
    return [{"ngram": tok, "count": c} for tok, c in ranked[:top]]
