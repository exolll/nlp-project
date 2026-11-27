"""Deterministic requirement-quality checks. Warnings never delete items or evidence."""
import hashlib
import json
import re
import unicodedata

from app.schemas.requirements import BUSINESS_FIELDS

VERSION = 'requirement-quality-v2'
# project_topic / project_description / project_type / domain are summaries by definition:
# demanding a verbatim slice of the source would block every normal extraction.
VERBATIM_FIELDS = frozenset(BUSINESS_FIELDS[4:])
_TRAILING = '.,;:!?。；：！？、'
_SPACE = re.compile(r'\s+')


def canonical(text):
    """NFKC + casefold + whitespace collapse + trailing punctuation, never synonym rewrite."""
    value = unicodedata.normalize('NFKC', text).casefold()
    value = _SPACE.sub(' ', value).strip()
    return value.rstrip(_TRAILING).strip()


def revision_hash(revision):
    data = json.dumps(revision.model_dump(mode='json'), ensure_ascii=False,
                      sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def _item_value(item):
    return canonical(item.text)


def quality_warnings(revision):
    """Return duplicate warnings plus the revision hash they are bound to."""
    items = list(revision.requirement_items)
    topics = [item for item in items if item.field == 'project_topic']
    topic_keys = {_item_value(item) for item in topics}
    warnings, seen = [], set()

    def warn(code, item, message):
        key = (code, item.requirement_item_id)
        if key in seen:
            return
        seen.add(key)
        warnings.append({'code': code, 'revision_hash': revision_hash(revision),
                         'field': item.field, 'requirement_item_id': item.requirement_item_id,
                         'evidence_ids': list(item.evidence_ids), 'message': message})

    for item in items:
        if item.field == 'project_topic':
            continue
        if _item_value(item) in topic_keys:
            warn('DUPLICATE_PROJECT_GOAL', item,
                 '该条与项目总体目标文字相同；请补充具体功能或确认删除。')
    groups = {}
    for item in items:
        groups.setdefault(_item_value(item), []).append(item)
    for key, members in groups.items():
        if not key or len(members) < 2:
            continue
        fields = {item.field for item in members}
        if len(fields) < 2:
            continue
        for item in members:
            others = sorted({other.field for other in members if other.field != item.field})
            if others:
                warn('DUPLICATE_REQUIREMENT', item,
                     '该条与另一字段的需求文字完全相同：' + '、'.join(others) + '。')
    return {'version': VERSION, 'revision_hash': revision_hash(revision), 'warnings': warnings}


def evidence_mismatches(revision):
    """Structural checks for every item; verbatim containment only for requirement fields.

    Two deliberate exemptions keep the gate about evidence, not about model style:
    - item.evidence_ids is the authoritative binding, so a null reverse link
      (evidence.requirement_item_id is None) is bound, not a mismatch;
    - informational fields (project_topic/description/type/domain) are summaries, so a
      paraphrase is expected and is not reported as not_verbatim.
    """
    source = list(revision.requirements.source_evidence)
    counts = {}
    for evidence in source:
        counts[evidence.evidence_id] = counts.get(evidence.evidence_id, 0) + 1
    duplicates = sorted(evidence_id for evidence_id, count in counts.items() if count > 1)
    by_id = {evidence.evidence_id: evidence for evidence in source if counts[evidence.evidence_id] == 1}
    mismatches = []
    if duplicates:
        mismatches.append({'requirement_item_id': None, 'field': None,
                           'reason': 'duplicate_evidence_id', 'evidence_ids': duplicates})
    for item in revision.requirement_items:
        linked = [by_id[evidence_id] for evidence_id in item.evidence_ids if evidence_id in by_id]
        candidates = [evidence for evidence in linked
                      if evidence.requirement_item_id in (None, item.requirement_item_id)]
        text = canonical(item.text)
        if not text:
            mismatches.append({'requirement_item_id': item.requirement_item_id, 'field': item.field,
                               'reason': 'empty_text', 'evidence_ids': list(item.evidence_ids)})
            continue
        if not item.evidence_ids:
            reason = 'empty_evidence_ids'
        elif any(evidence_id in duplicates for evidence_id in item.evidence_ids):
            reason = 'duplicate_evidence_id'
        elif not linked:
            reason = 'evidence_missing'
        elif not candidates:
            reason = 'evidence_not_bound'
        elif item.field not in VERBATIM_FIELDS:
            continue
        elif not any(text in canonical(evidence.original_text) for evidence in candidates):
            reason = 'not_verbatim'
        else:
            continue
        mismatches.append({'requirement_item_id': item.requirement_item_id, 'field': item.field,
                           'reason': reason, 'evidence_ids': list(item.evidence_ids)})
    return mismatches


def validate_quality(revision):
    """Warnings are non-blocking; a non-verbatim requirement item blocks the draft."""
    warnings = quality_warnings(revision)
    mismatches = evidence_mismatches(revision)
    return {'version': VERSION, 'revision_hash': warnings['revision_hash'],
            'warnings': warnings['warnings'], 'evidence_mismatches': mismatches,
            'ok': not mismatches}
