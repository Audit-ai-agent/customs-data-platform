-- A 법령일련번호(MST)는 공포 단위라서 단계 시행 시 여러 시행일 본문을 가질 수 있다.
-- 공개 MST는 인용/provenance에 그대로 보존하고, 저장소 조인에는
-- ``<MST>@<YYYYMMDD>`` 형식의 version_key를 별도로 사용한다.
--
-- 기존 3법 profile은 version_key=mst 로 backfill하여 동일한 동작을 유지한다.
-- law-history profile의 새 source만 MST+시행일 조합 key를 사용한다.

ALTER TABLE law_versions ADD COLUMN version_key text;
UPDATE law_versions SET version_key = mst WHERE version_key IS NULL;
ALTER TABLE law_versions ALTER COLUMN version_key SET NOT NULL;
ALTER TABLE law_versions
  ADD CONSTRAINT law_versions_law_id_version_key_key UNIQUE (law_id, version_key);

ALTER TABLE law_articles ADD COLUMN version_key text;
UPDATE law_articles SET version_key = version_mst WHERE version_key IS NULL;
ALTER TABLE law_articles ALTER COLUMN version_key SET NOT NULL;

ALTER TABLE law_version_sources ADD COLUMN version_key text;
UPDATE law_version_sources SET version_key = mst WHERE version_key IS NULL;
ALTER TABLE law_version_sources ALTER COLUMN version_key SET NOT NULL;

-- law_version_sources의 기존 FK가 MST 단일 uniqueness에 의존하므로
-- version_key FK/PK로 먼저 교체한 뒤 기존 MST uniqueness를 해제한다.
ALTER TABLE law_version_sources
  DROP CONSTRAINT law_version_sources_law_id_mst_fkey;
ALTER TABLE law_version_sources
  DROP CONSTRAINT law_version_sources_pkey;
ALTER TABLE law_version_sources
  ADD CONSTRAINT law_version_sources_pkey PRIMARY KEY (law_id, version_key);
ALTER TABLE law_version_sources
  ADD CONSTRAINT law_version_sources_law_id_version_key_fkey
  FOREIGN KEY (law_id, version_key) REFERENCES law_versions(law_id, version_key);

ALTER TABLE law_versions
  DROP CONSTRAINT law_versions_law_id_mst_key;
ALTER TABLE law_versions
  ADD CONSTRAINT law_versions_law_id_mst_enforcement_date_key
  UNIQUE (law_id, mst, enforcement_date);

DROP INDEX idx_law_version_sources_effective;
CREATE INDEX idx_law_version_sources_effective
  ON law_version_sources (law_id, effective_date, version_key);
