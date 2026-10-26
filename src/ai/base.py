from abc import ABC, abstractmethod
from collections import defaultdict
import json
from typing import List, Optional, Dict, Any, Tuple
from pydantic import BaseModel, Field


class AIProviderError(Exception):
    """Raised when an AI provider fails due to API keys, rate limits, network, or server errors."""
    pass


class DailyQuotaExhaustedError(AIProviderError):
    """Raised when an AI provider has exhausted its daily token or request quota."""
    pass


STAGE1_VALID_ACTIONS = {"extract", "no_extraction", "reject_delete"}
STAGE2_VALID_DECISIONS = {"import", "reject_keep_in_client", "reject_delete", "dismiss_pack"}


def format_episode_ranges(numbers: List[int]) -> str:
    """Format a list of episode numbers into compact ranges (e.g. [1, 2, 3, 5] -> 'E01-E03, E05')."""
    valid_nums = sorted({int(n) for n in numbers if isinstance(n, (int, str)) and str(n).isdigit()})
    if not valid_nums:
        return "none"
    ranges = []
    start = valid_nums[0]
    prev = valid_nums[0]
    for n in valid_nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            ranges.append(f"E{start:02d}" if start == prev else f"E{start:02d}-E{prev:02d}")
            start = n
            prev = n
    ranges.append(f"E{start:02d}" if start == prev else f"E{start:02d}-E{prev:02d}")
    return ", ".join(ranges)


def compact_ollama_stage2_context(
    candidate_media: Optional[Dict[str, Any]],
    library_candidates: Optional[List[Dict[str, Any]]],
    manual_import_files: List[Dict[str, Any]],
    existing_disk_files: List[Dict[str, Any]],
    known_episodes: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[List[Dict[str, Any]]], List[Dict[str, Any]], Any, Any]:
    """
    Ultra-lean context compactor specifically tailored for local Ollama models:
    1. Prunes library_candidates completely if candidate_media is already known by *arr (saving 500-1000 tokens!).
       If candidate_media is null/missing, retains only the top 5 library candidates.
    2. Caps candidate_files at 10 items (instead of 25).
    3. Transforms existing on-disk files into compact contiguous range strings (e.g. 'E01-E06 (1080p, CF:75)').
    4. Transforms known_episodes into compact missing episode range summaries.
    """
    # 1. Prune library candidates
    if candidate_media and candidate_media.get("id"):
        pruned_library = None
    else:
        pruned_library = (library_candidates or [])[:5]

    # 2. Cap candidate files at 10
    compacted_candidates = manual_import_files[:10]

    # 3. Compact existing disk files
    if not existing_disk_files:
        compacted_disk: Any = []
    elif len(existing_disk_files) <= 4:
        compacted_disk = existing_disk_files
    else:
        grouped: Dict[Tuple[Any, Any, Any], List[int]] = defaultdict(list)
        for ef in existing_disk_files:
            s_num = ef.get("season")
            ep_num = ef.get("episode")
            qual = ef.get("quality")
            cf = ef.get("customFormatScore")
            if ep_num is not None:
                grouped[(s_num, qual, cf)].append(int(ep_num))
        compacted_disk = [
            {
                "season": s,
                "on_disk": f"{format_episode_ranges(eps)} ({q or 'Unknown'}, CF:{cf if cf is not None else 0})",
                "count": len(eps),
            }
            for (s, q, cf), eps in sorted(grouped.items(), key=lambda x: (x[0][0] is None, x[0][0]))
        ]

    # 4. Compact known episodes
    if not known_episodes:
        compacted_known: Any = None
    else:
        by_season: Dict[Any, Dict[str, Any]] = defaultdict(
            lambda: {"total": 0, "missing": [], "on_disk": []}
        )
        for ep in known_episodes:
            s_num = ep.get("season")
            ep_num = ep.get("episode")
            has_file = bool(ep.get("hasFile", False))
            s_entry = by_season[s_num]
            s_entry["total"] += 1
            if ep_num is not None:
                if has_file:
                    s_entry["on_disk"].append(int(ep_num))
                else:
                    s_entry["missing"].append(int(ep_num))

        compacted_known = [
            {
                "season": s,
                "total_episodes": info["total"],
                "missing": format_episode_ranges(info["missing"]),
            }
            for s, info in sorted(by_season.items(), key=lambda x: (x[0] is None, x[0]))
        ]

    return pruned_library, compacted_candidates, compacted_disk, compacted_known


