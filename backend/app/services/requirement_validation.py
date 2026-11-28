"""Deterministic structure/provenance checks; source text never grants authority."""
import re
from uuid import uuid4

from app.schemas.requirements import (
    BUSINESS_FIELDS, ModelExtraction, ModelExtractionDraft, RequirementItem, UncertainItem,
)

NEGATION = re.compile(r"禁止|严禁|不得|不能|不可|不要|不准|不(?:使用|允许|采用|支持|依赖)|\b(?:must not|mustn't|shall not|do not|don't|cannot|not allowed|not permitted|without|prohibited|forbidden)\b", re.I)
PREFERENCE = re.compile(
    r'建议|推荐|优先|最好|希望|可选|尽量|可考虑|倾向|'
    r'\b(?:prefer(?:red|ably)?|recommend(?:ed)?|suggest(?:ed)?|optional|ideally|nice to have)\b', re.I)
MANDATORY = re.compile(r'必须|务必|需要|要求|\b(?:must|required|shall|mandatory)\b', re.I)
QUALITY_NEGATION = re.compile(
    r'crash|fail|lose|lost|data loss|frame|timeout|error|'
    r'中断|崩溃|失败|丢失|丢帧|卡顿|超时|数据丢失|泄露|损坏', re.I)
ANY_LOGIC = re.compile(r'任选|任一|其中一个|至少一种|或者|二选一|\b(?:either|any one|one of|or)\b', re.I)
ALL_LOGIC = re.compile(r'全部|同时|两者|均须|都(?:要|必须)|\b(?:both|all of|together)\b', re.I)


def is_prohibition(text):
    """A negated action is prohibited; a negated reliability quality remains required."""
    return bool(NEGATION.search(text)) and not QUALITY_NEGATION.search(text)


def extraction_diagnostic(error):
    from pydantic import ValidationError

    diagnostic = {'version': 1, 'validator': 'unknown'}
    trace = error.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if frame.f_code.co_filename == __file__ and frame.f_code.co_name != 'check':
            diagnostic.update(validator=frame.f_code.co_name, line=trace.tb_lineno)
            for name in ('item', 'evidence', 'entry'):
                field = getattr(frame.f_locals.get(name), 'field', None)
                if isinstance(field, str) and field in BUSINESS_FIELDS:
                    diagnostic['field'] = field
        trace = trace.tb_next
    if isinstance(error, ValidationError):
        diagnostic['schema_errors'] = [
            {'type': entry['type'], 'indices': [part for part in entry['loc'] if isinstance(part, int)]}
            for entry in error.errors(include_url=False, include_context=False, include_input=False)[:10]
        ]
    return diagnostic


def check(condition, message='需求结构或证据不一致。'):
    if not condition:
        raise ValueError(message)


def signature(item):
    # Position is not identity; source metadata is assigned by the server.
    return tuple(getattr(item, name) for name in ('field', 'text', 'modality', 'logic_group_id', 'operator'))


def values(requirements):
    result = {}
    for field in BUSINESS_FIELDS:
        value = getattr(requirements, field)
        if isinstance(value, list):
            for index, text in enumerate(value):
                result[f'{field}[{index}]'] = (field, text)
        elif value is not None:
            result[field] = (field, value)
    return result


def validate_coverage(requirements, items):
    expected = values(requirements)
    seen = set()
    for item in items:
        check(item.value_path not in seen)
        check(expected.get(item.value_path) == (item.field, item.text))
        check(bool(item.text.strip()))
        seen.add(item.value_path)
        if item.field.startswith('required_'):
            check(item.modality in ('required', 'prohibited'))
        elif item.field.startswith('preferred_'):
            check(item.modality == 'preferred')
        elif item.field in BUSINESS_FIELDS[:4]:
            check(item.modality == 'informational')
        if item.operator == 'not_applicable':
            check(item.logic_group_id is None)
        else:
            check(bool(item.logic_group_id and item.logic_group_id.strip()))
        if item.field in BUSINESS_FIELDS[4:] and is_prohibition(item.text):
            check(item.modality == 'prohibited', '否定条件必须保留禁止语义。')
    check(set(expected) == seen, '所有实值字段必须有且仅有一个对应需求项。')
    groups = {}
    for item in items:
        if item.logic_group_id:
            groups.setdefault(item.logic_group_id, []).append(item)
    for group in groups.values():
        check(len({item.operator for item in group}) == 1, '同组操作符必须一致。')


