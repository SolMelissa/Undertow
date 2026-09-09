"""
Self-test fixtures and regression-checking for tag_cleanup_x.
"""

from tag_cleanup_x_engine import Config, PerformerGazetteer, parse_filename_tag_batch
from tag_cleanup_x_render import Renderer, print_preview_table, FilePreview


FIXTURES = [
    "dir:12-sunset hike - teen couple watches full tide rise over lake after long trail up xz",
    "dir:03-bird watching - adult woman records a rare woodpecker through her binoculars on quiet morning qp",
    "dir:08-kitchen prep - young chef parts her fresh herbs after trimming up fresh basil for supper bk",
    "dir:26-slow river - small boat drifts past tall reeds and old wooden pier under warm noon sun fw",
    "dir:45-autumn walk - tall man picks up red maple leaves after strong wind blows them off branch ql",
    "dir:17-coffee break - slim barista pours hot milk into large ceramic cup on rustic wooden counter vp",
    "dir:31-library quiet - short student reads thick novel while soft rain taps glass window during study mn",
    "dir:09-garden work - kind grandma pulls up long weeds after steady rain soaks green flower bed tr",
    "dir:22-bike ride - fit courier delivers fresh bread across old stone tower up steep city hill kx",
    "dir:38-mountain view - young hiker spots a gray wolf near high ridge trail as cool fog rolls in yd",
    # -- Phase 2 regression fixtures: name detection, reserved-split, age pattern,
    # compound-noun, attribute stacking, dictionary-based truncation drop. --
    "dir:41-quiet moment - young anwen corvina watches distant boulder near an old gate",
    "dir:44-early stroll - bright grace hall strolls along old canal past quiet chapel",
    "dir:47-evening light - warm grace hall walks past a quiet meadow near soft hills",
    "dir:49-sunset scene - soft grace hall lingers by a quiet dock under warm sky",
    "dir:52-long walk - fit teen sprints across an old fence after steep climb",
    "dir:55-desert trip - 18 year old redhead poses near old barn under bright sun",
    "dir:58-quiet dawn - 25 years old traveler rests by a quiet lake near tall pines",
    "dir:61-cliff day - eager first timer climbs steep cliff above rocky shore",
    "dir:64-quarry walk - massive black boulder blocks a narrow forest path",
    "dir:67-cabin trip - small dark red cabin sits near an old pine forest",
    "dir:71-old photo - faded picture shows a dusty attic full of black sto",
    "dir:73-travel log - tired hiker rests inside a warm mountain swe",
    "dir:76-study break - reader browses a tall shelf inside the librar",
    "dir:79-project notes - focused engineer reviews a long report marked deepl",
    "dir:82-camera roll - wide landscape shows a bright field at 1920x1080 b",
    # -- Skip-filter fixtures: a single-word tag and a short multi-word tag
    # (under Config.min_process_tag_length) must never be parsed at all - both
    # come back as one unchanged tag, with an "exploded" trace of just one
    # "skipped" element, and never reach tokenization/corpus-stats. --
    "dir:mountain",
    "dir:5-old barn",
]


