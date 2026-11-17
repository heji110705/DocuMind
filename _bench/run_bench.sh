#!/bin/sh
# pgvector 索引选型 bench：122 块 / 94 条查询，HNSW vs IVFFlat vs 精确扫描
# 跑在一次性容器里，跑完就删，不接主链路（LightRAG 1.5.7 没有 pgvector 后端）
set -e
IMG=docker.m.daocloud.io/pgvector/pgvector:pg16
NAME=pgbench

echo "=== 起容器（mem 256m，不发布端口）==="
docker rm -f $NAME >/dev/null 2>&1 || true
docker run -d --name $NAME --memory=256m --memory-swap=512m \
  -e POSTGRES_PASSWORD=bench -e POSTGRES_DB=bench \
  $IMG >/dev/null

echo -n "等待就绪"
for i in $(seq 1 40); do
  if docker exec $NAME pg_isready -U postgres -d bench >/dev/null 2>&1; then echo " ok"; break; fi
  echo -n "."; sleep 1
done

docker cp /opt/bench/chunks.csv $NAME:/tmp/chunks.csv
docker cp /opt/bench/queries.csv $NAME:/tmp/queries.csv
for f in exact hnsw ivfflat; do docker cp /opt/bench/run_$f.sql $NAME:/tmp/run_$f.sql; done

echo "=== 建表并灌 122 条 ==="
docker exec $NAME psql -q -U postgres -d bench -c "CREATE EXTENSION IF NOT EXISTS vector;"
docker exec $NAME psql -q -U postgres -d bench -c "CREATE TABLE chunks(id text primary key, embedding vector(512));"
docker exec $NAME psql -q -U postgres -d bench -c "\copy chunks(id,embedding) FROM '/tmp/chunks.csv' WITH (FORMAT text, DELIMITER E'\t')"
docker exec $NAME psql -tA -U postgres -d bench -c "SELECT '行数=' || count(*) FROM chunks;"

echo "=== 精确扫描（基线）==="
docker exec $NAME psql -q -U postgres -d bench -f /tmp/run_exact.sql > /tmp/out_exact.txt 2>&1
echo "  输出行数 $(wc -l < /tmp/out_exact.txt)"

echo "=== HNSW (m=16, ef_construction=200, ef_search=40) ==="
docker exec $NAME psql -q -U postgres -d bench -c "CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=200);"
docker exec $NAME psql -q -U postgres -d bench -f /tmp/run_hnsw.sql > /tmp/out_hnsw.txt 2>&1
echo "  输出行数 $(wc -l < /tmp/out_hnsw.txt)"

echo "=== IVFFlat (lists=100, probes=10) ==="
docker exec $NAME psql -q -U postgres -d bench -c "DROP INDEX chunks_hnsw;"
docker exec $NAME psql -q -U postgres -d bench -c "CREATE INDEX chunks_ivf ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists=100);"
docker exec $NAME psql -q -U postgres -d bench -f /tmp/run_ivfflat.sql > /tmp/out_ivfflat.txt 2>&1
echo "  输出行数 $(wc -l < /tmp/out_ivfflat.txt)"

echo "=== 体积与内存 ==="
docker exec $NAME psql -tA -U postgres -d bench -c "SELECT '表=' || pg_size_pretty(pg_relation_size('chunks'))"
docker exec $NAME psql -tA -U postgres -d bench -c "SELECT '索引 ' || indexname || ' = ' || pg_size_pretty(pg_relation_size(indexname::regclass)) FROM pg_indexes WHERE tablename='chunks'"
docker stats --no-stream --format "容器内存 {{.MemUsage}}" $NAME