def compact_stage2_context(
    manual_import_files: List[Dict[str, Any]],
    existing_disk_files: List[Dict[str, Any]],
    known_episodes: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[Dict[str, Any]], Any, Any]:
    """
    Compact large episode and disk-file lists for Stage 2 AI prompts so multi-episode
    seasons (e.g. 40-100 episode series) do not overwhelm model context windows.
    """
    referenced_ep_keys = set()
    for m in manual_import_files:
        for ep in m.get("episodes") or []:
            s_num = ep.get("season")
            e_num = ep.get("episode")
            if s_num is not None and e_num is not None:
                referenced_ep_keys.add((s_num, e_num))

    # 1. Compact Candidate Files (cap at 25 files if a huge unmapped pack is sent)
    compacted_candidates = manual_import_files[:25]

    # 2. Compact Existing Imported Files on Disk
    if len(existing_disk_files) <= 12:
        compacted_existing: Any = existing_disk_files
    else:
        direct_matches = []
        grouped: Dict[Tuple[Any, Any, Any], List[Any]] = defaultdict(list)
        for ef in existing_disk_files:
            key = (ef.get("season"), ef.get("episode"))
            if key in referenced_ep_keys and len(direct_matches) < 15:
                direct_matches.append(ef)
            else:
                g_key = (ef.get("season"), ef.get("quality"), ef.get("customFormatScore"))
                grouped[g_key].append(ef.get("episode"))
        summaries = [
            {
                "season": s,
                "quality": q,
                "customFormatScore": cf,
                "episodes_on_disk": sorted([e for e in eps if e is not None]),
                "count": len(eps),
            }
            for (s, q, cf), eps in grouped.items()
        ]
        compacted_existing = {
            "matching_candidate_episodes": direct_matches,
            "season_disk_summary": summaries,
        }

    # 3. Compact Known Episodes
    if not known_episodes or len(known_episodes) <= 15:
        compacted_known: Any = known_episodes or []
    else:
        by_season: Dict[Any, Dict[str, Any]] = defaultdict(
            lambda: {"total_episodes": 0, "episodes_with_file": [], "missing_episodes": []}
        )
        relevant_episodes = []
        for ep in known_episodes:
            s_num = ep.get("season")
            e_num = ep.get("episode")
            has_file = bool(ep.get("hasFile", False))
            s_entry = by_season[s_num]
            s_entry["total_episodes"] += 1
            if has_file:
                if e_num is not None:
                    s_entry["episodes_with_file"].append(e_num)
            else:
                if e_num is not None:
                    s_entry["missing_episodes"].append(e_num)

            if (s_num, e_num) in referenced_ep_keys or not has_file:
                if len(relevant_episodes) < 20:
                    relevant_episodes.append(ep)

        if not relevant_episodes:
            relevant_episodes = known_episodes[:10]

        season_summaries = [
            {
                "season": s,
                "total_episodes": info["total_episodes"],
                "has_file_count": len(info["episodes_with_file"]),
                "missing_episodes": sorted(info["missing_episodes"]),
            }
            for s, info in sorted(by_season.items(), key=lambda x: (x[0] is None, x[0]))
        ]
        compacted_known = {
            "season_summaries": season_summaries,
            "relevant_episodes": relevant_episodes,
        }

    return compacted_candidates, compacted_existing, compacted_known


