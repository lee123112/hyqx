"""Portable, standard-library-only fields for the public predict response."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re


def extract_sample_limit(overrides: dict) -> int | None:
    """Consume the response-only limit, leaving processor overrides untouched."""
    value = overrides.pop("sampleLimit", 20)
    if isinstance(value, bool):
        raise ValueError("sampleLimit must be an integer")
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?[0-9]+", value):
            raise ValueError("sampleLimit must be an integer")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError("sampleLimit must be an integer")
    return value if value > 0 else None


def _iter_records(file_name, state=None):
    """Read JSONL incrementally; unavailable response artifacts are optional."""
    state = state if state is not None else {}
    state.update(complete=False, malformed=False, rows=0, objects=0)
    if not file_name:
        return
    try:
        path = Path(file_name)
        suffix = path.suffix.lower()
        if suffix not in {".jsonl", ".json"}:
            return
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            if suffix == ".json":
                try:
                    payload = json.load(stream)
                except (ValueError, RecursionError):
                    state["malformed"] = True
                    return
                rows = payload if isinstance(payload, list) else [payload]
                for row in rows:
                    state["rows"] += 1
                    if isinstance(row, dict):
                        state["objects"] += 1
                        yield row
                state["complete"] = True
                return
            for line in stream:
                if not line.strip():
                    continue
                state["rows"] += 1
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    state["malformed"] = True
                    continue
                if isinstance(row, dict):
                    state["objects"] += 1
                    yield row
                else:
                    state["malformed"] = True
        state["complete"] = True
    except (OSError, TypeError, ValueError):
        return


def _is_pair(record):
    return (isinstance(record.get("source"), str)
            and isinstance(record.get("target"), str))


def _pairs_in(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if _is_pair(item):
                yield item
            else:
                pending.extend(reversed(list(item.values())))
        elif isinstance(item, list):
            pending.extend(reversed(item))


def _reasons(record):
    source = record
    while isinstance(source, dict):
        reasons = []
        for name in ("reasons", "reason"):
            value = source.get(name)
            values = value if isinstance(value, list) else [value]
            for reason in values:
                if reason is not None:
                    text = str(reason)
                    if text and text not in reasons:
                        reasons.append(text)
        if reasons:
            return reasons
        source = source.get("record")
    return []


def _identities(record):
    identities = set()
    records = [record]
    if isinstance(record.get("record"), dict):
        records.append(record["record"])
    for item in records:
        for key in ("id", "record_id"):
            value = item.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value):
                identities.add(str(value))
    return identities


def _rejected_sample(record, reasons):
    """Flatten evidence, preserving a nested business ID over an audit row ID."""
    nested = record.get("record")
    sample = dict(nested) if isinstance(nested, dict) else {}
    if "decision" in sample:
        sample["previous_decision"] = sample.pop("decision")
    for key, value in record.items():
        if key != "record" and (value is not None or key not in sample):
            sample[key] = value
    nested_id = next((nested[key] for key in ("id", "pair_id", "record_id")
                      if isinstance(nested, dict) and nested.get(key) is not None), None)
    if nested_id is not None:
        if record.get("id") is not None and record["id"] != nested_id:
            sample.setdefault("audit_id", record["id"])
        sample["id"] = nested_id
    sample["reasons"] = reasons
    sample.setdefault("source", None)
    sample.setdefault("target", None)
    if sample.get("decision") not in ("retained", "rejected"):
        sample["decision"] = "unknown"
    return sample


def _exception_sources(result):
    seen = set()
    for key, default_reason in (("rejectFile", None), ("duplicateFile", "duplicate_pair")):
        value = result.get(key)
        if not value:
            continue
        try:
            path = Path(value).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if path not in seen:
            seen.add(path)
            yield path, default_reason


def _count(value):
    """Accept integral counters while excluding booleans and lossy coercions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and value.is_integer():
        return max(int(value), 0)
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        return max(int(value), 0)
    return None


def _first_count(statistics, keys):
    for key in keys:
        value = _count(statistics.get(key))
        if value is not None:
            return value
    return None


def _sum_counts(statistics, keys):
    values = [_count(statistics.get(key)) for key in keys]
    return sum(value or 0 for value in values) if any(value is not None for value in values) else None


_OPERATOR_NAMES = (
    "validate_lang_pair", "clean_encoding_control", "clean_blank_html",
    "clean_header_footer_timestamp", "length_filter", "hash_dedup",
    "desensitize_sensitive_info", "align_document_paragraph", "align_sentence",
    "align_clause_phrase", "domain_detection_filter", "score_alignment_confidence",
    "grade_alignment_quality", "semantic_equivalence_check", "fluency_check",
    "number_entity_consistency", "build_metadata_package", "quality_report",
    "manual_inspection", "back_translation", "synonym_substitution",
    "syntactic_transformation", "cross_lingual_synthesis",
)


