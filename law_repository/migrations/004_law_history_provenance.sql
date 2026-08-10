-- 법령 이력 프로필의 공식 원문 provenance와 명시적 refresh 원장.
--
-- 원문 JSON과 벡터는 Git 또는 PostgreSQL에 저장하지 않는다. 이 표는 검증된
-- 공식 URL, raw SHA-256, 수집 시각, 시행일 및 조문 수만 보존해 시간별
-- law_versions/law_articles가 어느 로컬 원문으로부터 만들어졌는지 추적한다.

-- 기존 3법 profile에도 이 migration이 적용될 수 있으므로 version 표시값은
-- nullable additive column으로 둔다. law-history source는 application
-- validator가 반드시 채운 값만 수용한다.
ALTER TABLE law_versions ADD COLUMN IF NOT EXISTS law_name text;
ALTER TABLE law_versions ADD COLUMN IF NOT EXISTS hierarchy text;
ALTER TABLE law_versions ADD COLUMN IF NOT EXISTS ministry text;

CREATE TABLE law_version_sources (
  law_id            text NOT NULL,
  mst               text NOT NULL,
  official_uri      text NOT NULL CHECK (official_uri ~ '^https://www\\.law\\.go\\.kr/'),
  raw_sha256        text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
  collected_at      timestamptz NOT NULL,
  effective_date    date NOT NULL,
  article_count     integer NOT NULL CHECK (article_count >= 0),
  is_repealed       boolean NOT NULL,
  source_kind       text NOT NULL CHECK (source_kind IN ('baseline', 'refresh')),
  PRIMARY KEY (law_id, mst),
  FOREIGN KEY (law_id, mst) REFERENCES law_versions(law_id, mst)
);

CREATE TABLE law_history_refreshes (
  refresh_id        text PRIMARY KEY,
  profile            text NOT NULL CHECK (profile = 'functional-uat-law-history'),
  from_date         date NOT NULL,
  to_date           date NOT NULL,
  collected_at      timestamptz NOT NULL,
  -- Digest of the deterministic refresh receipt, not of a mutable active
  -- manifest.  A later refresh legitimately changes that manifest while this
  -- immutable ledger row must remain verifiable.
  refresh_sha256    text NOT NULL CHECK (refresh_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (from_date <= to_date)
);

CREATE INDEX idx_law_version_sources_effective
  ON law_version_sources (law_id, effective_date, mst);