def validate_links(requirements, items):
    ids = [item.requirement_item_id for item in items]
    check(all(ids) and len(set(ids)) == len(ids))
    evidence = {entry.evidence_id: entry for entry in requirements.source_evidence}
    check(len(evidence) == len(requirements.source_evidence))
    linked = set()
    for item in items:
        check(bool(item.evidence_ids) and len(set(item.evidence_ids)) == len(item.evidence_ids))
        for eid in item.evidence_ids:
            check(eid in evidence)
            entry = evidence[eid]
            check(entry.field == item.field and entry.source_kind == item.source_kind)
            check(entry.requirement_item_id in (None, item.requirement_item_id))
            linked.add(eid)
    for uncertainty in requirements.uncertain_information or []:
        check(uncertainty.field in BUSINESS_FIELDS)
        check(set(uncertainty.requirement_item_ids) <= set(ids))
        check(set(uncertainty.evidence_ids) <= set(evidence))
        check(all(next(item for item in items if item.requirement_item_id == rid).field == uncertainty.field
                  for rid in uncertainty.requirement_item_ids))
        check(all(evidence[eid].field == uncertainty.field for eid in uncertainty.evidence_ids))
        linked.update(uncertainty.evidence_ids)
    check(linked == set(evidence), '不得携带无关或孤立证据。')


def _split_at_boundary(quote, left, right):
    """Split only when quote is exactly left suffix + right prefix (ignoring line-wrap whitespace)."""
    left_text, right_text = left.rstrip(), right.lstrip()
    if not left_text or not right_text:
        return None
    pairs = []
    for index in range(1, len(quote)):
        first, second = quote[:index].rstrip(), quote[index:].lstrip()
        if not first or not second:
            continue
        if not left_text.endswith(first) or not right_text.startswith(second):
            continue
        if quote[len(first):len(quote) - len(second)].strip():
            continue
        if (first, second) not in pairs:
            pairs.append((first, second))
    return pairs[0] if len(pairs) == 1 else None


def _draft_segment(quote, segment_id, document):
    segments = document['source_segments']
    by_id = {segment['segment_id']: segment for segment in segments}
    if segment_id is not None:
        check(segment_id in by_id, '引文片段不存在。')
        return by_id[segment_id]
    matches = [segment for segment in segments if quote in segment['original_content']]
    if len(matches) == 1:
        return matches[0]
    check(not matches, '引文在多个片段中出现，必须提供 segment_id。')
    pairs = [segments[position] for position in range(len(segments) - 1)
             if _split_at_boundary(quote, segments[position]['original_content'],
                                   segments[position + 1]['original_content']) is not None]
    check(len(pairs) == 1, '引文不是原始片段的精确引用。')
    return pairs[0]


def _draft_modality(field, text):
    if field in BUSINESS_FIELDS[:4]:
        return 'informational'
    if field.startswith('preferred_'):
        return 'preferred'
    return 'prohibited' if is_prohibition(text) else 'required'