def _operator_number(operator_id):
    name = str(operator_id or "").removeprefix("PRLt_")
    if name in _OPERATOR_NAMES:
        return str(_OPERATOR_NAMES.index(name) + 1).zfill(2)
    return name.zfill(2)


# These processors only write physically excluded instances to their exception
# artifacts. Warning/grade/kept branches go to the main output instead.
_ALL_PHYSICAL = {"01", "02", "03", "04", "05", "06", "07",
                 "13", "14", "17", "20", "21", "22", "23"}
_MIXED_AUDIT = {"08", "11", "12", "15", "16"}
_CONTEXT_FIELDS = ("document_id", "source_lang", "target_lang", "paragraph_id",
                   "source_ref", "target_ref")


def _diagnostic(sample, operator):
    return operator == "18" or (operator == "19" and sample.get("record_type") != "input")


def _context(record):
    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    context = {}
    for key in _CONTEXT_FIELDS:
        value = record.get(key)
        if value is None:
            value = metadata.get(key)
        if value is not None:
            context[key] = value
    return context


def _complete_instance(sample):
    return (bool(_identities(sample)) and _is_pair(sample)
            and bool(sample["source"].strip()) and bool(sample["target"].strip()))


def _retention_status(sample, operator):
    """Recognize only audit branches whose processor always keeps the record."""
    reasons = sample["reasons"]
    if operator == "12":
        return "conflict_multi_candidate" in reasons and "low_confidence" not in reasons
    if operator == "15":
        states = sample.get("status_values")
        states = states if isinstance(states, dict) else {}
        status = sample.get("fluency_status") or states.get("fluency_status")
        return status == "waiting_calibration" or "waiting_calibration" in reasons
    if operator == "16":
        return (sample.get("entity_consistency_status") in ("inconsistent", "waiting_manual")
                and not any(reason.startswith("policy:") for reason in reasons))
    return False


def _artifact_counts(file_name):
    state = {}
    pairs = 0
    for row in _iter_records(file_name, state):
        pairs += sum(1 for _ in _pairs_in(row))
    return state, pairs


def normalize_statistics(result: dict, operator_id=None, input_file=None,
                         audit_records: int = 0, retained_events=None) -> dict:
    """Normalize physical rejection counters without input-minus-output guesses.

    Alignment counters retain their processor units: API08 consumes plans and
    rejects paragraph events, API09 consumes paragraphs and rejects sentence
    events, and API10 consumes pairs and rejects clause events. Invalid input
    events may also occur in those exception counters. Original counters remain
    available under ``raw_statistics`` to expose the different units.
    """
    original = result.get("statistics")
    statistics = dict(original) if isinstance(original, dict) else {}
    normalized = isinstance(statistics.get("raw_statistics"), dict)
    raw = dict(statistics["raw_statistics"]) if normalized else dict(statistics)
    operator = _operator_number(operator_id)
    inputs = _first_count(statistics, ("input_records", "total", "input_pairs", "plan_loaded"))
    outputs = _first_count(statistics, (
        "output_records", "passed", "passed_records", "admitted_records",
        "pairs_written", "pairs", "aligned_clauses", "scored", "graded",
        "delivery_records", "queue_written_count"))
    rejects = _first_count(statistics, ("rejected_records", "rejected", "rejected_clauses"))
    if not normalized:
        if operator == "06":
            rejects = _sum_counts(raw, ("rejected", "duplicates"))
        elif operator == "08":
            inputs = _first_count(raw, ("input_records", "plan_loaded"))
            outputs = _first_count(raw, ("output_records", "pairs_written", "pairs"))
            if rejects is not None:
                rejects = max(rejects - (_first_count(raw, ("multi_candidate",)) or 0), 0)
        elif operator == "09" and "marked_low_confidence" in raw:
            rejects = max((rejects or 0) - (_count(raw["marked_low_confidence"]) or 0), 0)
        elif operator == "11":
            rejects = _first_count(raw, ("physical_reject_records",))
            if rejects is None:
                rejects = _sum_counts(raw, ("schema_error", "out_of_scope", "moved_to_rejects"))
        elif operator == "12":
            rejects = _sum_counts(raw, ("hard_rejected", "moved_to_rejects"))
        elif operator == "14" and "kept_rejected" in raw:
            rejects = max((rejects or 0) - (_count(raw["kept_rejected"]) or 0), 0)
        elif operator == "15":
            rejects = _first_count(raw, ("physical_reject_records",))
            if rejects is None:
                rejects = _sum_counts(raw, ("hard_rejected_records", "moved_to_rejects"))
        elif operator == "16":
            rejects = _sum_counts(raw, ("schema_error", "moved_to_rejects"))
        elif operator == "17":
            outputs = _first_count(raw, ("output_records", "delivery_records", "accepted_records"))
        elif operator == "18":
            state, _ = _artifact_counts(result.get("outputFile"))
            outputs = int(state["complete"] and not state["malformed"] and state["objects"] > 0)
            # Report diagnostics also include metrics and side inputs. They do
            # not establish how many corpus records were physically rejected.
            physical = _first_count(raw, ("physical_reject_records", "input_rejected_count"))
            rejects = physical if physical is not None else 0
            statistics["rejected_records_known"] = physical is not None
        elif operator == "19":
            outputs = _first_count(raw, ("output_records", "queue_written_count"))
            rejects = _first_count(raw, ("input_rejected_count",))
    if not normalized and operator in {"08", "10"}:
        if retained_events is None:
            kept_keys = _alignment_outputs(result, operator, {}) if operator == "10" else {}
            retained_events = 0
            complete = True
            for path, _ in _exception_sources(result):
                state = {}
                for row in _iter_records(path, state):
                    retained_events += int(
                        (operator == "08" and _paragraph_keep(row))
                        or (operator == "10" and _consume_alignment_keep(row, operator, kept_keys)))
                complete = complete and state["complete"] and not state["malformed"]
            if not complete:
                statistics["rejected_records_known"] = False
        rejects = max((rejects or 0) - retained_events, 0)
    if inputs is None:
        state, _ = _artifact_counts(input_file)
        inputs = state["rows"]
    if outputs is None:
        _, outputs = _artifact_counts(result.get("outputFile"))
    if rejects is None:
        rejects = _first_count(raw, ("physical_reject_records",)) or 0
    statistics.update(input_records=inputs, output_records=outputs, rejected_records=rejects,
                      audit_records=max(_count(audit_records) or 0,
                                        _count(statistics.get("audit_records")) or 0),
                      raw_statistics=raw)
    return statistics