def run_self_test(cfg: Config, renderer: Renderer) -> None:
    previews = [FilePreview(label=f"fixture {i}", entries=[parsed])
                for i, parsed in enumerate(parse_filename_tag_batch(FIXTURES, cfg), start=1)]
    print_preview_table(previews, renderer)

    expected_fixture_1 = ["sunset", "hike", "teen", "couple", "full tide", "lake", "long trail"]
    actual_fixture_1 = previews[0].entries[0].tags
    if actual_fixture_1 == expected_fixture_1:
        renderer.out("Fixture 1 matches expected output. [OK]")
    else:
        renderer.out(f"Fixture 1 MISMATCH.")
        renderer.out(f"  expected: {expected_fixture_1}")
        renderer.out(f"  actual:   {actual_fixture_1}")

    all_tags = [t for fp in previews for e in fp.entries for t in e.tags]
    all_dropped = [t for fp in previews for e in fp.entries for t in e.dropped]

    checks = [
        ("No character: namespace is emitted anywhere",
         not any(t.startswith("character:") for t in all_tags)),
        ("'teen' always emits standalone, never merged",
         "teen" in all_tags and not any(t != "teen" and "teen" in t.split(" ") for t in all_tags)),
        ("'18 year old' groups as one token", "18 year old" in all_tags),
        ("'25 years old' groups as one token", "25 years old" in all_tags),
        ("'first timer' survives as a compound", "first timer" in all_tags),
        ("'massive black boulder' stacks into one tag", "massive black boulder" in all_tags),
        ("'small dark red cabin' stacks into one tag", "small dark red cabin" in all_tags),
        ("Truncated fragments dropped, not kept as tags",
         not any(t in ("sto", "swe", "librar", "deepl") for t in all_tags)
         and all(frag in all_dropped for frag in ("sto", "swe", "librar", "deepl"))),
        ("Single-char remnant 'b' dropped", "b" not in all_tags and "b" in all_dropped),
        ("Single-word tag is skipped entirely, kept unchanged",
         previews[-2].entries[0].skipped and previews[-2].entries[0].tags == ["mountain"]),
        ("Short (<35 char) multi-word tag is skipped entirely, kept unchanged",
         previews[-1].entries[0].skipped and previews[-1].entries[0].tags == ["old barn"]),
    ]
    renderer.out("\nRegression checks:")
    for label, passed in checks:
        renderer.out(f"  [{'OK' if passed else 'FAIL'}] {label}")
    if not all(passed for _, passed in checks):
        renderer.out("\nOne or more regression checks FAILED - see FIXTURES/expected output above.")

    # Offline performer-gazetteer checks: a synthetic gazetteer standing in for a real
    # ThePornDB/StashDB fetch, so this exercises the name-detection path without a network
    # call. "faith" and "cruz" are deliberately also plausible ordinary words/surnames, and
    # "grace"+"cruz" is deliberately NOT a real pairing even though both halves are
    # individually known, to verify the co-occurrence (not cross-product) requirement.
    name_pairs = {("stacy", "cruz"), ("grace", "hall"), ("faith", "hall")}
    # "xyzelle" stands in for a real single-word stage name/mononym; "petite" is deliberately
    # also stuffed into single_names (as noisy scraped data would) to verify attribute-lexicon
    # protection still wins even on the lower-precision single-name path.
    name_cfg = Config(performer_gazetteer=PerformerGazetteer(
        full_name_phrases={"stacy cruz", "grace hall", "faith hall"},
        name_pairs=name_pairs,
        single_names={"xyzelle", "petite"},
        initials_by_last={"hall": {"g", "f"}, "cruz": {"s"}},
        max_phrase_len=2,
    ))
    name_fixtures = [
        "dir:38-angelic teen stacy cruz gets ass fucked by big cock outdoors",
        "dir:12-quiet evening faith hall relaxes by the old lake shore",
        "dir:19-color study the ocean looked deep blue under fading light",
        "dir:45-portrait session g hall poses by a quiet window in soft light",
        "dir:61-market day grace cruz browses old stalls near the quiet square",
        "dir:73-garden shoot - petite xyzelle poses barefoot by the old fence",
    ]
    name_results = parse_filename_tag_batch(name_fixtures, name_cfg)
    name_checks = [
        ("Full-name gazetteer phrase 'stacy cruz' survives as one protected tag, "
         "not dropped/merged", "stacy cruz" in name_results[0].tags),
        ("Alias 'faith hall' (adjacency pair, order as-is) recognized as a name",
         "faith hall" in name_results[1].tags),
        ("Lone ambiguous word 'blue' (no adjacent gazetteer match) is NOT force-classified "
         "as a name - falls through to ordinary attribute handling",
         "blue" not in name_results[2].dropped),
        ("Initial form 'g hall' (first-name initial + known last name) recognized as a name",
         "g hall" in name_results[3].tags),
        ("'grace cruz' is NOT recognized as a name - 'grace' and 'cruz' are each individually "
         "known but never co-occurred, so the cross-product match is correctly rejected",
         "grace cruz" not in name_results[4].tags
         and "grace" in name_results[4].tags and "cruz" in name_results[4].tags),
        ("Single-word stage name 'xyzelle' stays its own tag, not swallowed into the "
         "preceding attribute ('petite xyzelle')", "xyzelle" in name_results[5].tags
         and "petite xyzelle" not in name_results[5].tags),
        ("'petite' is NOT treated as a single-word name even though it's (deliberately, for "
         "this test) stuffed into single_names - attribute-lexicon protection still wins",
         "petite" in name_results[5].tags),
    ]
    renderer.out("\nPerformer-gazetteer regression checks (offline, synthetic gazetteer):")
    for label, passed in name_checks:
        renderer.out(f"  [{'OK' if passed else 'FAIL'}] {label}")
    if not all(passed for _, passed in name_checks):
        renderer.out("\nOne or more performer-gazetteer regression checks FAILED.")