def build_extraction_from_draft(raw, document):
    """Expand the model's item-only draft into the existing Revision-compatible structure."""
    draft = ModelExtractionDraft.model_validate(raw)
    requirements = {field: None for field in BUSINESS_FIELDS}
    requirements.update(missing_information=list(draft.missing_information),
                        uncertain_information=None, source_evidence=[])
    items, uncertainties = [], []
    for index, item in enumerate(draft.requirement_items):
        field, text = item.field, item.text
        if field in BUSINESS_FIELDS[:4]:
            check(requirements[field] is None, '同一单值字段出现多个需求项。')
            requirements[field] = text
            value_path = field
        else:
            if requirements[field] is None:
                requirements[field] = []
            requirements[field].append(text)
            value_path = f'{field}[{len(requirements[field]) - 1}]'
        item_id = f'draft-{index}'
        evidence_ids, item_source_kind = [], None
        for evidence_index, evidence in enumerate(item.evidence):
            segment = _draft_segment(evidence.original_text, evidence.segment_id, document)
            if item_source_kind is None:
                item_source_kind = segment['source_kind']
            evidence_id = f'evidence-{index}-{evidence_index}'
            evidence_ids.append(evidence_id)
            requirements['source_evidence'].append({
                'evidence_id': evidence_id, 'field': field,
                'original_text': evidence.original_text,
                'source_kind': segment['source_kind'],
                'source_location': segment['source_location'],
                'requirement_item_id': item_id, 'segment_id': segment['segment_id'],
            })
        items.append({
            'requirement_item_id': item_id, 'field': field, 'value_path': value_path,
            'text': text, 'modality': _draft_modality(field, text),
            'source_kind': item_source_kind,
            'evidence_ids': evidence_ids, 'logic_group_id': item.logic_group_id,
            'operator': item.operator, 'supersedes_item_id': None,
        })
        if item.uncertainty is not None:
            uncertainties.append({
                'field': field, 'requirement_item_ids': [item_id],
                'reason': item.uncertainty.reason, 'question': item.uncertainty.question,
                'evidence_ids': list(evidence_ids),
            })
    if uncertainties:
        requirements['uncertain_information'] = uncertainties
    return validate_extraction({'requirements': requirements, 'requirement_items': items}, document)


def validate_extraction_output(raw, document, *, allow_legacy=True):
    """Real extraction accepts only the item-only draft; legacy shape is test compatibility."""
    if allow_legacy and isinstance(raw, dict) and 'requirements' in raw:
        return validate_extraction(raw, document)
    return build_extraction_from_draft(raw, document)


def split_cross_segment_evidence(requirements, items, document):
    """Split a model quote that spans adjacent PDF blocks into one evidence row per block."""
    segments = document['source_segments']
    index = {segment['segment_id']: position for position, segment in enumerate(segments)}
    used = {entry.evidence_id for entry in requirements.source_evidence}
    normalized = []
    for evidence in requirements.source_evidence:
        position = index.get(evidence.segment_id)
        declared = segments[position] if position is not None else None
        if (declared is None or evidence.start_offset is not None or evidence.end_offset is not None
                or evidence.original_text in declared['original_content']):
            normalized.append(evidence)
            continue
        candidates = []
        for left_position, right_position in ((position - 1, position), (position, position + 1)):
            if not 0 <= left_position < right_position < len(segments):
                continue
            left, right = segments[left_position], segments[right_position]
            if (left['source_kind'] != evidence.source_kind
                    or right['source_kind'] != evidence.source_kind):
                continue
            pair = _split_at_boundary(evidence.original_text, left['original_content'], right['original_content'])
            if pair is not None:
                candidates.append((left, right, pair[0], pair[1]))
        if len(candidates) != 1:
            normalized.append(evidence)
            continue
        left, right, first, second = candidates[0]
        suffix = 2
        while f'{evidence.evidence_id}#{suffix}' in used:
            suffix += 1
        new_id = f'{evidence.evidence_id}#{suffix}'
        used.add(new_id)
        pieces = [evidence.model_copy(update={'original_text': first, 'source_kind': left['source_kind'],
                                              'source_location': left['source_location'],
                                              'segment_id': left['segment_id'],
                                              'start_offset': None, 'end_offset': None}),
                  evidence.model_copy(update={'evidence_id': new_id, 'original_text': second,
                                              'source_kind': right['source_kind'],
                                              'source_location': right['source_location'],
                                              'segment_id': right['segment_id'],
                                              'start_offset': None, 'end_offset': None})]
        normalized.extend(pieces)
        linked = (evidence.evidence_id, new_id)
        for item in items:
            item.evidence_ids = [part for eid in item.evidence_ids
                                 for part in (linked if eid == evidence.evidence_id else (eid,))]
        for uncertainty in requirements.uncertain_information or []:
            uncertainty.evidence_ids = [part for eid in uncertainty.evidence_ids
                                        for part in (linked if eid == evidence.evidence_id else (eid,))]
    requirements.source_evidence = normalized


