"""Project constants."""

from __future__ import annotations

import os
import re
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
    "Personalize_Card": "core_term",  # 羁旅印章 -> Sigil
    "WeRougeBuffTriggerNameDivineDamage": "core_term",  # 徽记 -> Sigil
    "FunctionMenu_10130_FunctionName": "core_term",  # 飞讯 -> WavesLine
    "ItemType_ChatTheme": "core_term",  # 飞讯主题 -> WavesLine Theme
    "ItemType_ChatCharm": "core_term",  # 飞讯摆件 -> WavesLine Decor
    "ItemType_ChatBg": "core_term",  # 聊天背景 -> Chat Background
    "ItemType_ChatBubble": "core_term",  # 聊天气泡 -> Chat Bubble
    "HelpText_694_Title": "core_term",  # 历程回顾模式 -> Replay Mode
    "VisionFetterTitle": "core_term",  # 合鸣效果 -> Sonata Effect
    "VisionSkinTitleText": "core_term",  # 声骸异相 -> Phantom Appearance
    "Advertising_VersionTitle_37": "core_term",  # 镜锁妄世，心照红尘 -> Prism's Illusion, Heart's Illumination
    # A pinned map mark rather than a core system name. The value is still the
    # term category, so it enters lookup and locking as a location.
    "MapMark_170100_MarkTitle": "location",  # 扶风转标 -> Gravity Calibrator
    # World, region, faction and field names that the Term/Influence tables
    # carry with an exact official pair but the BinData specs do not register.
    "Term850078_Title": "core_term",  # 无音区 -> Tacet Field
    "Term850079_Title": "core_term",  # 索拉里斯 -> Solaris
    "Term850093_Title": "location",  # 瑝珑 -> Huanglong
    "Influence_998_Title": "core_term",  # 残星会 -> Fractsidus
    # Announcement cosmetics whose BinData table also carries unrelated
    # ordinary English words, so the individual verified row is pinned here
    # instead of importing the table.
    "BackgroundCard_80065030_Title": "item",  # 舞台时刻！ -> Showtime!
    "BackgroundCard_80065016_Title": "item",  # 匿于宙海的星光 -> Concealed Starlight
    "AchievementGroup_1013_Title": "item",  # 万相归心境 -> All Forms in One Heart
    "PlayerHead_82000019_Name": "item",  # 羊咩祝福 -> Wooly's Blessing
    "PlayerHead_82000018_Name": "item",  # 推布布 -> Abby Tossing
    "MotorSticker_89103070_Name": "item",  # 落日狐影 -> Fox Shadow at Sunset
    "MotorDecorations_89300004_Name": "item",  # 心团子 -> Hsin Cube
    "OrnamentName_80871006": "item",  # 布是偶 -> Not Even Close
    "OrnamentName_80870016": "item",  # 何处梦清辉 -> Where Dreams Shine Clear
    "FlySkinConfig_84100004_Name": "item",  # 缀星纺宙之缍 -> Cosmic Spindle
}

OFFICIAL_ANNOUNCEMENT_SOURCE_FILE = "official_announcement"

# Derived name rows. Three builder mechanisms add names no BinData row pairs:
# the Arikatsu Area_<n>_Title keys no area/area.json row references, imported
# from the TextMap as locations; the Advertising_VersionTitle_<n> family from
# the same TextMap, which names a release rather than a glossary meaning; and
# the base pair of a complete four-tier item family, whose source_id is
# ITEM_TIER_BASE_SOURCE_PREFIX plus the Basic-tier row id. Runtime reads a
# row's provenance from these markers (term_classification.is_derived_name_entry).
ARIKATSU_MULTITEXT_SOURCE_FILE = "Textmaps/multi_text/MultiText.json"
AREA_TITLE_KEY_RE = re.compile(r"Area_\d+_Title")
ADVERTISING_VERSION_TITLE_RE = re.compile(r"Advertising_VersionTitle_\d+")
ITEM_TIER_BASE_SOURCE_PREFIX = "tier-base:"

# Fixed official bilingual announcement terms with no standalone MultiText key:
# the CMS announcement label that the 3.7 maintenance notice renders as
# 飞讯外观 / WavesLine Looks (absent from client MultiText, 0 zh / 0 en), the
# 3.7 companion-task pair carried inside HelpText_487_Content and
# AdvertisingIntelligence_3010503_ContentText (伴行任务 / Starpath Tasks), and
# the 3.7 reward currency HelpText_516_Title renders as 月影溯环获取方式 /
# How to Obtain Moonlit Rings. Only pairs with no ordinary homograph are listed:
# (zh, en, category, source_id).
#
# 连携技能 / Concerto Skill and 冰痕 / Frostbite are deliberately absent:
# 连携技能 is absent from 3.7 and every bilingual 3.7 skill description carrying
# "Concerto Skill" renders it as 协奏技能, so the pairing would hard-lock a word
# the game no longer uses, and Frostbite also occurs in 39 official English
# strings as an ordinary noun and as a monster prefix ("Frostbite ointment" ->
# 冻伤膏, "Frostbite Coleoid" -> 霜鳞蜃甲), so the pair would override those
# readings in the en->zh direction.
OFFICIAL_ANNOUNCEMENT_TERMS: tuple[tuple[str, str, str, str], ...] = (
    ("飞讯外观", "WavesLine Looks", "core_term", "wavesline_looks"),
    ("伴行任务", "Starpath Task", "core_term", "starpath_task"),
    ("月影溯环", "Moonlit Ring", "item", "moonlit_ring"),
)

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
