"""Project constants."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceProfile:
    name: str
    repo_url: str
    pinned_commit: str
    layout: str
    sparse_paths: tuple[str, ...]
    textmap_root: str
    textmap_record_kind: str
    version_file: str | None = None
    expected_game_version: str | None = None
    expected_resource_version: str | None = None
    expected_changelist: str | None = None
    representative_exact_hits: tuple[tuple[str, str], ...] = ()


SOURCE_PROFILES = {
    "arikatsu": SourceProfile(
        name="arikatsu",
        repo_url="https://github.com/Arikatsu/WutheringWaves_Data.git",
        pinned_commit="9218d612ad815e398e064e577e42aaf878899968",
        layout="arikatsu_textmaps",
        sparse_paths=("README.md", "Textmaps", "BinData"),
        textmap_root="Textmaps",
        textmap_record_kind="id_content_arrays",
        version_file="README.md",
        expected_game_version="3.7.0",
        expected_resource_version="3.7.8",
        expected_changelist="8975829",
        # Re-measured on the built 3.7 candidate, not copied forward from the
        # 3.6 note. 景燃 -> Jingran is still the only en for 景燃 and the only
        # zh for "Jingran" (one resonator row and one speaker row). The retired
        # 3.5 pair 穗穗 -> Suisui is still unusable: 穗穗（通讯中） -> Suisui
        # keeps the reverse direction multi-valued. A new single-valued pair
        # measured on this candidate, 棠宁 -> Tangning, is recorded in the
        # data-refresh note; this check stays the pair that remained
        # single-valued across the 3.6 to 3.7 build.
        representative_exact_hits=(("景燃", "Jingran"),),
    ),
    "dimbreath_legacy": SourceProfile(
        name="dimbreath_legacy",
        repo_url="https://github.com/Dimbreath/WutheringData.git",
        pinned_commit="e9234ffe094b2d944d16b222d31102e8ab32d954",
        layout="dimbreath_configdb",
        sparse_paths=("ConfigDB", "TextMap/zh-Hans", "TextMap/en"),
        textmap_root="TextMap",
        textmap_record_kind="multitext_object",
    ),
}

DEFAULT_SOURCE_PROFILE_NAME = "arikatsu"
SOURCE_PROFILE_ENV = "WUWATERM_SOURCE_PROFILE"


def get_source_profile(name: str | None = None) -> SourceProfile:
    profile_name = name or os.getenv(SOURCE_PROFILE_ENV, DEFAULT_SOURCE_PROFILE_NAME)
    try:
        return SOURCE_PROFILES[profile_name]
    except KeyError as exc:
        known = ", ".join(sorted(SOURCE_PROFILES))
        raise ValueError(f"unknown source profile {profile_name!r}; known profiles: {known}") from exc


def source_profile_choices() -> tuple[str, ...]:
    return tuple(sorted(SOURCE_PROFILES))


ACTIVE_SOURCE_PROFILE = get_source_profile()
PINNED_WUTHERINGDATA_COMMIT = ACTIVE_SOURCE_PROFILE.pinned_commit
WUTHERINGDATA_REPO = ACTIVE_SOURCE_PROFILE.repo_url

CATEGORY_ORDER = {
    "core_term": 0,
    "resonator": 10,
    "weapon": 20,
    "echo": 30,
    "skill": 40,
    "sonata_effect": 50,
    "location": 60,
    "item": 70,
    "speaker": 80,
}

CORE_TERM_KEYS = {
    "LoadingTipsText_1005_Title": "core_term",  # 共鸣者 -> Resonator
    "Term850082_Title": "core_term",  # 声骸 -> Echo
    "OccupationConfig_漂泊者_Name": "core_term",  # 漂泊者 -> Rover
}

# Free-text lock exclusions. The builder counts how often each English
# dictionary surface is locked in official English text and how often the
# parallel official Chinese text carries its official Chinese name. Surfaces
# locked at least FREE_TEXT_LOCK_MIN_OCCURRENCES times with an aligned share
# below FREE_TEXT_LOCK_MAX_ALIGNED_RATIO (numerator, denominator) are stored
# under the metadata key and do not lock in free-text sentence translation.
FREE_TEXT_LOCK_EXCLUSIONS_KEY = "free_text_lock_exclusions"
FREE_TEXT_LOCK_EXCLUSIONS_VERSION = 1
FREE_TEXT_LOCK_MIN_OCCURRENCES = 10
FREE_TEXT_LOCK_MAX_ALIGNED_RATIO = (1, 5)

# Chinese-surface free-text lock exclusions, measured in the zh direction: the
# builder counts how often each Chinese dictionary surface locks in official
# Chinese lines that have a parallel English line, and counts a line aligned
# when the parallel English (casefolded, quote-stripped) carries the surface's
# official English or the English of any record carrying that surface. On the
# 3.7 census (277,917 aligned line pairs) the per-surface aligned share is
# strongly bimodal with a density valley around 0.2 — 74 surfaces sit in
# [0.05,0.15), only 26 in [0.15,0.25), then the density rises again to 676
# surfaces at >=0.95 — so 1/5 is the cut that misclassifies the fewest
# boundary surfaces. N>=10 is where the distribution actually separates: the
# N in [5,9] band shows no valley (one aligned line moves a ratio by 10-20
# points there), and cutting at N>=5 would add 30 surfaces on that noise. The
# values coincide with the English side by convergence from an independent
# distribution, not by copying; they are defined separately so either side
# can move without renegotiating the other.
FREE_TEXT_LOCK_ZH_EXCLUSIONS_KEY = "free_text_lock_exclusions_zh"
FREE_TEXT_LOCK_ZH_EXCLUSIONS_VERSION = 1
FREE_TEXT_LOCK_ZH_MIN_OCCURRENCES = 10
FREE_TEXT_LOCK_ZH_MAX_ALIGNED_RATIO = (1, 5)
