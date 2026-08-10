-- PostgreSQL 표준 문자열에서 ``\\.``은 정규식의 점 이스케이프가 아니라
-- 역슬래시 자체를 요구한다.  004의 제약은 정상 공식 HTTPS URI까지 거부했으므로
-- 기존 체크 제약만 교체한다.  애플리케이션의 allowlist 검증은 그대로 유지한다.
ALTER TABLE law_version_sources
  DROP CONSTRAINT law_version_sources_official_uri_check;
ALTER TABLE law_version_sources
  ADD CONSTRAINT law_version_sources_official_uri_check
  CHECK (official_uri ~ '^https://www[.]law[.]go[.]kr/');