def filter_uncertainty_links(requirements, items):
    """Keep only field-consistent references; unknown IDs remain and fail later."""
    item_fields = {item.requirement_item_id: item.field for item in items}
    evidence_fields = {entry.evidence_id: entry.field for entry in requirements.source_evidence}
    for uncertainty in requirements.uncertain_information or []:
        uncertainty.requirement_item_ids = [
            rid for rid in uncertainty.requirement_item_ids
            if rid not in item_fields or item_fields[rid] == uncertainty.field]
        uncertainty.evidence_ids = [
            eid for eid in uncertainty.evidence_ids
            if eid not in evidence_fields or evidence_fields[eid] == uncertainty.field]


def _partition_items_over_values(current, texts):
    """Each field value must explain at least one item, and every item must be explained in order."""
    position = 0
    for value in current:
        cursor, consumed = 0, 0
        while position < len(texts):
            found = value.find(texts[position], cursor)
            if found < 0:
                break
            cursor = found + len(texts[position])
            position += 1
            consumed += 1
        if consumed == 0:
            return False
    return position == len(texts)


def align_split_list_values(requirements, items):
    """Align items with their list field when every value is explained by its split items in order."""
    for field in BUSINESS_FIELDS[4:]:
        rows = [item for item in items if item.field == field]
        if not rows:
            continue
        texts = [item.text for item in rows]
        current = getattr(requirements, field)
        if isinstance(current, list) and current == texts:
            for index, item in enumerate(rows):
                item.value_path = f'{field}[{index}]'
            continue
        if not isinstance(current, list) or not current or not _partition_items_over_values(current, texts):
            continue
        setattr(requirements, field, texts)
        for index, item in enumerate(rows):
            item.value_path = f'{field}[{index}]'


def align_evidence_ownership(requirements, items):
    """Derive ownership from item links; shared evidence is unowned, never double-owned."""
    owners = {}
    for item in items:
        for evidence_id in item.evidence_ids:
            owners.setdefault(evidence_id, []).append(item)
    for evidence in requirements.source_evidence:
        rows = owners.get(evidence.evidence_id, [])
        if len(rows) > 1:
            evidence.requirement_item_id = None
        elif len(rows) == 1 and evidence.requirement_item_id not in (
                None, rows[0].requirement_item_id):
            evidence.requirement_item_id = rows[0].requirement_item_id


def normalize_required_negation(items):
    """A negated required_* item is a prohibition; keep its text and evidence unchanged."""
    for item in items:
        if item.field.startswith('required_') and is_prohibition(item.text):
            item.modality = 'prohibited'


def resolve_spans(requirements, document):
    segments = {entry['segment_id']: entry for entry in document['source_segments']}
    check(len(segments) == len(document['source_segments']))
    for evidence in requirements.source_evidence:
        check(evidence.segment_id in segments, '引文片段不存在。')
        segment = segments[evidence.segment_id]
        check(evidence.source_kind == segment['source_kind'], '引文来源不得伪造。')
        check(evidence.source_location == segment['source_location'], '引文位置不一致。')
        original, quote = segment['original_content'], evidence.original_text
        start, end = evidence.start_offset, evidence.end_offset
        if start is None and end is None:
            start = original.find(quote)
            check(start >= 0 and original.find(quote, start + 1) < 0, '重复引文必须明确字符范围。')
            end = start + len(quote)
        check(start is not None and end is not None)
        check(0 <= start < end <= len(original) and original[start:end] == quote, '引文不是原始片段的精确切片。')
        evidence.start_offset, evidence.end_offset = start, end


def language_logic_explicit(items):
    languages = [item for item in items if item.field == 'programming_languages' and item.modality == 'required']
    if len(languages) < 2:
        return True
    return (len({item.logic_group_id for item in languages}) == 1
            and languages[0].logic_group_id is not None
            and len({item.operator for item in languages}) == 1
            and languages[0].operator in ('ALL', 'ANY'))


