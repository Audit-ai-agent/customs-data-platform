-- 검증된 공식 HSK/CCCT archive 이력.
--
-- archive 원문(XLSX, 페이지별 CCCT receipt)은 Git과 PostgreSQL에 넣지 않는다.
-- 이 migration은 검증된 URI/hash/date/count와 정규화 projection만 보존한다.
-- 기존 Neo4j current HSK/Requirement projection은 이 table들로 대체하지 않는다.

CREATE TABLE hsk_ccct_archives (
  archive_id                    text PRIMARY KEY
    CHECK (archive_id ~ '^hsk_ccct_[0-9a-f]{64}$'),
  archive_as_of                 date NOT NULL UNIQUE
    CHECK (archive_as_of >= DATE '2026-01-01'),
  manifest_sha256               text NOT NULL UNIQUE
    CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  collected_at                  timestamptz NOT NULL,
  hsk_level_names_uri           text NOT NULL
    CHECK (hsk_level_names_uri ~ '^https://(www[.])?data[.]go[.]kr/data/'),
  hsk_level_names_sha256        text NOT NULL
    CHECK (hsk_level_names_sha256 ~ '^[0-9a-f]{64}$'),
  hsk_level_names_byte_size     bigint NOT NULL CHECK (hsk_level_names_byte_size > 0),
  hsk_level_names_published_at  date NOT NULL,
  hsk_level_names_effective_at  date NOT NULL,
  hsk_leaf_attributes_uri       text NOT NULL
    CHECK (hsk_leaf_attributes_uri ~ '^https://(www[.])?data[.]go[.]kr/data/'),
  hsk_leaf_attributes_sha256    text NOT NULL
    CHECK (hsk_leaf_attributes_sha256 ~ '^[0-9a-f]{64}$'),
  hsk_leaf_attributes_byte_size bigint NOT NULL CHECK (hsk_leaf_attributes_byte_size > 0),
  hsk_leaf_attributes_published_at date NOT NULL,
  hsk_leaf_attributes_effective_at date NOT NULL,
  ccct_official_uri             text NOT NULL
    CHECK (ccct_official_uri ~ '^https://apis[.]data[.]go[.]kr/1220000/retrieveCcctLworCd/'),
  ccct_receipts_sha256          text NOT NULL
    CHECK (ccct_receipts_sha256 ~ '^[0-9a-f]{64}$'),
  ccct_receipts_byte_size       bigint NOT NULL CHECK (ccct_receipts_byte_size > 0),
  ccct_receipts_published_at    date NOT NULL,
  ccct_receipts_effective_at    date NOT NULL,
  CHECK (
    hsk_level_names_published_at <= archive_as_of
    AND hsk_level_names_effective_at <= archive_as_of
    AND hsk_leaf_attributes_published_at <= archive_as_of
    AND hsk_leaf_attributes_effective_at <= archive_as_of
    AND ccct_receipts_published_at <= archive_as_of
    AND ccct_receipts_effective_at <= archive_as_of
  )
);

CREATE TABLE hsk_snapshots (
  hsk_snapshot_id     text PRIMARY KEY
    CHECK (hsk_snapshot_id ~ '^hsk_[0-9a-f]{64}$'),
  archive_id          text NOT NULL UNIQUE
    REFERENCES hsk_ccct_archives(archive_id) ON DELETE RESTRICT,
  snapshot_as_of      date NOT NULL,
  projection_sha256   text NOT NULL
    CHECK (projection_sha256 ~ '^[0-9a-f]{64}$'),
  node_count          integer NOT NULL CHECK (node_count > 0),
  leaf_count          integer NOT NULL CHECK (leaf_count > 0 AND leaf_count <= node_count),
  CHECK (snapshot_as_of >= DATE '2026-01-01')
);

CREATE TABLE hsk_node_versions (
  hsk_snapshot_id  text NOT NULL
    REFERENCES hsk_snapshots(hsk_snapshot_id) ON DELETE CASCADE,
  code             text NOT NULL CHECK (code ~ '^[0-9]{2,10}$'),
  level            smallint NOT NULL CHECK (level IN (2, 4, 5, 6, 7, 8, 9, 10)),
  name_ko          text,
  name_en          text,
  is_leaf          boolean NOT NULL,
  hsk10            char(10),
  hs6              char(6),
  heading4         char(4),
  chapter2         char(2),
  valid_from       date,
  valid_to         date,
  quantity_unit    text,
  weight_unit      text,
  category_code    text,
  category_name    text,
  version_hash     text NOT NULL CHECK (version_hash ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY (hsk_snapshot_id, code),
  CHECK (char_length(code) = level),
  CHECK (
    (is_leaf AND level = 10 AND hsk10 = code AND hs6 = left(code, 6)
     AND heading4 = left(code, 4) AND chapter2 = left(code, 2))
    OR
    (NOT is_leaf AND hsk10 IS NULL AND hs6 IS NULL AND heading4 IS NULL AND chapter2 IS NULL)
  ),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE INDEX idx_hsk_node_versions_snapshot_leaf
  ON hsk_node_versions (hsk_snapshot_id, code)
  WHERE is_leaf;

CREATE TABLE hsk_ccct_archive_ccct_bindings (
  archive_id         text PRIMARY KEY
    REFERENCES hsk_ccct_archives(archive_id) ON DELETE RESTRICT,
  ccct_snapshot_id   text NOT NULL UNIQUE
    REFERENCES knowledge_snapshots(snapshot_id) ON DELETE RESTRICT,
  query_count        integer NOT NULL CHECK (query_count >= 0),
  raw_row_count      integer NOT NULL CHECK (raw_row_count >= 0),
  empty_query_count  integer NOT NULL CHECK (empty_query_count >= 0),
  CHECK (empty_query_count <= query_count)
);

CREATE INDEX idx_hsk_ccct_archive_date
  ON hsk_ccct_archives (archive_as_of);
