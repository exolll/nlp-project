"""Chroma collection adapter: explicit vectors, snapshot/repository isolation, raw distances."""
import math

MODEL_VERSION_METADATA = 'model_version'


class ChromaStoreError(ValueError):
    pass


def _slug(model):
    return model.lower().replace('/', '_').replace('-', '_').replace('.', '_')


class ChromaStore:
    def __init__(self, client, *, model, dimension, version, preprocess_version='raw-v1'):
        self.client, self.model, self.dimension = client, model, dimension
        self.version, self.preprocess = version, preprocess_version
        self.name = f'pp_{_slug(model)}_{dimension}_{preprocess_version}'
        self.collection = client.get_or_create_collection(self.name, metadata={'hnsw:space': 'cosine'})

    @classmethod
    def persistent(cls, path, **kwargs):
        import chromadb
        client = chromadb.PersistentClient(path=str(path),
                                           settings=chromadb.Settings(anonymized_telemetry=False))
        return cls(client, **kwargs)

    def _check_vector(self, vector):
        if (not isinstance(vector, list) or len(vector) != self.dimension
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
                or not any(value != 0 for value in vector)):
            raise ChromaStoreError('向量维度或数值无效。')
        return [float(value) for value in vector]

    def _metadata(self, chunk):
        required = ('repository_id', 'snapshot_id', 'commit_sha', 'readme_path', 'content_hash')
        if any(not isinstance(chunk.get(key), str) or not chunk[key] for key in required):
            raise ChromaStoreError('块元数据不完整。')
        return {'repository_id': str(chunk['repository_id']), 'snapshot_id': chunk['snapshot_id'],
                'chunk_id': chunk['chunk_id'], 'commit_sha': chunk['commit_sha'],
                'readme_path': chunk['readme_path'], 'chunk_index': int(chunk['ordinal']),
                'heading_path': ' > '.join(chunk.get('heading_path') or []),
                'line_start': int(chunk['line_start']), 'line_end': int(chunk['line_end']),
                'byte_start': int(chunk['byte_start']), 'byte_end': int(chunk['byte_end']),
                'content_hash': chunk['content_hash'], MODEL_VERSION_METADATA: self.version}

    def upsert(self, chunks, vectors):
        if not chunks:
            return 0
        ids, embeddings, metadatas = [], [], []
        for chunk in chunks:
            if chunk['chunk_id'] not in vectors:
                raise ChromaStoreError('缺少块向量。')
            ids.append(chunk['chunk_id'])
            embeddings.append(self._check_vector(list(vectors[chunk['chunk_id']])))
            metadatas.append(self._metadata(chunk))
        if len(set(ids)) != len(ids):
            raise ChromaStoreError('同批块 ID 重复。')
        self.collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)
        return len(ids)

    def count(self, snapshot_id=None):
        if snapshot_id is None:
            return self.collection.count()
        return len(self.collection.get(where={'snapshot_id': snapshot_id}, include=[])['ids'])

    def verify(self, snapshot_id, expected):
        rows = self.collection.get(where={'snapshot_id': snapshot_id}, include=['metadatas'])
        found = dict(zip(rows['ids'], rows['metadatas']))
        missing = sorted(set(expected) - set(found))
        extra = sorted(set(found) - set(expected))
        mismatched = sorted(chunk_id for chunk_id, digest in expected.items()
                            if chunk_id in found and found[chunk_id].get('content_hash') != digest)
        return {'count': len(found), 'missing': missing, 'extra': extra, 'mismatched': mismatched,
                'ok': not missing and not extra and not mismatched}

    def query(self, vector, *, snapshot_id, repository_id, limit=10):
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ChromaStoreError('检索数量超出允许范围。')
        result = self.collection.query(
            query_embeddings=[self._check_vector(list(vector))], n_results=limit,
            where={'$and': [{'snapshot_id': snapshot_id}, {'repository_id': str(repository_id)}]},
            include=['metadatas', 'distances'])
        return [{'chunk_id': chunk_id, 'distance': float(distance), 'metadata': metadata}
                for chunk_id, distance, metadata in zip(result['ids'][0], result['distances'][0],
                                                        result['metadatas'][0])]