def extracted_language_uncertainty(requirements, items):
    languages = [item for item in items if item.field == 'programming_languages']
    if len(languages) < 2:
        return
    evidence = {e.evidence_id: e for e in requirements.source_evidence}
    operators = {item.operator for item in languages}
    quotes = '\n'.join(evidence[eid].original_text for item in languages for eid in item.evidence_ids)
    explicit = (len({item.logic_group_id for item in languages}) == 1
                and languages[0].logic_group_id is not None and len(operators) == 1)
    if explicit:
        op = languages[0].operator
        explicit = bool((op == 'ALL' and ALL_LOGIC.search(quotes)) or (op == 'ANY' and ANY_LOGIC.search(quotes)))
    if explicit:
        return
    for item in languages:
        item.logic_group_id, item.operator = None, 'not_applicable'
    uncertain = requirements.uncertain_information or []
    if not any(item.field == 'programming_languages' for item in uncertain):
        uncertain.append(UncertainItem(
            field='programming_languages', requirement_item_ids=[item.requirement_item_id for item in languages],
            reason='多个语言的组合关系未明确，不能自动采用 ALL 或 ANY。',
            question='请明确这些语言是全部必需还是任选其一，并编辑同组 ALL/ANY 或调整需求属性。',
            evidence_ids=list(dict.fromkeys(eid for item in languages for eid in item.evidence_ids)),
        ))
    requirements.uncertain_information = uncertain


def validate_source_rules(requirements, items, document):
    evidence = {entry.evidence_id: entry for entry in requirements.source_evidence}
    for item in items:
        quotes = '\n'.join(evidence[eid].original_text for eid in item.evidence_ids)
        if item.field in BUSINESS_FIELDS[4:]:
            if is_prohibition(quotes):
                check(item.modality == 'prohibited' and bool(NEGATION.search(item.text)), '不得丢弃证据中的否定条件。')
            if PREFERENCE.search(quotes) and not MANDATORY.search(quotes):
                check(item.modality != 'required', '不得把偏好改成硬条件。')
    # Conservative guard against silently omitting explicit prohibitions, even when
    # the model emits no items. Ambiguous prose must be reviewed, not guessed.
    for segment in document['source_segments']:
        for match in NEGATION.finditer(segment['original_content']):
            text = segment['original_content']
            start = max((text.rfind(mark, 0, match.start()) for mark in '。；;.!?\n'), default=-1) + 1
            ends = [text.find(mark, match.end()) for mark in '。；;.!?\n']
            end = min((value for value in ends if value >= 0), default=len(text))
            if not is_prohibition(text[start:end]):
                continue
            check(any(item.modality == 'prohibited' and any(
                evidence[eid].segment_id == segment['segment_id']
                and evidence[eid].start_offset <= match.start() < evidence[eid].end_offset
                for eid in item.evidence_ids) for item in items), '原文禁止条件未被提取或定位。')


def validate_extraction(raw, document):
    extraction = ModelExtraction.model_validate(raw)
    requirements, items = extraction.requirements, extraction.requirement_items
    check(all(item.requirement_item_id for item in items))
    check(len({item.requirement_item_id for item in items}) == len(items))
    check(all(item.supersedes_item_id is None for item in items))
    split_cross_segment_evidence(requirements, items, document)
    filter_uncertainty_links(requirements, items)
    align_split_list_values(requirements, items)
    align_evidence_ownership(requirements, items)
    normalize_required_negation(items)
    resolve_spans(requirements, document)
    validate_links(requirements, items)
    extracted_language_uncertainty(requirements, items)
    validate_coverage(requirements, items)
    validate_source_rules(requirements, items, document)
    mapping = {item.requirement_item_id: str(uuid4()) for item in items}
    for item in items:
        item.requirement_item_id = mapping[item.requirement_item_id]
    for evidence in requirements.source_evidence:
        if evidence.requirement_item_id is not None:
            check(evidence.requirement_item_id in mapping)
            evidence.requirement_item_id = mapping[evidence.requirement_item_id]
    for uncertain in requirements.uncertain_information or []:
        uncertain.requirement_item_ids = [mapping[rid] for rid in uncertain.requirement_item_ids]
    verified_items = [RequirementItem.model_validate(item.model_dump()) for item in items]
    validate_links(requirements, verified_items)
    return requirements, verified_items