def _paragraph_keep(row):
    # Multi-candidate audit events already have their dedicated counter.
    return ("conflict_multi_candidate" not in _reasons(row)
            and ("已按 mark_and_keep 写入通过输出" in str(row.get("detail", ""))
                 or (row.get("decision") == "retained" and "low_confidence" in _reasons(row))))


def _alignment_key(row, operator, output=False):
    """Match generated alignment instances by parent, complete refs and text."""
    metadata = row.get("metadata") if output else row
    if not isinstance(metadata, dict):
        return None
    parent_field = "paragraph_id" if operator == "09" else ("parent_pair_id" if output else "pair_id")
    parent = metadata.get(parent_field)
    source_refs, target_refs = metadata.get("source_refs"), metadata.get("target_refs")
    if not parent or not source_refs or not target_refs or not _is_pair(row):
        return None
    if output and (str(metadata.get("flagged_by")) != operator or metadata.get("review_status") != "needs_review"):
        return None
    reason = metadata.get("low_confidence_reason")
    if output and operator == "10" and reason == "duplicate_unit":
        # The parent repetition guard overwrites the displayed reason, but only
        # a retained fragment/cross-position event carries its original gate.
        gate = metadata.get("gate")
        if not isinstance(gate, dict):
            return None
        if {"source_chars", "target_chars", "min_chars"} <= gate.keys():
            reason = "fragment_too_short"
        elif {"source_start", "target_start", "gap", "max_gap"} <= gate.keys():
            reason = "cross_position_mismatch"
        else:
            return None
    payload = [parent, source_refs, target_refs, row["source"], row["target"], reason]
    if operator == "09":
        payload.append(metadata.get("paragraph_line") if output else row.get("line"))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).digest()


_ALIGNMENT_KEEP_REASONS = {
    "09": {"low_confidence", "conflict_multi_candidate", "source_residual_note",
           "target_residual_note", "boundary_mismatch", "unclosed_quote"},
    "10": {"fragment_too_short", "cross_position_mismatch"},
}


def _alignment_outputs(result, operator, state):
    counts = {}
    for record in _iter_records(result.get("outputFile"), state):
        for row in _pairs_in(record):
            key = _alignment_key(row, operator, output=True)
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _consume_alignment_keep(row, operator, counts):
    reasons = _reasons(row)
    if not reasons or not set(reasons).issubset(_ALIGNMENT_KEEP_REASONS[operator]):
        return False
    key = _alignment_key(row, operator)
    if not counts.get(key):
        return False
    counts[key] -= 1
    return True


