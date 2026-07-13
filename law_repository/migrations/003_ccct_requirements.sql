-- 세관장확인대상물품(CCCT) 무손실 원장 + Requirement 정규화 모델.
--
-- ccct_queries는 빈 items 응답까지 보존한다. 그래야 검색 0건을
-- "조회되지 않음"과 "공식 API가 요건 없음을 응답함"으로 구분할 수 있다.
-- ccct_source_records는 API item 하나를 raw_record jsonb로 보존한다.
-- ccct_requirements는 canonical key로 만든 조회/그래프 projection용 정규화 뷰다.

CREATE TABLE knowledge_snapshots (
  snapshot_id    text PRIMARY KEY,
  source_type    text NOT NULL,
  content_hash   text NOT NULL,
  query_count    integer NOT NULL CHECK (query_count >= 0),
  raw_row_count  integer NOT NULL CHECK (raw_row_count >= 0),
  source_uri     text NOT NULL,
  generated_at   timestamptz NOT NULL DEFAULT now(),
  is_current     boolean NOT NULL DEFAULT false
);

CREATE UNIQUE INDEX uq_knowledge_snapshot_current_source
  ON knowledge_snapshots (source_type)
  WHERE is_current;

CREATE TABLE ccct_queries (
  snapshot_id    text NOT NULL REFERENCES knowledge_snapshots(snapshot_id) ON DELETE CASCADE,
  hsk10          char(10) NOT NULL CHECK (hsk10 ~ '^[0-9]{10}$'),
  trade_type     text NOT NULL CHECK (trade_type IN ('import', 'export')),
  source_uri     text NOT NULL,
  item_count     integer NOT NULL CHECK (item_count >= 0),
  query_hash     text NOT NULL,
  raw_response   jsonb NOT NULL,
  PRIMARY KEY (snapshot_id, hsk10, trade_type)
);

CREATE INDEX idx_ccct_queries_current_lookup
  ON ccct_queries (hsk10, trade_type, snapshot_id);

CREATE TABLE ccct_requirement_documents (
  document_code  text PRIMARY KEY,
  display_name   text NOT NULL,
  normalized_name text NOT NULL UNIQUE
);

CREATE TABLE ccct_requirements (
  snapshot_id            text NOT NULL REFERENCES knowledge_snapshots(snapshot_id) ON DELETE CASCADE,
  requirement_id         text NOT NULL,
  requirement_version_hash text NOT NULL,
  hsk10                  char(10) NOT NULL CHECK (hsk10 ~ '^[0-9]{10}$'),
  trade_type             text NOT NULL CHECK (trade_type IN ('import', 'export')),
  law_code               text NOT NULL,
  law_name               text NOT NULL,
  agency_code            text NOT NULL,
  agency_name            text NOT NULL,
  document_code          text NOT NULL REFERENCES ccct_requirement_documents(document_code),
  document_name          text NOT NULL,
  valid_from             date,
  valid_to               date,
  rtm_tpcd               text NOT NULL DEFAULT '',
  PRIMARY KEY (snapshot_id, requirement_id),
  CHECK (valid_to IS NULL OR valid_from IS NULL OR valid_to >= valid_from)
);

CREATE INDEX idx_ccct_requirements_lookup
  ON ccct_requirements (snapshot_id, hsk10, trade_type, valid_from, valid_to);

CREATE TABLE ccct_source_records (
  snapshot_id      text NOT NULL,
  source_record_id text NOT NULL,
  requirement_id   text NOT NULL,
  hsk10            char(10) NOT NULL CHECK (hsk10 ~ '^[0-9]{10}$'),
  trade_type       text NOT NULL CHECK (trade_type IN ('import', 'export')),
  source_item_index integer NOT NULL CHECK (source_item_index >= 0),
  source_uri       text NOT NULL,
  raw_record_hash  text NOT NULL,
  raw_record       jsonb NOT NULL,
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (snapshot_id, source_record_id),
  UNIQUE (snapshot_id, hsk10, trade_type, source_item_index),
  FOREIGN KEY (snapshot_id, requirement_id)
    REFERENCES ccct_requirements(snapshot_id, requirement_id) ON DELETE CASCADE
);

CREATE INDEX idx_ccct_source_records_requirement
  ON ccct_source_records (snapshot_id, requirement_id);