def validate_stage1_payload(data: Any) -> List[str]:
    """Validate raw parsed JSON for Stage1TriageResult. Returns list of error descriptions (empty if valid)."""
    if not isinstance(data, dict):
        return ["Response must be a JSON object"]

    errors: List[str] = []
    action = data.get("action")
    if action not in STAGE1_VALID_ACTIONS:
        errors.append(
            f"Missing or invalid 'action': got {action!r}, expected one of {sorted(STAGE1_VALID_ACTIONS)}"
        )

    conf = data.get("confidence")
    if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append(f"Missing or non-numeric 'confidence': got {conf!r}, expected float between 0.0 and 1.0")
    elif not (0.0 <= float(conf) <= 1.0):
        errors.append(f"'confidence' {conf} out of range [0.0, 1.0]")

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("Missing or empty 'reasoning' string")

    return errors


def validate_stage2_payload(
    data: Any,
    candidate_media: Optional[Dict[str, Any]],
    library_candidates: Optional[List[Dict[str, Any]]],
    manual_import_files: List[Dict[str, Any]],
) -> List[str]:
    """Validate raw parsed JSON for Stage2DecisionResult. Returns list of error descriptions (empty if valid)."""
    if not isinstance(data, dict):
        return ["Response must be a JSON object"]

    errors: List[str] = []
    decision = data.get("decision")
    if decision not in STAGE2_VALID_DECISIONS:
        errors.append(
            f"Missing or invalid 'decision': got {decision!r}, expected one of {sorted(STAGE2_VALID_DECISIONS)}"
        )

    conf = data.get("confidence")
    if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append(f"Missing or non-numeric 'confidence': got {conf!r}, expected float between 0.0 and 1.0")
    elif not (0.0 <= float(conf) <= 1.0):
        errors.append(f"'confidence' {conf} out of range [0.0, 1.0]")

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("Missing or empty 'reasoning' string")

    valid_media_ids = set()
    if candidate_media and candidate_media.get("id") is not None:
        valid_media_ids.add(int(candidate_media["id"]))
    for lc in library_candidates or []:
        if isinstance(lc, dict) and lc.get("id") is not None:
            valid_media_ids.add(int(lc["id"]))

    valid_paths = {m.get("path") for m in manual_import_files if isinstance(m, dict) and m.get("path")}

    files_to_import = data.get("files_to_import")
    matched_id = data.get("matched_media_id")
    try:
        matched_id_int = int(matched_id) if matched_id is not None else None
    except (ValueError, TypeError):
        matched_id_int = None

    if matched_id is not None and valid_media_ids and matched_id_int not in valid_media_ids:
        errors.append(
            f"Invalid 'matched_media_id': got {matched_id!r}, must be a valid series/movie ID from {sorted(valid_media_ids)} (do NOT use episode IDs)"
        )
    elif decision == "import" and valid_media_ids and matched_id_int not in valid_media_ids:
        errors.append(
            f"Missing 'matched_media_id' for import: must be one of {sorted(valid_media_ids)}"
        )

    if decision == "import":
        if manual_import_files and (not isinstance(files_to_import, list) or len(files_to_import) == 0):
            errors.append("When 'decision' is 'import', 'files_to_import' must be a non-empty list of file objects")

    if isinstance(files_to_import, list):
        for idx, item in enumerate(files_to_import):
            if not isinstance(item, dict):
                errors.append(
                    f"files_to_import[{idx}] must be an object with 'path', 'season_number', 'episode_numbers', got {item!r}"
                )
            else:
                p = item.get("path")
                if not p or not isinstance(p, str):
                    errors.append(f"files_to_import[{idx}] is missing a valid 'path' string")
                elif valid_paths and p not in valid_paths:
                    errors.append(
                        f"files_to_import[{idx}].path {p!r} does not match any candidate file path in {sorted(valid_paths)}"
                    )
    elif files_to_import is not None:
        errors.append(f"'files_to_import' must be a list, got {type(files_to_import).__name__}")

    return errors


