"""Explicitly authorized SiliconFlow preflight and real chunk index/retrieval acceptance.

`preflight` checks the model list and one embedding call. `index` embeds the saved README
snapshots of a stable run (no GitHub). `retrieve` is read-only over the saved index.
"""
import argparse
import json
import sys
from pathlib import Path

OWNER = 'e9b9a322101866a4eceff782b3f18fbcf8264c70bef2434b1eed94c9f1f0f547'
RUN = '287e4237-42df-4748-92a8-d114c418778e'


def load_revision(storage, owner, run_id):
    from app.schemas.requirements import RequirementRevision
    raw, _ = storage.read_version(f'sessions/{owner}.json')
    session = json.loads(raw)
    task = next(t for t in session['tasks'].values()
                if any(r['run_id'] == run_id for r in (t.get('search_runs') or {}).values()))
    value = next(r for r in task['requirements']['revisions']
                 if r['status'] == 'confirmed' and r['confirmed_at'])
    return task, RequirementRevision.model_validate(value)


def preflight():
    import anyio
    import httpx

    from app.core.settings import SiliconFlowSettings
    from app.services.siliconflow_client import SiliconFlowClient

    config = SiliconFlowSettings()
    headers = {'Authorization': 'Bearer ' + config.siliconflow_api_key.get_secret_value()}
    report = {'base_url': config.siliconflow_base_url, 'model': config.embedding_model,
              'configured_dimension': config.embedding_dimension}
    try:
        response = httpx.get(config.siliconflow_base_url.rstrip('/') + '/models',
                             params={'sub_type': 'embedding'}, headers=headers,
                             timeout=20, trust_env=False)
        ids = [row.get('id') for row in response.json().get('data', [])] if response.status_code == 200 else []
        report['model_list'] = {'status': response.status_code, 'count': len(ids),
                                'contains_model': config.embedding_model in ids, 'ids': ids[:20]}
    except Exception as exc:
        report['model_list'] = {'status': 'error', 'error': type(exc).__name__}
    client = SiliconFlowClient(config)
    vectors, usage, attempts = anyio.run(client.embed, ['你好，这是 Embedding 预检。'])
    vector = vectors[0]
    report['embedding'] = {'dimension': len(vector), 'finite': all(isinstance(v, float) for v in vector),
                           'nonzero': any(v != 0 for v in vector), 'usage': usage, 'attempts': attempts}
    report['ok'] = report['embedding']['dimension'] == 1024 and report['embedding']['finite'] and report['embedding']['nonzero']
    print(json.dumps(report, ensure_ascii=False))
    if not report['ok']:
        raise SystemExit(1)


def build_stack():
    from app.core.settings import Settings, SiliconFlowSettings
    from app.persistence.search_database import SearchDatabaseSettings, create_search_engine
    from app.persistence.search_repository import SearchRepository
    from app.services.chroma_store import ChromaStore
    from app.services.embedding_service import EmbeddingService
    from app.services.search_persistence import SearchPersistence
    from app.services.siliconflow_client import SiliconFlowClient
    from app.services.storage import create_storage

    settings, embedding = Settings(), SiliconFlowSettings()
    storage = create_storage(settings)
    engine = create_search_engine(SearchDatabaseSettings())
    persistence = SearchPersistence(storage, SearchRepository(engine))
    service = EmbeddingService(SiliconFlowClient(embedding), storage, embedding)
    chroma = ChromaStore.persistent(settings.chroma_path, model=embedding.embedding_model,
                                    dimension=embedding.embedding_dimension,
                                    version=embedding.version())
    return settings, embedding, storage, engine, persistence, service, chroma


def index(owner, run_id):
    from app.services.chunk_index import ChunkIndexer

    settings, embedding, storage, engine, persistence, service, chroma = build_stack()
    try:
        task, revision = load_revision(storage, owner, run_id)
        indexer = ChunkIndexer(persistence, service, embedding, chroma_factory=lambda: chroma)
        result = indexer.index(owner, task['id'], run_id, revision)
        print(json.dumps({'phase': 'index', 'status': result['status'],
                          'chunk_count': result['chunk_count'],
                          'total_estimate_tokens': result['total_estimate_tokens'],
                          'snapshots': result['snapshots'], 'embedding': result['embedding']},
                         ensure_ascii=False))
        return result
    finally:
        engine.dispose()


