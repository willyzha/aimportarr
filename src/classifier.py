from enum import Enum
from typing import List, Optional, Dict, Any

class ErrorCategory(str, Enum):
    FAKE_OR_MALWARE = "fake_or_malware"
    ID_MATCH = "id_match"
    UNKNOWN_MEDIA = "unknown_media"
    UNKNOWN_EPISODE = "unknown_episode"
    SAMPLE_OR_EXTRA = "sample_or_extra"
    QUALITY_OR_LANGUAGE = "quality_or_language"
    MISSING_EPISODES = "missing_episodes"
    OTHER = "other"  # Fallback / Catch-all category

QUALITY_REJECTION_KEYWORDS = [
    "not a custom format upgrade",
    "not an upgrade",
    "higher quality",
    "equal or higher quality",
    "equal or higher preference",
    "existing file on disk",
    "better quality",
    "quality not wanted",
    "language not wanted",
    "cutoff unmet",
    "meets cutoff",
    "custom format score",
    "custom format cutoff",
    "quality profile",
]

ALREADY_IMPORTED_KEYWORDS = [
    "already imported",
    "episode file already imported",
    "movie file already imported",
]

def classify_issue(
    messages: List[str],
    has_media_info: bool = True,
    manual_rejections: Optional[List[str]] = None,
    title: Optional[str] = None
) -> ErrorCategory:
    """
    Classify a Sonarr/Radarr queue warning or import failure into a configured ErrorCategory.
    Analyzes status messages from the queue, release title, and rejection reasons from /manualimport.
    """
    combined_texts = [m.lower() for m in messages]
    if manual_rejections:
        combined_texts.extend([r.lower() for r in manual_rejections])
    if title:
        combined_texts.append(title.lower())
    all_text = " ".join(combined_texts)

    # 1. Fake or Malware (Unsupported archives, executables, malware signatures)
    if any(phrase in all_text for phrase in [
        "unsupported extension: '.7z'",
        "unsupported extension: '.exe'",
        "unsupported extension: '.scr'",
        "unsupported extension: '.bat'",
        "unsupported extension: '.cmd'",
        "unsupported extension: '.iso'",
        "invalid video file",
        "has unsupported extension",
        ".7z",
        ".exe",
        "malware",
        "virus",
        "trojan",
    ]):
        return ErrorCategory.FAKE_OR_MALWARE

    # 2. ID Match guardrail (Release matched by ID, blocked from automatic import)
    if "matched to series by id" in all_text or "matched to movie by id" in all_text or "matched by id" in all_text:
        return ErrorCategory.ID_MATCH

    # 2. Unknown Media (Unknown series or unknown movie)
    if not has_media_info or any(phrase in all_text for phrase in [
        "unknown series",
        "unknown movie",
        "no series was found",
        "no movie was found",
        "not matched to any series",
        "not matched to any movie"
    ]):
        return ErrorCategory.UNKNOWN_MEDIA

    # 3. Unknown Episode (Series is known, but episode could not be parsed)
    if any(phrase in all_text for phrase in [
        "unknown episode",
        "unable to parse episode",
        "failed to map episode",
        "episode was not found",
        "does not match any existing episode"
    ]):
        return ErrorCategory.UNKNOWN_EPISODE

    # 4. Sample or Extra (File flagged as a short video/sample)
    if any(phrase in all_text for phrase in [
        "sample file",
        "is a sample",
        "short video",
        "trailer",
        "extra detected"
    ]):
        return ErrorCategory.SAMPLE_OR_EXTRA

    # 5. Quality or Language (Rejected due to quality cutoff or language profile)
    if any(phrase in all_text for phrase in QUALITY_REJECTION_KEYWORDS) or "custom format" in all_text:
        return ErrorCategory.QUALITY_OR_LANGUAGE

    # 6. Expected episodes missing from release (e.g. season pack with only single/subset episodes present)
    if any(phrase in all_text for phrase in [
        "one or more episodes expected in this release were not imported",
        "missing from the release",
        "missing from this release",
        "missing from release",
        "episodes expected in this release were not imported",
        "partially imported"
    ]):
        return ErrorCategory.MISSING_EPISODES

    # 7. Fallback / Catch-all for any other error or warning status
    return ErrorCategory.OTHER


DEFAULT_FALLBACK_QUALITY: Dict[str, Any] = {
    "quality": {
        "id": 5,
        "name": "WEBDL-720p",
        "source": "webdl",
        "resolution": 720,
        "modifier": "none",
    },
    "revision": {
        "version": 1,
        "real": 0,
        "isRepack": False,
    },
}


def resolve_import_quality(
    file_quality: Optional[Dict[str, Any]],
    queue_quality: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Ensure the quality payload sent to Radarr/Sonarr ManualImport is never null, empty, or zero.
    Radarr and Sonarr SQLite databases enforce NOT NULL constraints on MovieFiles.Quality
    and EpisodeFiles.Quality. Passing null causes an unhandled SQLite constraint exception.
    """
    if isinstance(file_quality, dict) and isinstance(file_quality.get("quality"), dict):
        q_id = file_quality["quality"].get("id", 0)
        if q_id and q_id > 0:
            return file_quality

    if isinstance(queue_quality, dict) and isinstance(queue_quality.get("quality"), dict):
        q_id = queue_quality["quality"].get("id", 0)
        if q_id and q_id > 0:
            return queue_quality

    return {
        "quality": {
            "id": 5,
            "name": "WEBDL-720p",
            "source": "webdl",
            "resolution": 720,
            "modifier": "none",
        },
        "revision": {
            "version": 1,
            "real": 0,
            "isRepack": False,
        },
    }


def is_plausible_title_match(
    release_title: str,
    media_title: str,
    aliases: Optional[List[str]] = None,
) -> bool:
    """
    Check if a release title has plausible identity alignment with a media title or its aliases.
    Prevents hallucinated imports where an AI blindly approves importing a release simply because
    Sonarr/Radarr matched it by ID or grab history (e.g. 'A Love Other Than Yours' into '100 Days of Deception').
    """
    if not release_title or not media_title:
        return False

    # 1. Non-ASCII characters (e.g. CJK, Cyrillic, Arabic) often translate into English titles,
    # so we allow AI translation to hold for non-Latin releases.
    if any(ord(c) > 127 for c in release_title) or any(ord(c) > 127 for c in media_title):
        return True

    import re
    stopwords = {"a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "with", "by", "from", "s", "season", "complete"}

    def tokenize(s: str) -> set:
        s = re.sub(
            r"\b(1080p|720p|2160p|4k|bluray|web-dl|webdl|webrip|h264|x264|h265|x265|hevc|ddp2|aac|amzn|nf|repack|proper|ldrip|dvdrip)\b",
            " ",
            s,
            flags=re.I,
        )
        words = re.findall(r"[a-z0-9]+", s.lower())
        return {w for w in words if w not in stopwords and not w.isdigit() and len(w) > 1}

    rel_tokens = tokenize(release_title)
    media_tokens = tokenize(media_title)

    if rel_tokens and media_tokens and bool(rel_tokens & media_tokens):
        return True

    for alias in aliases or []:
        if not alias:
            continue
        if any(ord(c) > 127 for c in alias):
            return True
        alias_tokens = tokenize(alias)
        if rel_tokens and alias_tokens and bool(rel_tokens & alias_tokens):
            return True

    return False