def validate_backfill_payload(data: Any, candidate_releases: List[Dict[str, Any]]) -> List[str]:
    """Validate raw parsed JSON for BackfillPackMatchResult. Returns list of error descriptions (empty if valid)."""
    if not isinstance(data, dict):
        return ["Response must be a JSON object"]

    errors: List[str] = []
    if "matched_guid" not in data:
        errors.append("Missing required key 'matched_guid' (must be string GUID or null)")
    else:
        m_guid = data.get("matched_guid")
        valid_guids = {str(r.get("guid")) for r in candidate_releases if isinstance(r, dict) and r.get("guid")}
        if m_guid is not None and valid_guids and str(m_guid) not in valid_guids:
            errors.append(
                f"Invalid 'matched_guid': got {m_guid!r}, must be one of {sorted(valid_guids)} or null"
            )

    conf = data.get("confidence")
    if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append(f"Missing or non-numeric 'confidence': got {conf!r}, expected float between 0.0 and 1.0")
    elif not (0.0 <= float(conf) <= 1.0):
        errors.append(f"'confidence' {conf} out of range [0.0, 1.0]")

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("Missing or empty 'reasoning' string")

    return errors


def validate_bulk_episodes_payload(
    data: Any,
    candidate_releases: List[Dict[str, Any]],
    missing_episodes: List[Dict[str, Any]],
) -> List[str]:
    """Validate raw parsed JSON for BackfillBulkEpisodesMatchResult. Returns list of error descriptions."""
    if not isinstance(data, dict):
        return ["Response must be a JSON object"]

    errors: List[str] = []
    matches = data.get("matches")
    if matches is None or not isinstance(matches, list):
        return ["Missing or invalid 'matches' array"]

    valid_guids = {str(r.get("guid")) for r in candidate_releases if isinstance(r, dict) and r.get("guid")}
    valid_ep_nums = {
        int(e.get("episodeNumber"))
        for e in missing_episodes
        if isinstance(e, dict) and e.get("episodeNumber") is not None
    }

    for idx, m in enumerate(matches):
        if not isinstance(m, dict):
            errors.append(f"matches[{idx}] must be an object")
            continue

        ep_num = m.get("episode_number")
        if ep_num is None or not isinstance(ep_num, int) or isinstance(ep_num, bool):
            errors.append(f"matches[{idx}] has missing or non-integer 'episode_number': {ep_num!r}")
        elif valid_ep_nums and ep_num not in valid_ep_nums:
            errors.append(f"matches[{idx}].episode_number {ep_num} is not in missing episodes {sorted(valid_ep_nums)}")

        m_guid = m.get("matched_guid")
        if m_guid is not None and valid_guids and str(m_guid) not in valid_guids:
            errors.append(f"matches[{idx}].matched_guid {m_guid!r} is not in candidate GUIDs {sorted(valid_guids)}")

        conf = m.get("confidence")
        if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
            errors.append(f"matches[{idx}] has missing or non-numeric 'confidence': {conf!r}")
        elif not (0.0 <= float(conf) <= 1.0):
            errors.append(f"matches[{idx}].confidence {conf} out of range [0.0, 1.0]")

    return errors


def validate_alias_payload(data: Any) -> List[str]:
    """Validate raw parsed JSON for AliasVerificationResult."""
    if not isinstance(data, dict):
        return ["Response must be a JSON object"]
    errors: List[str] = []
    if "is_same_media" not in data:
        errors.append("Missing required key 'is_same_media' (boolean)")
    elif not isinstance(data.get("is_same_media"), bool):
        errors.append(f"'is_same_media' must be a boolean, got {type(data.get('is_same_media')).__name__}")

    conf = data.get("confidence")
    if conf is None or not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append(f"Missing or non-numeric 'confidence': got {conf!r}")
    elif not (0.0 <= float(conf) <= 1.0):
        errors.append(f"'confidence' {conf} out of range [0.0, 1.0]")

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append("Missing or empty 'reasoning' string")

    return errors