def retrieve(owner, run_id):
    from app.services.chunk_index import ChunkIndexer
    from app.services.embedding_service import EmbeddingProgress
    from app.services.retrieval import retrieve as hybrid

    settings, embedding, storage, engine, persistence, service, chroma = build_stack()
    try:
        task, revision = load_revision(storage, owner, run_id)
        indexer = ChunkIndexer(persistence, service, embedding, chroma_factory=lambda: chroma)
        view = indexer.read(owner, task['id'], run_id, revision)
        report = view['chunk_manifest']
        snapshots = [{'repository_id': row['repository_id'], 'snapshot_id': row['snapshot_id'],
                      'coverage': row['coverage'], 'chunks': row['chunks']} for row in report['snapshots']]
        progress = EmbeddingProgress()

        def embed(texts):
            summary = service.embed_sync(texts, progress, max_batches=1, max_tokens=16384)
            return [summary.vectors[text] for text in texts]

        bundle = hybrid(revision.requirement_items, snapshots, embed=embed,
                        dense_query=lambda vector, sid, rid, limit: chroma.query(
                            vector, snapshot_id=sid, repository_id=rid, limit=limit))
        bundle.update(model=embedding.embedding_model, dimension=embedding.embedding_dimension,
                      owner=owner, task_id=task['id'], run_id=run_id,
                      confirmed_revision_id=str(revision.revision_id),
                      query_usage=progress.snapshot()['usage'])
        summary = []
        evidence = {'plain .txt': False, 'per-word timestamps': False}
        for candidate in bundle['candidates']:
            for item in candidate['items']:
                for chunk in item['chunks']:
                    for needle in evidence:
                        if needle in chunk['text']:
                            evidence[needle] = True
        for candidate in bundle['candidates']:
            if not any(item['chunks'] for item in candidate['items']):
                continue
            summary.append({'repository_id': candidate['repository_id'], 'snapshot_id': candidate['snapshot_id'],
                            'coverage': candidate['coverage'],
                            'items': [{'text': item['text'], 'top': [
                                {'chunk_id': chunk['chunk_id'], 'line': chunk['line_start'],
                                 'methods': chunk['methods'],
                                 'dense_distance': chunk['dense_distance'],
                                 'rrf': chunk['rrf_score'],
                                 'quote': chunk['text'][:90]}
                                for chunk in item['chunks'][:2]]} for item in candidate['items']]})
        print(json.dumps({'phase': 'retrieve', 'chunk_count': report['chunk_count'],
                          'query_usage': bundle['query_usage'], 'known_evidence': evidence,
                          'candidates': summary}, ensure_ascii=False))
        return bundle
    finally:
        engine.dispose()


def publish(owner, run_id):
    from app.services.chunk_index import ChunkIndexer
    from app.services.evidence_bundle import EvidenceBundleService, EvidenceBundleStore

    settings, embedding, storage, engine, persistence, service, chroma = build_stack()
    try:
        task, revision = load_revision(storage, owner, run_id)
        indexer = ChunkIndexer(persistence, service, embedding, chroma_factory=lambda: chroma)
        bundle = EvidenceBundleService(persistence, indexer, embedding).publish(
            owner, task['id'], run_id, revision)
        stored, operation = EvidenceBundleStore(storage).read(owner, run_id, revision)
        print(json.dumps({'phase': 'publish', 'status': bundle['status'],
                          'operation_status': operation['status'],
                          'bundle_matches_stored': stored == bundle,
                          'query_usage': bundle.get('query_usage'),
                          'requirements': len(bundle['requirements']),
                          'candidates': [{'repository_id': row['repository_id'],
                                          'status': row['status'], 'coverage': row['coverage'],
                                          'repository': row.get('repository'),
                                          'items': [{'text': item['text'], 'status': item['status'],
                                                     'chunks': len(item['chunks'])}
                                                    for item in row['items']]}
                                         for row in bundle['candidates']]}, ensure_ascii=False))
        return bundle
    finally:
        engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['preflight', 'index', 'retrieve', 'publish'], required=True)
    parser.add_argument('--owner', default=OWNER)
    parser.add_argument('--run', default=RUN)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    {'preflight': preflight, 'index': lambda: index(arguments.owner, arguments.run),
     'retrieve': lambda: retrieve(arguments.owner, arguments.run),
     'publish': lambda: publish(arguments.owner, arguments.run)}[arguments.phase]()
