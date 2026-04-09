"""Domain models that adapt `ProjectRecord` instances for OAI-PMH exports."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID

import logging
import mimetypes
import os

from django.conf import settings
from django.db.models import Count, Q

from arkumu.projects import ProjectDigitalObject, ProjectDigitalObjectLicense, ProjectRecord
from arkumu.projects.fixity import FixityInfo, parse_fixity
from arkumu.oaipmh.models import OAIProjectMediaLink
from arkumu.oaipmh.services import dcp_index

from .path_mapping import resolve_external_paths

# In-memory cache for DCP folder mappings (digital_object_uri -> folder_name)
# Loaded once from Triple table, avoids N+1 queries during snapshot generation
_DCP_FOLDER_CACHE: Dict[str, str] = {}
_DCP_FOLDER_CACHE_LOADED = False


def _load_dcp_folder_cache() -> None:
    """Load all KHM DCP folder mappings into memory (one-time)."""
    global _DCP_FOLDER_CACHE, _DCP_FOLDER_CACHE_LOADED
    if _DCP_FOLDER_CACHE_LOADED:
        return

    from arkumu.metadata.models import Triple

    DCP_PREDICATE = "http://arkumu.org/data/khm/properties/dateipfad-dcp-ordner"
    triples = Triple.objects.filter(
        predicate__uri=DCP_PREDICATE,
    ).select_related("subject", "object")

    for t in triples:
        uri = getattr(t.subject, "uri", None)
        value = getattr(t.object, "value", None)
        if uri and value:
            path = str(value).strip().replace("\\", "/")
            folder_name = path.rstrip("/").split("/")[-1] if "/" in path else path
            _DCP_FOLDER_CACHE[uri] = folder_name

    _DCP_FOLDER_CACHE_LOADED = True
    logger.info("DCP folder cache loaded: %d mappings", len(_DCP_FOLDER_CACHE))


HARVESTABLE_STORAGE_STATUSES = {"completed", "verified"}


logger = logging.getLogger(__name__)


def _clean(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    normalized = str(value).strip()
    return normalized or None


def _guess_mime_type(obj: ProjectDigitalObject) -> Optional[str]:
    """Derive a MIME type from explicit metadata or file extension."""

    if getattr(obj, "content_type", None):
        return obj.content_type

    candidates = [obj.file_name, obj.access_url, obj.path, obj.storage_key]
    for candidate in candidates:
        candidate = _clean(candidate)
        if not candidate:
            continue
        guess = mimetypes.guess_type(candidate)[0]
        if guess:
            return guess
    return None


def _infer_file_name(obj: ProjectDigitalObject) -> Optional[str]:
    """Return the most likely filename for a digital object."""

    if getattr(obj, "file_name", None):
        return _clean(obj.file_name)

    candidates = [obj.path, obj.storage_key, obj.access_url]
    for candidate in candidates:
        candidate = _clean(candidate)
        if not candidate:
            continue
        try:
            name = Path(candidate).name
        except ValueError:
            name = candidate
        if name == candidate:
            for separator in ("/", "\\"):
                if separator in candidate:
                    name = candidate.split(separator)[-1]
                    break
        name = _clean(name)
        if name:
            return name
    return None


def _status_token(status: Optional[str]) -> Optional[str]:
    if not status:
        return None
    return status.lower().strip() or None


def _apply_rosetta_prefix(prefix: str, path: str) -> str:
    """Join a configured prefix with a curated Rosetta path."""

    normalized_prefix = (prefix or "").rstrip("/")
    if not normalized_prefix:
        return path
    token = (path or "").lstrip()
    if token.startswith("/"):
        return f"{normalized_prefix}{token}"
    token = token.lstrip("/")
    if not token:
        return normalized_prefix
    return f"{normalized_prefix}/{token}"


@dataclass(frozen=True)
class CuratedLinkWarning:
    code: str
    message: Optional[str] = None
    digital_object_uri: Optional[str] = None


@dataclass(frozen=True)
class CuratedMediaSelection:
    ordered_resource_ids: Tuple[str, ...]
    ordered_object_uris: Tuple[str, ...]
    resource_uri_pairs: Tuple[Tuple[str, Optional[str]], ...]
    label_overrides_by_id: Mapping[str, str]
    label_overrides_by_uri: Mapping[str, str]
    curated_missing_uris: Tuple[str, ...]
    graph_only_uris: Tuple[str, ...]
    warnings: Tuple[CuratedLinkWarning, ...]


@dataclass(frozen=True)
class NormalizedDigitalObject:
    """Normalized digital object enriched with Rosetta/S3 metadata."""

    uri: Optional[str]
    original_path: Optional[str]
    storage_key: Optional[str]
    rosetta_path: Optional[str]
    rosetta_candidates: Tuple[str, ...]
    file_name: Optional[str]
    content_type: Optional[str]
    size_bytes: Optional[int]
    checksum: Optional[str]
    checksum_algorithm: Optional[str]
    checksum_provenance: Optional[str]
    access_url: Optional[str]
    storage_status: Optional[str]
    source: str
    uuid: Optional[str] = None
    genesis_type: Optional[str] = None
    media_type: Optional[str] = None
    significant_properties_de: Optional[str] = None
    significant_properties_en: Optional[str] = None
    license: Optional[ProjectDigitalObjectLicense] = None
    resource_id: Optional[str] = None
    label_override: Optional[str] = None
    download_href: Optional[str] = None

    @property
    def harvestable(self) -> bool:
        """Return True when the object is eligible for OAI dissemination."""

        if self.source == "rosetta":
            return bool(self.rosetta_path)

        if self.source == "s3":
            if not self.storage_key:
                return False
            status = _status_token(self.storage_status)
            return status in HARVESTABLE_STORAGE_STATUSES

        # Default fallback for objects with limited metadata
        return bool(self.rosetta_path or self.storage_key)

    @property
    def preferred_location(self) -> Optional[str]:
        """Return the location that should appear in METS FLocat.

        For METS, prefer rosetta_path (local hbz path) over download_href (S3 URL)
        since hbz harvests files from S3 and stores them locally.
        """
        if self.rosetta_path:
            return self.rosetta_path
        if self.download_href:
            return self.download_href
        if self.storage_key:
            return self.storage_key
        return self.original_path or self.access_url

    def checksum_tuple(self) -> Tuple[Optional[str], Optional[str]]:
        return self.checksum_algorithm, self.checksum

    def checksum_label(self) -> Optional[str]:
        if not self.checksum_algorithm:
            return None
        normalized = self.checksum_algorithm.lower().replace('-', '')
        mapping = {
            'sha256': 'SHA-256',
            'sha1': 'SHA-1',
            'sha512': 'SHA-512',
            'md5': 'MD5',
        }
        return mapping.get(normalized, self.checksum_algorithm.upper())

    @property
    def display_label(self) -> Optional[str]:
        if self.label_override:
            return self.label_override
        return self.file_name


@dataclass(frozen=True)
class OAIProject:
    """Immutable view over a `ProjectRecord` tailored for OAI-PMH."""

    record: ProjectRecord
    institution_code: Optional[str]
    digital_objects: Tuple[NormalizedDigitalObject, ...]
    curated_selection: Optional[CuratedMediaSelection] = None
    graph_data: Optional[Dict[str, Any]] = None  # For RDF generation without queries

    @property
    def harvestable(self) -> bool:
        return any(obj.harvestable for obj in self.digital_objects)

    @property
    def uri(self) -> str:
        return self.record.uri

    def primary_object(self) -> Optional[NormalizedDigitalObject]:
        for obj in self.digital_objects:
            if obj.harvestable:
                return obj
        return None

    def mime_types(self) -> Tuple[str, ...]:
        values: List[str] = []
        seen: set[str] = set()
        for obj in self.digital_objects:
            mime = _clean(obj.content_type)
            if not mime or mime in seen:
                continue
            seen.add(mime)
            values.append(mime)
        return tuple(values)


class OAIProjectBuilder:
    """Factory that enriches snapshot records with harvest metadata."""

    def __init__(
        self,
        *,
        s3_orgs: Optional[Sequence[str]] = None,
        rosetta_orgs: Optional[Sequence[str]] = None,
        path_resolver: Callable[..., List[str]] = resolve_external_paths,
        code_aliases: Optional[dict[str, str]] = None,
        label_aliases: Optional[dict[str, str]] = None,
    ) -> None:
        self._s3_orgs = {
            code.lower().strip()
            for code in (
                s3_orgs
                if s3_orgs is not None
                else getattr(settings, "OAI_S3_HARVESTABLE_ORGS", ())
            )
            if code
        }
        self._rosetta_orgs = {
            code.lower().strip()
            for code in (
                rosetta_orgs
                if rosetta_orgs is not None
                else getattr(settings, "OAI_ROSETTA_HARVESTABLE_ORGS", ())
            )
            if code
        }
        self._path_resolver = path_resolver
        alias_cfg = code_aliases if code_aliases is not None else getattr(settings, 'OAI_INSTITUTION_CODE_ALIASES', {})
        self._code_aliases = {
            str(alias).lower().strip(): str(target).lower().strip()
            for alias, target in alias_cfg.items()
            if alias and target
        }
        label_cfg = label_aliases if label_aliases is not None else getattr(settings, 'OAI_INSTITUTION_LABEL_ALIASES', {})
        self._label_aliases = {
            str(label).lower().strip(): str(code).lower().strip()
            for label, code in label_cfg.items()
            if label and code
        }
        base_cfg = getattr(settings, 'OAI_S3_ROSETTA_BASE_PATHS', {})
        self._s3_rosetta_bases = {
            str(code).lower().strip(): str(path).rstrip('/')
            for code, path in base_cfg.items()
            if code and path
        }
        prefix_cfg = getattr(settings, 'OAI_ROSETTA_CURATED_PREFIXES', {}) or {}
        curated_prefixes = {
            str(code).lower().strip(): str(path).rstrip('/')
            for code, path in prefix_cfg.items()
            if code and path
        }
        fallback_prefixes = getattr(settings, 'OAI_EXTERNAL_PATH_PREFIXES', {}) or {}
        for code, candidates in fallback_prefixes.items():
            normalized_code = str(code).lower().strip()
            if not normalized_code or normalized_code in curated_prefixes:
                continue
            if not candidates:
                continue
            for candidate in candidates:
                candidate = str(candidate or '').strip()
                if not candidate:
                    continue
                curated_prefixes[normalized_code] = candidate.rstrip('/')
                break
        self._rosetta_curated_prefixes = curated_prefixes

    def from_project_record(
        self,
        record: ProjectRecord,
        *,
        skip_shared_event_filter: bool = False,
        skip_format_exclusion: bool = False,
        use_curated_media_links: bool = False,
    ) -> OAIProject:
        institution_code = self._resolve_institution_code(record)

        filtered_record = record
        if not skip_shared_event_filter:
            # For KHM/HMT: Filter out digital objects from shared events to prevent cross-project contamination
            filtered_record = self._filter_shared_event_objects(filtered_record, institution_code)
        filtered_record = self._filter_flagged_digital_objects(filtered_record)

        curated_selection: Optional[CuratedMediaSelection] = None
        if use_curated_media_links:
            curated_selection = self._resolve_curated_selection(filtered_record)

        normalized_objects = self._normalize_objects(
            filtered_record,
            institution_code,
            skip_format_exclusion=skip_format_exclusion,
            curated_selection=curated_selection,
        )

        return OAIProject(
            record=filtered_record,
            institution_code=institution_code,
            digital_objects=tuple(normalized_objects),
            curated_selection=curated_selection,
        )

    # Internal helpers -----------------------------------------------------

    def _filter_shared_event_objects(
        self,
        record: ProjectRecord,
        institution_code: Optional[str],
    ) -> ProjectRecord:

        if getattr(record, "ownership_filtered", False):
            return record
        """
        For KHM/HMT only: Filter out digital objects from events that are shared with other projects.

        This prevents cross-project contamination where projects sharing the same event
        (e.g., "Showcase 2007" linked to multiple projects) would incorrectly include
        each other's digital objects.

        Rule: Only include digital objects from events that are exclusively linked to this project.

        Performance: Uses a single aggregation query instead of N queries (one per event).
        """
        # Only apply filtering for KHM and HMT organizations
        if not institution_code or institution_code.lower() not in {'khm', 'hmt'}:
            return record

        # If there are no events or digital objects, nothing to filter
        events = getattr(record, 'events', None)
        digital_objects = getattr(record, 'digital_objects', None)
        if not events or not digital_objects:
            return record

        # Import here to avoid circular dependencies
        from arkumu.metadata.models.triples import Triple

        # Get the canonical event predicate URI
        event_predicate = "http://arkumu.org/data/properties/ereignis"

        # Find which events are exclusive to this project
        project_id = getattr(record, 'subject_id', None)
        if not project_id:
            return record

        # Collect all event IDs
        raw_event_ids = [getattr(event, 'id', None) for event in events if getattr(event, 'id', None)]
        valid_event_ids: List[str] = []
        for candidate in raw_event_ids:
            try:
                UUID(str(candidate))
            except (ValueError, TypeError, AttributeError):
                continue
            valid_event_ids.append(str(candidate))

        if not valid_event_ids:
            return record

        # OPTIMIZED: Single query to get all event-project relationships
        # Instead of N queries (one per event), we fetch all relationships at once
        event_project_relationships = Triple.objects.filter(
            predicate__canonical_uri=event_predicate,
            object_id__in=valid_event_ids
        ).values('object_id', 'subject_id')

        # Build a mapping of event_id -> set of project_ids that reference it
        event_to_projects = {}
        for relationship in event_project_relationships:
            event_id = str(relationship['object_id'])
            proj_id = str(relationship['subject_id'])
            if event_id not in event_to_projects:
                event_to_projects[event_id] = set()
            event_to_projects[event_id].add(proj_id)

        # Filter to find exclusive events (only linked to this project)
        exclusive_event_ids = set()
        for event_id in valid_event_ids:
            project_refs = event_to_projects.get(event_id, set())

            # Only include events exclusively linked to this project
            if len(project_refs) == 1 and str(project_id) in project_refs:
                exclusive_event_ids.add(event_id)
            elif len(project_refs) == 0:
                # Orphaned event - include it since it was in get_detailed_event_data
                exclusive_event_ids.add(event_id)
            else:
                logger.info(
                    "OAI: Excluding shared event %s from project %s (shared with %d other projects)",
                    event_id,
                    project_id,
                    len(project_refs) - 1,
                )

        # If no exclusive events, return record as-is (no filtering needed)
        if not exclusive_event_ids:
            return record

        # Filter the events list to only include exclusive events
        filtered_events = [
            event for event in events
            if str(getattr(event, 'id', None)) in exclusive_event_ids
        ]

        # Create a new record with filtered events
        # We need to create a new ProjectRecord instance with the filtered events
        filtered_record = replace(record, events=filtered_events)

        return filtered_record

    def _filter_flagged_digital_objects(self, record: ProjectRecord) -> ProjectRecord:
        """
        Remove digital objects that snapshot generation marked as filtered.

        Snapshot service retains the original resources for auditing via
        `filtered_digital_object_ids`. When we build OAI views we must ensure
        those resources are not exported to downstream consumers.
        """

        filtered_ids = {
            str(identifier).strip()
            for identifier in getattr(record, "filtered_digital_object_ids", []) or []
            if identifier is not None and str(identifier).strip()
        }
        if not filtered_ids:
            return record

        objects = list(getattr(record, "digital_objects", []) or [])
        if not objects:
            return record

        retained: List[ProjectDigitalObject] = []
        removed = False

        for obj in objects:
            resource_id = getattr(obj, "resource_id", None)
            if resource_id is not None and str(resource_id) in filtered_ids:
                removed = True
                continue
            retained.append(obj)

        if not removed:
            return record

        sources = getattr(record, "digital_object_sources", None)
        updated_sources = sources
        if isinstance(sources, dict):
            updated_sources = {
                key: value
                for key, value in sources.items()
                if str(key) not in filtered_ids
            }

        harvestable_flag = bool(retained)
        reference_only_flag = getattr(record, "ownership_filtered", False) and not harvestable_flag

        return replace(
            record,
            digital_objects=retained,
            digital_object_sources=updated_sources,
            harvestable=harvestable_flag,
            reference_only=reference_only_flag,
        )

    def _filter_overarching_projects(
        self,
        record: ProjectRecord,
        institution_code: Optional[str],
    ) -> ProjectRecord:
        """
        Prevent overarching HMT works (Oberwerke) from exporting digital objects.

        These umbrella records aggregate subordinate projects but should only
        reference them. They are identifiable by their slug/URI pattern
        (`hfmt-ow-*`). Any digital objects present on such records are shared
        with their sub-projects and must not be emitted to hbz.
        """

        if (institution_code or "").lower() != "hmt":
            return record

        uri = getattr(record, "uri", "") or ""
        slug = record.slug if hasattr(record, "slug") else uri.rstrip("/").split("/")[-1]
        if not slug.startswith("hfmt-ow-"):
            return record

        if not getattr(record, "digital_objects", None):
            return record

        sources = getattr(record, "digital_object_sources", None)
        updated_sources = {}
        if isinstance(sources, dict):
            updated_sources = {}

        return replace(
            record,
            digital_objects=[],
            digital_object_sources=updated_sources,
            harvestable=False,
            reference_only=True,
        )

    @staticmethod
    def _should_skip_digital_object(
        obj: ProjectDigitalObject,
        institution_code: Optional[str],
    ) -> bool:
        code = (institution_code or "").lower().strip()
        if code != "hmt":
            return False

        def _matches(candidate: Optional[str]) -> bool:
            if not candidate:
                return False
            candidate_lower = str(candidate).lower()
            return candidate_lower.endswith(".mp3")

        return any(
            _matches(candidate)
            for candidate in (
                getattr(obj, "file_name", None),
                getattr(obj, "path", None),
                getattr(obj, "storage_key", None),
            )
        )

    def _resolve_institution_code(self, record: ProjectRecord) -> Optional[str]:
        institution = getattr(record, "institution", None)
        if institution and getattr(institution, "code", None):
            code = institution.code
            if code:
                normalized = str(code).lower().strip()
                return self._code_aliases.get(normalized, normalized)

        label = getattr(institution, 'label', None) if institution else None
        if label:
            label_normalized = str(label).lower().strip()
            alias_code = self._label_aliases.get(label_normalized)
            if alias_code:
                return alias_code

        for candidate in getattr(record, "institution_codes", []) or []:
            candidate = _clean(candidate)
            if candidate:
                normalized = candidate.lower()
                return self._code_aliases.get(normalized, normalized)
        return None

    def _normalize_objects(
        self,
        record: ProjectRecord,
        institution_code: Optional[str],
        *,
        skip_format_exclusion: bool = False,
        curated_selection: Optional[CuratedMediaSelection] = None,
    ) -> List[NormalizedDigitalObject]:
        objects: List[NormalizedDigitalObject] = []
        seen: set[str] = set()
        label_by_id: Dict[str, str] = {}
        label_by_uri: Dict[str, str] = {}
        allowed_resource_ids: Optional[set[str]] = None
        allowed_uris: Optional[set[str]] = None
        capture_groups = curated_selection is not None
        grouped_by_resource: Dict[str, List[NormalizedDigitalObject]] = {}
        grouped_by_uri: Dict[str, List[NormalizedDigitalObject]] = {}

        if curated_selection:
            allowed_resource_ids = {rid for rid in curated_selection.ordered_resource_ids if rid}
            allowed_uris = {uri for uri in curated_selection.ordered_object_uris if uri}
            label_by_id = dict(curated_selection.label_overrides_by_id)
            label_by_uri = dict(curated_selection.label_overrides_by_uri)

        for obj in getattr(record, "digital_objects", []) or []:
            if not skip_format_exclusion and self._should_skip_digital_object(obj, institution_code):
                logger.info(
                    "OAI digital object skipped: format excluded (org=%s path=%s file=%s)",
                    institution_code,
                    getattr(obj, "path", None),
                    getattr(obj, "file_name", None),
                )
                continue
            # Check if this object represents a DCP folder
            expanded_objects = self._expand_dcp_folder_if_needed(obj, institution_code)

            # If DCP folder was expanded, use all files; otherwise use single object
            objects_to_process = expanded_objects if expanded_objects else [obj]

            if expanded_objects:
                logger.info(f"Processing {len(expanded_objects)} expanded DCP files for object {getattr(obj, 'uri', 'N/A')}")

            for current_obj in objects_to_process:
                resource_identifier = getattr(current_obj, "resource_id", None)
                normalized_resource_id = str(resource_identifier) if resource_identifier else None
                obj_uri = _clean(getattr(current_obj, "uri", None))
                if allowed_resource_ids is not None:
                    include = False
                    if normalized_resource_id and normalized_resource_id in allowed_resource_ids:
                        include = True
                    elif obj_uri and allowed_uris and obj_uri in allowed_uris:
                        include = True
                    if not include:
                        continue

                normalized = self._normalize_object(current_obj, institution_code)
                if not normalized:
                    continue
                if not self._is_harvestable(normalized):
                    logger.info(
                        "OAI digital object dropped: not harvestable (org=%s source=%s status=%s storage_key=%s rosetta_path=%s)",
                        institution_code,
                        normalized.source,
                        normalized.storage_status,
                        normalized.storage_key,
                        normalized.rosetta_path,
                    )
                    continue
                identity = normalized.preferred_location
                if identity:
                    identity_key = identity.lower()
                    if identity_key in seen:
                        if expanded_objects and '.dcp/' in identity:
                            logger.info(f"DCP file deduplicated: {identity}")
                        continue
                    seen.add(identity_key)

                if curated_selection:
                    label_override = None
                    if normalized.resource_id and normalized.resource_id in label_by_id:
                        label_override = label_by_id[normalized.resource_id]
                    elif normalized.uri and normalized.uri in label_by_uri:
                        label_override = label_by_uri[normalized.uri]
                    if label_override:
                        normalized = replace(normalized, label_override=label_override)

                objects.append(normalized)

                if capture_groups:
                    if normalized.resource_id:
                        grouped_by_resource.setdefault(normalized.resource_id, []).append(normalized)
                    if normalized.uri:
                        grouped_by_uri.setdefault(normalized.uri, []).append(normalized)

                if expanded_objects and '.dcp/' in identity:
                    logger.info(f"DCP file added: {identity}")

        if not curated_selection:
            return objects

        ordered_objects: List[NormalizedDigitalObject] = []
        consumed_ids: set[int] = set()

        def _extend_with_candidates(candidates: Iterable[NormalizedDigitalObject]) -> None:
            for candidate in candidates:
                candidate_id = id(candidate)
                if candidate_id in consumed_ids:
                    continue
                ordered_objects.append(candidate)
                consumed_ids.add(candidate_id)

        for resource_id in curated_selection.ordered_resource_ids:
            matches = grouped_by_resource.get(resource_id, [])
            _extend_with_candidates(matches)

        for uri in curated_selection.ordered_object_uris:
            matches = grouped_by_uri.get(uri, [])
            _extend_with_candidates(matches)

        return ordered_objects

    def _normalize_object(
        self,
        obj: ProjectDigitalObject,
        institution_code: Optional[str],
    ) -> Optional[NormalizedDigitalObject]:
        original_path = _clean(getattr(obj, "path", None))
        storage_key = _clean(getattr(obj, "storage_key", None))
        if original_path:
            original_path = original_path.replace("\\", "/")
            obj.path = original_path
        if storage_key:
            storage_key = storage_key.replace("\\", "/")
            obj.storage_key = storage_key
        if not storage_key and original_path:
            storage_key = original_path
        access_url = _clean(getattr(obj, "access_url", None))
        file_name = _infer_file_name(obj)
        content_type = _guess_mime_type(obj)
        checksum_value = _clean(getattr(obj, "checksum", None))
        checksum_algorithm_attr = _clean(getattr(obj, "checksum_algorithm", None))
        provenance = _clean(getattr(obj, "checksum_provenance", None))
        fixity = parse_fixity(checksum_value)
        if checksum_algorithm_attr:
            normalized_algorithm = checksum_algorithm_attr.lower().replace('-', '')
            fixity = FixityInfo(
                normalized_algorithm,
                fixity.digest or checksum_value,
                provenance,
            )
        else:
            fixity = fixity.with_provenance(provenance)

        storage_status = _clean(getattr(obj, "storage_status", None))
        object_uri = _clean(getattr(obj, "uri", None))
        resource_identifier = getattr(obj, "resource_id", None)
        resource_id = None
        if resource_identifier not in (None, ""):
            resource_id = str(resource_identifier)

        rosetta_candidates: Tuple[str, ...] = ()
        rosetta_path: Optional[str] = None

        normalized_code = (institution_code or "").strip().lower()
        is_s3_org = bool(normalized_code and normalized_code in self._s3_orgs)
        is_rosetta_org = bool(normalized_code and normalized_code in self._rosetta_orgs and not is_s3_org)
        curated_override = bool(getattr(obj, "_from_curated_media_link", False))
        curated_rosetta_path: Optional[str] = None
        if curated_override and is_rosetta_org:
            curated_rosetta_path = _clean(getattr(obj, "_curated_rosetta_path_override", None))
            if not curated_rosetta_path:
                curated_rosetta_path = original_path or storage_key
            prefix = self._rosetta_curated_prefixes.get(normalized_code)
            if curated_rosetta_path and prefix:
                curated_rosetta_path = _apply_rosetta_prefix(prefix, curated_rosetta_path)

        from_s3_inventory = getattr(obj, "_from_s3_file_object", False)
        bypass_dump_fixity = getattr(obj, "_bypass_dump_fixity", False)
        needs_fixity_lookup = bool(
            is_s3_org
            and not from_s3_inventory
            and not storage_key
            and not bypass_dump_fixity
        )

        # For S3 orgs (FUK, DET, RSH): check if file exists in dump/fixity index
        if needs_fixity_lookup:
            from arkumu.projects.services.dump_fixity_index import find_fixity

            candidates = [original_path, storage_key, access_url, file_name]
            fixity_record = find_fixity(institution_code, candidates)

            if not fixity_record:
                logger.info(
                    "OAI digital object skipped: no dump match (org=%s path=%s storage_key=%s)",
                    institution_code,
                    original_path,
                    storage_key,
                )
                return None

            # Update storage_key and fixity info from dump index
            if fixity_record.storage_key and not storage_key:
                storage_key = fixity_record.storage_key
            if fixity_record.status and not storage_status:
                storage_status = fixity_record.status
            if fixity_record.checksum_or_etag and not fixity.digest:
                fixity = parse_fixity(fixity_record.checksum_or_etag)

        if curated_rosetta_path:
            rosetta_candidates = (curated_rosetta_path,)
            rosetta_path = curated_rosetta_path
        elif is_rosetta_org:
            resolved = self._path_resolver(
                institution_code,
                path=original_path or storage_key,
                file_name=file_name,
            )
            if resolved:
                rosetta_candidates = tuple(resolved)
                rosetta_path = resolved[0]
            else:
                logger.info(
                    "OAI Rosetta object skipped: no candidate path (org=%s path=%s storage_key=%s file=%s)",
                    institution_code,
                    original_path,
                    storage_key,
                    file_name,
                )
                return None

        # Build Rosetta path for orgs with configured base paths (both S3 and non-S3)
        # S3 orgs need this for METS FLocat since hbz harvests files from S3 and stores locally
        if (
            not rosetta_path
            and normalized_code
            and normalized_code in self._s3_rosetta_bases
            and storage_key
        ):
            base = self._s3_rosetta_bases[normalized_code]
            # Use just filename for S3 orgs (flat storage), full key for others
            path_component = file_name if is_s3_org and file_name else storage_key.lstrip('/')
            candidate = f"{base}/{path_component}"
            rosetta_path = candidate
            rosetta_candidates = (candidate,)

        if not rosetta_path and original_path and original_path.startswith("/rosetta/"):
            rosetta_candidates = (original_path,)
            rosetta_path = original_path

        is_s3_org = bool(normalized_code and normalized_code in self._s3_orgs)
        source = "s3" if is_s3_org else ("rosetta" if rosetta_path else "unknown")

        size_bytes = getattr(obj, "size_bytes", None)
        if isinstance(size_bytes, str) and size_bytes.isdigit():
            size_bytes = int(size_bytes)

        uuid_value = _clean(getattr(obj, "uuid", None))
        genesis_type = _clean(getattr(obj, "genesis_type", None))
        media_type = _clean(getattr(obj, "media_type", None))
        significant_de = _clean(getattr(obj, "significant_properties_de", None))
        significant_en = _clean(getattr(obj, "significant_properties_en", None))
        license_info = getattr(obj, "license", None)
        if license_info and not isinstance(license_info, ProjectDigitalObjectLicense):
            try:
                license_info = ProjectDigitalObjectLicense(**license_info)  # type: ignore[call-arg]
            except TypeError:
                license_info = None

        download_href = None
        if is_s3_org and storage_key:
            download_href = self._build_s3_download_href(storage_key, institution_code)

        normalized_obj = NormalizedDigitalObject(
            uri=object_uri,
            original_path=original_path,
            storage_key=storage_key,
            rosetta_path=rosetta_path,
            rosetta_candidates=rosetta_candidates,
            file_name=file_name,
            content_type=content_type,
            size_bytes=size_bytes,
            checksum=fixity.digest,
            checksum_algorithm=fixity.algorithm,
            checksum_provenance=fixity.provenance or (source if source in {"rosetta", "s3"} else None),
            access_url=access_url,
            storage_status=storage_status,
            source=source,
            uuid=uuid_value,
            genesis_type=genesis_type,
            media_type=media_type,
            significant_properties_de=significant_de,
            significant_properties_en=significant_en,
            license=license_info,
            resource_id=resource_id,
            download_href=download_href,
        )
        if getattr(obj, "_from_s3_file_object", False):
            object.__setattr__(normalized_obj, "_from_s3_file_object", True)
            if storage_key and storage_key.lower().startswith("metadata/"):
                object.__setattr__(normalized_obj, "_allow_metadata_exports", True)
        return normalized_obj

    def _build_s3_download_href(
        self,
        storage_key: Optional[str],
        institution_code: Optional[str],
    ) -> Optional[str]:
        if not storage_key:
            return None
        host = (
            os.environ.get("AWS_S3_BROWSER_ENDPOINT_URL", "")
            or os.environ.get("AWS_S3_ENDPOINT_URL", "")
        ).strip()
        if not host:
            host = "http://localhost:9020"
        base = host.rstrip("/")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"

        # Check for S3 namespace (used by Dell EMC systems like digikunst)
        # When USE_MINIO=false, use DJANGO_AWS_STORAGE_BUCKET_NAME as namespace
        use_minio = os.environ.get("USE_MINIO", "true").lower() in ("true", "1", "yes")
        s3_namespace = ""
        if not use_minio:
            s3_namespace = os.environ.get("DJANGO_AWS_STORAGE_BUCKET_NAME", "").strip()

        token = storage_key.strip()
        if token.startswith("s3://"):
            token = token[5:]
        token = token.lstrip("/")
        if not token:
            return None

        normalized_code = (institution_code or "").strip().lower()
        bucket = normalized_code or None
        if bucket:
            prefix = f"{bucket}/"
            if token.lower().startswith(prefix):
                token = token[len(prefix):]
        else:
            candidate_bucket, sep, remainder = token.partition('/')
            if not sep:
                return None
            bucket = candidate_bucket.strip()
            token = remainder

        token = token.lstrip('/')
        if not bucket or not token:
            return None

        from urllib.parse import quote

        escaped_key = quote(token, safe="/-_.~")
        if s3_namespace:
            return f"{base}/{s3_namespace}/{bucket}/{escaped_key}"
        return f"{base}/{bucket}/{escaped_key}"

    def _resolve_curated_selection(
        self,
        record: ProjectRecord,
    ) -> Optional[CuratedMediaSelection]:
        subject_id = getattr(record, "subject_id", None)
        if not subject_id:
            return None

        try:
            project_uuid = UUID(str(subject_id))
        except (TypeError, ValueError):
            return None

        links = list(
            OAIProjectMediaLink.objects.for_project(project_uuid)
            .ordered()
            .select_related("digital_object")
            .annotate(
                other_project_refs=Count(
                    "digital_object__oai_media_references",
                    filter=~Q(digital_object__oai_media_references__project_id=project_uuid),
                    distinct=True,
                )
            )
        )
        if not links:
            return None

        record_objects = getattr(record, "digital_objects", []) or []
        record_resource_ids: set[str] = set()
        graph_only_candidates: List[str] = []
        for obj in record_objects:
            rid = getattr(obj, "resource_id", None)
            uri = getattr(obj, "uri", None)
            if rid:
                record_resource_ids.add(str(rid))
            elif uri:
                graph_only_candidates.append(uri)

        ordered_resource_ids = tuple(str(link.digital_object_id) for link in links)
        ordered_object_uris = tuple(
            link.digital_object.uri
            for link in links
            if getattr(link.digital_object, "uri", None)
        )
        resource_uri_pairs: List[Tuple[str, Optional[str]]] = []

        curated_missing: List[str] = []
        label_overrides_by_id: Dict[str, str] = {}
        label_overrides_by_uri: Dict[str, str] = {}
        warnings: List[CuratedLinkWarning] = []

        for link in links:
            resource_key = str(link.digital_object_id)
            digital_uri = getattr(link.digital_object, "uri", None)
            resource_uri_pairs.append((resource_key, digital_uri))
            if link.label_override:
                label_overrides_by_id[resource_key] = link.label_override
                if digital_uri:
                    label_overrides_by_uri[digital_uri] = link.label_override
            if resource_key not in record_resource_ids:
                curated_missing.append(digital_uri or resource_key)
            if link.is_stale:
                warnings.append(
                    CuratedLinkWarning(
                        code="curated_stale",
                        digital_object_uri=digital_uri,
                        message="Curated link marked stale; canonical graph no longer references this object.",
                    )
                )
            other_refs = getattr(link, "other_project_refs", 0)
            if other_refs:
                warnings.append(
                    CuratedLinkWarning(
                        code="digital_object_multi_project",
                        digital_object_uri=digital_uri,
                        message="Digital object approved for multiple projects.",
                    )
                )

        curated_set = set(ordered_resource_ids)
        uncategorized_graph_uris: List[str] = list(dict.fromkeys(graph_only_candidates))
        for obj in record_objects:
            rid = getattr(obj, "resource_id", None)
            uri = getattr(obj, "uri", None)
            if not rid or not uri:
                continue
            if str(rid) not in curated_set:
                uncategorized_graph_uris.append(uri)

        if curated_missing:
            warnings.append(
                CuratedLinkWarning(
                    code="curated_missing_in_graph",
                    message=f"{len(curated_missing)} curated object(s) missing from canonical graph.",
                )
            )
        if uncategorized_graph_uris:
            warnings.append(
                CuratedLinkWarning(
                    code="graph_objects_uncurated",
                    message=f"{len(uncategorized_graph_uris)} canonical object(s) lack curated approvals.",
                )
            )

        ordered_uri_tuple = tuple(uri for uri in ordered_object_uris if uri)
        curated_missing_tuple = tuple(dict.fromkeys(curated_missing))
        uncategorized_tuple = tuple(dict.fromkeys(uncategorized_graph_uris))

        return CuratedMediaSelection(
            ordered_resource_ids=ordered_resource_ids,
            ordered_object_uris=ordered_uri_tuple,
            resource_uri_pairs=tuple(resource_uri_pairs),
            label_overrides_by_id=label_overrides_by_id,
            label_overrides_by_uri=label_overrides_by_uri,
            curated_missing_uris=curated_missing_tuple,
            graph_only_uris=uncategorized_tuple,
            warnings=tuple(warnings),
        )

    def _expand_dcp_folder_if_needed(
        self,
        obj: ProjectDigitalObject,
        institution_code: Optional[str],
    ) -> Optional[List[ProjectDigitalObject]]:
        """
        Check if digital object represents a DCP folder and expand to all files.
        Returns None if not a DCP folder, or list of ProjectDigitalObject for all files in folder.
        """
        # Only check for KHM institution
        if (institution_code or "").strip().lower() != "khm":
            return None

        obj_uri = getattr(obj, "uri", None)
        if not obj_uri:
            return None

        # Use cached DCP folder mapping (loaded once, avoids N+1 queries)
        _load_dcp_folder_cache()
        folder_name = _DCP_FOLDER_CACHE.get(obj_uri)

        if not folder_name:
            return None

        logger.info("Looking for DCP folder %s (from cache, uri: %s)", folder_name, obj_uri)

        # Use the DCP index service to resolve bundle members as relative paths
        lookup = dcp_index.get_bundle_members("khm", folder_name)
        if not lookup.relative_file_paths:
            logger.info("No files found in DCP folder %s via index", folder_name)
            return None

        rosetta_root = getattr(settings, "OAI_EXTERNAL_ROSETTA_ROOTS", {}).get("khm") or ""
        rosetta_root = str(rosetta_root).rstrip("/")

        expanded_objects: List[ProjectDigitalObject] = []
        for rel_path in lookup.relative_file_paths:
            rel_path_clean = str(rel_path).lstrip("/")
            if not rel_path_clean:
                continue
            abs_path = f"{rosetta_root}/{rel_path_clean}" if rosetta_root else rel_path_clean

            new_obj = ProjectDigitalObject(
                path=abs_path,
                uri=obj.uri,
            )
            new_obj.resource_id = getattr(obj, "resource_id", None)

            # Copy other relevant attributes from the original object
            for attr in [
                "content_type",
                "size_bytes",
                "checksum",
                "checksum_algorithm",
                "checksum_provenance",
                "access_url",
                "storage_status",
                "created_at",
                "updated_at",
                "license",
                "uuid",
                "genesis_type",
                "media_type",
                "significant_properties_de",
                "significant_properties_en",
            ]:
                if hasattr(obj, attr):
                    value = getattr(obj, attr)
                    if value is not None:
                        setattr(new_obj, attr, value)

            expanded_objects.append(new_obj)

        logger.info("Expanded DCP folder %s to %d files using index", folder_name, len(expanded_objects))
        return expanded_objects

    @staticmethod
    def _is_harvestable(obj: NormalizedDigitalObject) -> bool:
        """Return True only for objects that meet dissemination rules."""

        if obj.source == "rosetta":
            return bool(obj.rosetta_path)

        if obj.source == "s3":
            if not obj.storage_key:
                return False
            key_normalized = obj.storage_key.lower()
            if key_normalized.startswith("metadata/") and not getattr(obj, "_allow_metadata_exports", False):
                return False
            status = _status_token(obj.storage_status)
            if status is None:
                return True
            return status in HARVESTABLE_STORAGE_STATUSES

        if obj.storage_key or obj.rosetta_path:
            return True

        return False
