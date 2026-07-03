"""Procedural name generation for simtank_rpg.

Layered/weighted gibberish generator. A name is built in stages, each gated by
a probability knob you can tune. Stages:

  1. core        - stitch 1-N gibberish syllables
  2. reduplicate - maybe double the final syllable (chi-po-PO, ti-pa-PA)
  3. realword    - maybe inject a recognizable English chunk (fart, spooky)
  4. affix       - maybe slap on a mock-ethnic prefix/suffix (von-, -onio, -us)

`generate()` is a pure function: call it, get one string. Nothing about files,
IDs, or areas lives here.
"""

import random

# =============================================================================
# BUILDING BLOCKS  --  edit freely. Append your own strings to any list.
# =============================================================================
SIMPLE_ONSETS = list("bcdfgklmnprstvz")
CLUSTER_ONSETS = ["ch", "sh", "br", "gr", "tr", "sp"]   # rarer, start-friendly
VOWELS = ["a", "a", "o", "o", "oo", "i", "e", "ee", "u"]  # front-weighted to a/o/oo
CODAS = ["", "", "", "", "n", "r", "s", "m", "l"]          # usually none

# ugly opener that only ever appears at position 0 (rgargae)
UGLY_ONSETS = ["rg", "sk", "gn", "zr"]

# mock-ethnic affixes
PREFIXES = ["von", "la", "de", "chi", "don", "hog", "lard", "tard", "chin"]
SUFFIXES = ["onio", "us", "imus", "aka", "ini", "oso", "ulon", "po", "pa", "hog", "dark", "tard", "id", "kie", "felt", "chan", "saar", "etto"]

# recognizable chunks dropped in for laughs  <-- most likely list you'll grow
REALWORDS = ["fart", "spooky", "chewi", "poo", "nascar", "chomp",
             "man", "tard", "puff", "goo", "hog", "poot", "nuts", "guff", "bald", "fart", "shart", "pig", "chublar", "pud", "kush", "koosh", "tar", "poot", "pete", "felt"]

# =============================================================================
# KNOBS  --  tune the feel
# =============================================================================
# core syllable count, weighted toward the middle. keys = # syllables,
# values = relative likelihood. 1 and 5 are the rare tails.
SYLL_WEIGHTS = {1: 4, 2: 34, 3: 33, 4: 15, 5: 5}

P_CLUSTER = 0.20         # syllable uses a cluster onset instead of a simple one
P_UGLY_START = 0.08      # name opens with an ugly cluster
P_CODA = 0.25            # a given syllable gets a coda
P_REDUP = 0.20           # double the final syllable (chipopo, tipapa)

P_EMBELLISH = 0.55       # name gets ANY flourish (realword / affix)
P_EXTRA_FLOURISH = 0.10  # given one flourish, chance of stacking another
                         #   (this is what yields the rare very-long names)
# when a flourish fires, split between kinds:
W_REALWORD = 0.45
W_SUFFIX = 0.40
W_PREFIX = 0.15


def _syllable(rng):
    if rng.random() < P_CLUSTER:
        onset = rng.choice(CLUSTER_ONSETS)
    else:
        onset = rng.choice(SIMPLE_ONSETS)
    vowel = rng.choice(VOWELS)
    coda = rng.choice(CODAS) if rng.random() < P_CODA else ""
    return onset + vowel + coda


def _core(rng):
    counts = list(SYLL_WEIGHTS.keys())
    weights = list(SYLL_WEIGHTS.values())
    n = rng.choices(counts, weights=weights)[0]
    sylls = [_syllable(rng) for _ in range(n)]
    if rng.random() < P_UGLY_START:
        sylls[0] = rng.choice(UGLY_ONSETS) + rng.choice(VOWELS)
    return sylls


def _apply_flourish(name, rng):
    kind = rng.choices(
        ["realword", "suffix", "prefix"],
        weights=[W_REALWORD, W_SUFFIX, W_PREFIX],
    )[0]
    if kind == "realword":
        word = rng.choice(REALWORDS)
        return word + name if rng.random() < 0.5 else name + word
    if kind == "suffix":
        return name + rng.choice(SUFFIXES)
    return rng.choice(PREFIXES) + name


def generate(rng=None):
    rng = rng or random
    sylls = _core(rng)

    if rng.random() < P_REDUP:
        sylls.append(sylls[-1])

    name = "".join(sylls)

    if rng.random() < P_EMBELLISH:
        name = _apply_flourish(name, rng)
        # rarely stack more flourishes -> the occasional monster name
        while rng.random() < P_EXTRA_FLOURISH:
            name = _apply_flourish(name, rng)

    return name


if __name__ == "__main__":
    import sys
    import json

    args = sys.argv[1:]

    # `python names.py json N [outfile]` -> write numbered JSON
    if args and args[0] == "json":
        n = int(args[1]) if len(args) > 1 else 100
        outfile = args[2] if len(args) > 2 else "names.json"
        data = {str(i): generate() for i in range(n)}
        with open(outfile, "w") as f:
            json.dump(data, f, indent=2)
        print(f"wrote {n} names to {outfile}")
        sys.exit(0)

    # default: print a batch + a length histogram for tuning
    n = int(args[0]) if args else 50
    names = [generate() for _ in range(n)]
    for nm in names:
        print(nm)

    print("\n--- length histogram ---")
    from collections import Counter
    hist = Counter(len(nm) for nm in names)
    for length in sorted(hist):
        print(f"{length:2d} chars | {'#' * hist[length]} ({hist[length]})")