def build_stage3_repair_prompt(
    stage_name: str,
    raw_output: str,
    validation_errors: List[str],
    target_schema: str,
    reference_context: Dict[str, Any],
) -> Tuple[str, str]:
    """
    Build the system and user prompts for Stage 3 AI Output Interpreter to repair a malformed
    Stage 1 or Stage 2 AI response into the strict target JSON schema.
    """
    system_prompt = (
        "You are a Stage 3 JSON Schema Repair & Output Interpreter for AImportarr. "
        "A previous AI stage produced an incomplete or malformed JSON response. "
        "Your job is to interpret the intent of the malformed output, map any loose references "
        "(such as episode names, episode IDs, or action aliases) to the exact valid IDs and file paths "
        "from the Reference Context, and output ONLY a valid JSON object matching the Target Schema."
    )
    user_prompt = (
        f"Failed Stage: {stage_name}\n"
        f"Validation Errors Detected:\n- " + "\n- ".join(validation_errors) + "\n\n"
        f"Malformed Raw Output from Previous Stage:\n{raw_output}\n\n"
        f"Reference Context (Valid IDs & Candidate Paths):\n{json.dumps(reference_context, ensure_ascii=False)}\n\n"
        f"REQUIRED TARGET JSON SCHEMA:\n{target_schema}"
    )
    return system_prompt, user_prompt



class Stage1TriageResult(BaseModel):
    action: str = Field(
        default="no_extraction",
        description="One of: 'extract', 'no_extraction', 'reject_delete'"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the Stage 1 triage decision")
    category_tag: str = Field(default="malware_or_corrupt", description="Short tag such as malware_or_corrupt, sample_only, archive_extracted")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")


class ApprovedFileItem(BaseModel):
    path: str = Field(default="", description="Exact file path from the candidate files list")
    season_number: Optional[int] = Field(default=None, description="Season number (for Sonarr)")
    episode_numbers: List[int] = Field(default_factory=list, description="Episode numbers (for Sonarr)")


class Stage2DecisionResult(BaseModel):
    decision: str = Field(
        default="reject_keep_in_client",
        description="One of: 'import', 'reject_keep_in_client', 'reject_delete', 'dismiss_pack'"
    )
    matched_media_id: Optional[int] = Field(default=None, description="Matched seriesId or movieId")
    files_to_import: List[ApprovedFileItem] = Field(default_factory=list, description="Files approved for import")
    category_tag: str = Field(default="ai_decision", description="Short tag describing the primary reason")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the Stage 2 import/reject decision")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")

    def __init__(self, **kwargs):
        if "files_to_import" in kwargs and isinstance(kwargs["files_to_import"], list):
            kwargs["files_to_import"] = [
                ApprovedFileItem(**f) if isinstance(f, dict) else f
                for f in kwargs["files_to_import"]
            ]
        super().__init__(**kwargs)


class BackfillPackMatchResult(BaseModel):
    matched_guid: Optional[str] = Field(default=None, description="GUID of the best matching season pack candidate, or null if none match")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of the season pack title match decision")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")


class EpisodeMatchItem(BaseModel):
    episode_number: int = Field(..., description="Episode number being matched")
    matched_guid: Optional[str] = Field(default=None, description="GUID of the matching candidate release, or null if no valid match")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of why this release matches or why no match was made")


class BackfillBulkEpisodesMatchResult(BaseModel):
    matches: List[EpisodeMatchItem] = Field(default_factory=list, description="Per-episode matching decisions")
    raw_response: str = Field(default="", description="Raw response text")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider that generated this result")


class AliasVerificationResult(BaseModel):
    is_same_media: bool = Field(default=False, description="True if release title represents the exact same media (original, translation, or alias)")
    media_relationship: str = Field(
        default="completely_different_media",
        description="One of: 'identical', 'official_translation', 'alternate_alias', 'completely_different_media'"
    )
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    reasoning: str = Field(default="", description="Explanation of whether release title and candidate media are the same work")
    model_name: Optional[str] = Field(default=None, description="Name of the model or provider")


    def __init__(self, **kwargs):
        if "matches" in kwargs and isinstance(kwargs["matches"], list):
            kwargs["matches"] = [
                EpisodeMatchItem(**m) if isinstance(m, dict) else m
                for m in kwargs["matches"]
            ]
        super().__init__(**kwargs)


