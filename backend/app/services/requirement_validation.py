"""Deterministic structure/provenance checks; source text never grants authority."""
import re
from uuid import uuid4

from app.schemas.requirements import BUSINESS_FIELDS, ModelExtraction, RequirementItem, UncertainItem

NEGATION = re.compile(r"禁止|严禁|不得|不能|不可|不要|不准|不(?:使用|允许|采用|支持|依赖)|\b(?:must not|mustn't|shall not|do not|don't|cannot|not allowed|not permitted|without|prohibited|forbidden)\b", re.I)
PREFERENCE = re.compile(r'建议|推荐|优先|最好|可选|尽量|可以|可考虑|倾向|\b(?:prefer(?:red|ably)?|recommend(?:ed)?|suggest(?:ed)?|optional|ideally|may|could|nice to have)\b', re.I)
MANDATORY = re.compile(r'必须|务必|需要|要求|\b(?:must|required|shall|mandatory)\b', re.I)
ANY_LOGIC = re.compile(r'任选|任一|其中一个|至少一种|或者|二选一|\b(?:either|any one|one of|or)\b', re.I)
ALL_LOGIC = re.compile(r'全部|同时|两者|均须|都(?:要|必须)|\b(?:both|all of|together)\b', re.I)


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
    """One item per declared requirement value.

    Descriptive fields (project_topic / project_description / project_type / domain) are
    context for the reader, not search input: their items only carry provenance, so they sit
    outside the one-to-one contract. A model that invents or omits such an item must not
    discard an otherwise valid extraction.
    """
    expected = {path: pair for path, pair in values(requirements).items() if pair[0] not in BUSINESS_FIELDS[:4]}
    seen = set()
    for item in items:
        if item.field in BUSINESS_FIELDS[:4]:
            continue
        check(item.value_path not in seen)
        check(expected.get(item.value_path) == (item.field, item.text))
        check(bool(item.text.strip()))
        seen.add(item.value_path)
        if item.field.startswith('required_'):
            check(item.modality in ('required', 'prohibited'))
        elif item.field.startswith('preferred_'):
            check(item.modality == 'preferred')
        if item.operator == 'not_applicable':
            check(item.logic_group_id is None)
        else:
            check(bool(item.logic_group_id and item.logic_group_id.strip()))
        if NEGATION.search(item.text):
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
        # Uncertainties may quote a sentence that also supports another field; the evidence
        # is a supporting quote, not a claim about the uncertainty's field. Rejecting the
        # whole extraction over that bookkeeping detail would discard a valid result.
        linked.update(uncertainty.evidence_ids)
    check(linked == set(evidence), '不得携带无关或孤立证据。')


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
            if NEGATION.search(quotes):
                check(item.modality == 'prohibited' and bool(NEGATION.search(item.text)), '不得丢弃证据中的否定条件。')
            if PREFERENCE.search(quotes) and not MANDATORY.search(quotes):
                check(item.modality != 'required', '不得把偏好改成硬条件。')
    # Conservative guard against silently omitting explicit prohibitions, even when
    # the model emits no items. Ambiguous prose must be reviewed, not guessed.
    for segment in document['source_segments']:
        for match in NEGATION.finditer(segment['original_content']):
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