def build_predict_fields(result: dict, sample_limit: int | None = 20,
                         operator_id: str | None = None, input_file=None) -> dict:
    """Return response samples without changing processor results or artifacts."""
    limit = sample_limit if sample_limit is not None and sample_limit > 0 else None
    operator = _operator_number(operator_id)
    # Only compact fingerprints of flagged alignment outputs are indexed. Audit
    # IDs differ from generated business IDs in these two operators.
    alignment_output_state = {}
    alignment_kept = (_alignment_outputs(result, operator, alignment_output_state)
                      if operator in {"09", "10"} else {})
    retained_events = 0
    rejected = []
    reason_counts = {}
    audit_records = 0
    valid_exceptions = 0
    exceptions_complete = True
    for path, default_reason in _exception_sources(result):
        reject_state = {}
        for row in _iter_records(path, reject_state):
            valid_exceptions += 1
            reasons = _reasons(row) or ([default_reason] if default_reason else [])
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            explicit_keep = (row.get("kept_in_output") is True
                             or (operator == "08" and _paragraph_keep(row))
                             or (operator in {"09", "10"} and _consume_alignment_keep(row, operator, alignment_kept)))
            retained_events += int(explicit_keep)
            if limit is None or len(rejected) < limit:
                sample = _rejected_sample(row, reasons)
                if explicit_keep and sample["decision"] == "unknown":
                    sample["decision"] = "retained"
                # Duplicate instances can share both ID and text with the
                # retained instance. Their physical branch overrides ID-only
                # matching, including API06 duplicates merged into rejectFile.
                if sample["decision"] == "unknown" and (
                        default_reason == "duplicate_pair" or operator in _ALL_PHYSICAL
                        or (operator in {"09", "10"} and (
                            any(reason.startswith(("invalid_", "missing_", "schema_error", "model_error", "empty_", "split_failed", "unaligned_")) for reason in reasons)
                            or (operator == "10" and set(reasons) & {"low_confidence", "conflict_multi_candidate", "no_evidence"})
                            or (_alignment_key(row, operator) is not None
                                and alignment_output_state.get("complete")
                                and not alignment_output_state.get("malformed"))))
                        or (operator == "19" and sample.get("record_type") == "input")
                        or (operator == "11" and sample.get("domain_status") == "out_of_scope")
                        or (operator == "16" and any(reason.startswith("policy:") for reason in reasons))):
                    sample["decision"] = "rejected"
                rejected.append(sample)
        audit_records += reject_state["rows"]
        exceptions_complete = (exceptions_complete and reject_state["complete"]
                               and not reject_state["malformed"])

    statistics = normalize_statistics(result, operator_id, input_file, audit_records,
                                      retained_events=retained_events)
    physical_rejects = statistics["rejected_records"]
    if (operator in _MIXED_AUDIT and exceptions_complete and valid_exceptions > 0
            and physical_rejects == valid_exceptions):
        for row in rejected:
            if row["decision"] == "unknown":
                row["decision"] = "rejected"

    # Only identities needed by the returned exception sample are retained.
    # A bounded response therefore never builds an index of the full corpus.
    by_identity = {}
    for index, row in enumerate(rejected):
        if (row["decision"] == "unknown" and operator in _MIXED_AUDIT
                and not _diagnostic(row, operator) and _complete_instance(row)):
            for identity in _identities(row):
                by_identity.setdefault(identity, set()).add(index)
    matched = set()
    conflicting_instance = set()
    sample = []
    output_count = 0
    output_state = {}
    for record in _iter_records(result.get("outputFile"), output_state):
        for row in _pairs_in(record):
            output_count += 1
            if limit is None or len(sample) < limit:
                sample.append(row)
            candidates = set()
            for identity in _identities(row):
                candidates.update(by_identity.get(identity, ()))
            for index in candidates:
                candidate = rejected[index]
                if _context(candidate) != _context(row):
                    conflicting_instance.add(index)
                elif candidate["source"] == row["source"] and candidate["target"] == row["target"]:
                    matched.add(index)
                else:
                    conflicting_instance.add(index)
    for index, row in enumerate(rejected):
        if (row["decision"] != "unknown" or _diagnostic(row, operator)
                or operator not in _MIXED_AUDIT or not _complete_instance(row)
                or index in conflicting_instance):
            continue
        if index in matched and _retention_status(row, operator):
            row["decision"] = "retained"
        elif (index not in matched and physical_rejects > 0 and output_state["complete"]
              and not output_state["malformed"] and (output_count or not output_state["rows"])):
            row["decision"] = "rejected"
    return {"statistics": statistics, "outputSample": sample,
            "rejectedSample": rejected, "rejectReasons": reason_counts}