class BaseAIProvider(ABC):
    """Abstract interface for Two-Stage AI Providers (Gemini, Ollama, etc.)"""

    @property
    def name(self) -> str:
        return self.__class__.__name__

    @abstractmethod
    async def evaluate_stage1_triage(
        self,
        release_title: str,
        error_messages: List[str],
        disk_files: List[str],
        archive_name: Optional[str] = None,
        archive_files: Optional[List[str]] = None,
    ) -> Stage1TriageResult:
        """
        Stage 1 AI Triage: Determine if a stuck release requires archive extraction ('extract'),
        no extraction ('no_extraction'), or immediate deletion & blocklisting as virus/sample/corrupt ('reject_delete').
        """
        pass

    @abstractmethod
    async def evaluate_import_decision(
        self,
        media_type: str,
        release_title: str,
        folder_name: str,
        queue_messages: List[str],
        candidate_media: Optional[Dict[str, Any]],
        library_candidates: Optional[List[Dict[str, Any]]],
        manual_import_files: List[Dict[str, Any]],
        existing_disk_files: List[Dict[str, Any]],
        known_episodes: Optional[List[Dict[str, Any]]] = None,
        raw_disk_files: Optional[List[str]] = None,
    ) -> Stage2DecisionResult:
        """
        Stage 2 Unified AI Decision: Determine whether to 'import', 'reject_keep_in_client' (downgrade/wrong media),
        'reject_delete' (virus/sample-only/corrupt), or 'dismiss_pack' (already satisfied season pack).
        """
        pass

    async def evaluate_backfill_season_pack(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        """
        Tier 2 AI Fallback for Season Pack Backfill: Given a target series + season and a list of
        'Unknown Series' season pack releases from Sonarr search, identify the best matching valid pack.
        """
        return BackfillPackMatchResult(matched_guid=None, confidence=0.0, reasoning="Not implemented")

    async def evaluate_backfill_episode(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        episode_number: int,
        episode_title: str,
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        """
        Tier 2 AI Fallback for Ongoing Season Missing Episode Retry: Given a target series + SxxExx and a list of
        'Unknown Series' episode releases from Sonarr search, identify the best matching valid episode release.
        """
        return BackfillPackMatchResult(matched_guid=None, confidence=0.0, reasoning="Not implemented")

    async def evaluate_backfill_episodes_bulk(
        self,
        series_info: Dict[str, Any],
        season_number: int,
        missing_episodes: List[Dict[str, Any]],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillBulkEpisodesMatchResult:
        """
        Tier 2 Bulk AI Fallback for Ongoing Season Missing Episodes: Given a target series + season,
        a list of missing episodes, and a list of candidate releases from the bulk season search,
        identify the best matching valid release for each missing episode in a single AI call.
        """
        return BackfillBulkEpisodesMatchResult(matches=[], model_name=self.name)

    async def evaluate_missing_movie(
        self,
        movie_info: Dict[str, Any],
        candidate_releases: List[Dict[str, Any]],
    ) -> BackfillPackMatchResult:
        """
        AI Fallback for Radarr Missing Movie Search: Given a target movie and a list of
        'Unknown Movie' / rejected releases from Radarr search, identify the best matching valid movie release.
        """
        return BackfillPackMatchResult(matched_guid=None, confidence=0.0, reasoning="Not implemented")

    async def verify_media_alias(
        self,
        release_title: str,
        candidate_media: Dict[str, Any],
        media_type: str = "series",
    ) -> AliasVerificationResult:
        """
        Focused zero-shot identity verification: Confirm whether a release title is genuinely an
        alternate title, translation, or alias of candidate_media, or an entirely different show/movie.
        """
        return AliasVerificationResult(
            is_same_media=False,
            media_relationship="completely_different_media",
            confidence=0.0,
            reasoning="verify_media_alias not implemented",
            model_name=self.name,
        )

