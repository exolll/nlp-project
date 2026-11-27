"""Regression tests for the evidence gate: overview fields, null reverse links, surfaced diagnostics."""
from uuid import uuid4

from test_requirements_api import api_factory, confirm, prepare, read, start  # noqa: F401 - fixtures/helpers
from test_requirement_validation import extraction
from app.services.search_routes import run_view


def _paraphrased_overview(doc):
    raw = extraction(doc, [('project_description', '实现检索', 'informational')])
    # The stored requirement is a summary of the evidence, not a slice of it.
    raw['requirements']['project_description'] = '实现检索与导出功能'
    raw['requirement_items'][0]['text'] = '实现检索与导出功能'
    return raw


def test_confirm_allows_a_paraphrased_overview_field(api_factory):
    api_factory.behavior['raw'] = _paraphrased_overview
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    queued = start(client, headers, did, str(uuid4()))
    assert queued.status_code == 202
    body = read(client, headers, did)
    assert body['extraction']['status'] == 'completed'
    created = body['revisions'][0]
    assert confirm(client, headers, created['revision_id']).status_code == 200


def test_confirm_still_blocks_a_paraphrased_requirement_field(api_factory):
    def paraphrased(doc):
        raw = extraction(doc)
        raw['requirements']['required_features'][0] += '，并支持邮件提醒'
        raw['requirement_items'][0]['text'] = raw['requirements']['required_features'][0]
        return raw

    api_factory.behavior['raw'] = paraphrased
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    assert start(client, headers, did, str(uuid4())).status_code == 202
    created = read(client, headers, did)['revisions'][0]
    response = confirm(client, headers, created['revision_id'])
    assert response.status_code == 422
    assert response.json()['error_code'] == 'REQUIREMENT_EVIDENCE_MISMATCH'


def test_failed_extraction_exposes_the_sanitised_diagnostic_and_prompt_version(api_factory):
    def uncovered(doc):
        raw = extraction(doc)
        # A value without a matching item breaks coverage, not the JSON envelope.
        raw['requirements']['required_features'].append('多出来的需求')
        return raw

    api_factory.behavior['raw'] = uncovered
    client, _, _ = api_factory()
    headers, _, did = prepare(client)
    assert start(client, headers, did, str(uuid4())).status_code == 202
    job = read(client, headers, did)['extraction']
    assert job['status'] == 'failed'
    assert job['error']['error_code'] == 'EXTRACTION_INVALID'
    assert job['prompt_version'] == 'requirements-v4'
    assert job['diagnostic']['validator'] == 'validate_coverage'
    # Sanitised: no line numbers and no provider payload leak into the response.
    assert set(job['diagnostic']) == {'validator', 'field'}


def test_run_view_exposes_planner_source_normalisation():
    record = {'run_id': str(uuid4()), 'task_id': str(uuid4()), 'confirmed_revision_id': str(uuid4()),
              'status': 'running', 'deep_check': None}
    manifest = {'run_status': 'running', 'plan_publication_status': 'published', 'dispatch_closed': False,
                'search_execution': {}, 'readme_persistence_operations': {}, 'candidates': {},
                'planner_operation': {'source_normalization': [
                    {'group': 'A', 'declared': ['/project_topic'], 'used': ['/project_description']}]}}
    view = run_view(record, manifest)
    assert view['plan_warnings'] == [
        {'group': 'A', 'declared': ['/project_topic'], 'used': ['/project_description']}]
