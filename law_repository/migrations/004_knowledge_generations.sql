-- Immutable cross-store knowledge generation ledger.
--
-- This migration is additive.  Existing law, HSK, CCCT, Qdrant, and Neo4j
-- data remains untouched until an explicitly validated generation is
-- promoted through the generation coordinator.

DO $$
BEGIN
  IF to_regclass('public.law_articles') IS NULL
     OR to_regclass('public.laws') IS NULL
     OR to_regclass('public.knowledge_snapshots') IS NULL
     OR to_regclass('public.ccct_queries') IS NULL
     OR to_regclass('public.ccct_requirements') IS NULL
     OR to_regclass('public.ccct_source_records') IS NULL
     OR to_regclass('public.ccct_requirement_documents') IS NULL THEN
    RAISE EXCEPTION
      'knowledge generation migration requires completed migrations 001..003';
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema='public'
      AND table_name='law_articles'
      AND column_name='parent_article_pk'
  ) THEN
    RAISE EXCEPTION
      'knowledge generation migration requires 002_parent_article_pk.sql';
  END IF;
END
$$;

CREATE TABLE knowledge_generations (
  generation_id              text PRIMARY KEY
    CHECK (generation_id ~ '^[0-9a-f]{64}$'),
  state                      text NOT NULL DEFAULT 'building'
    CHECK (state IN ('building', 'aborted', 'validated', 'sealed', 'active', 'retired')),
  source_manifest            jsonb NOT NULL,
  source_manifest_sha256     text NOT NULL
    CHECK (source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  model_manifest             jsonb NOT NULL,
  model_manifest_sha256      text NOT NULL
    CHECK (model_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  index_config               jsonb NOT NULL,
  index_config_sha256        text NOT NULL
    CHECK (index_config_sha256 ~ '^[0-9a-f]{64}$'),
  evaluation_report          jsonb,
  evaluation_report_sha256   text
    CHECK (
      evaluation_report_sha256 IS NULL
      OR evaluation_report_sha256 ~ '^[0-9a-f]{64}$'
    ),
  human_reviewed_gold        boolean NOT NULL DEFAULT false,
  seal_attestation_sha256    text
    CHECK (
      seal_attestation_sha256 IS NULL
      OR seal_attestation_sha256 ~ '^[0-9a-f]{64}$'
    ),
  previous_generation_id     text REFERENCES knowledge_generations(generation_id),
  created_at                 timestamptz NOT NULL DEFAULT now(),
  validated_at               timestamptz,
  sealed_at                  timestamptz,
  activated_at               timestamptz,
  retired_at                 timestamptz,
  aborted_at                 timestamptz,
  CHECK (
    (evaluation_report IS NULL) =
    (evaluation_report_sha256 IS NULL)
  )
);

CREATE UNIQUE INDEX uq_knowledge_generations_single_active
  ON knowledge_generations ((state))
  WHERE state = 'active';

CREATE TABLE knowledge_generation_laws (
  generation_id          text NOT NULL
    REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
  law_id                 text NOT NULL CHECK (law_id ~ '^[0-9]{6}$'),
  law_name               text NOT NULL CHECK (btrim(law_name) <> ''),
  mst                    text NOT NULL CHECK (mst ~ '^[0-9]{1,20}$'),
  official_uri           text NOT NULL CHECK (official_uri LIKE 'https://%'),
  content_sha256         text NOT NULL
    CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  current_article_count  bigint NOT NULL CHECK (current_article_count > 0),
  PRIMARY KEY (generation_id, law_id)
);

CREATE TABLE knowledge_generation_datasets (
  generation_id            text NOT NULL
    REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
  dataset_id               text NOT NULL CHECK (dataset_id IN ('ccct', 'hsk')),
  snapshot_id              text NOT NULL CHECK (btrim(snapshot_id) <> ''),
  source_definition        jsonb NOT NULL,
  source_definition_sha256 text NOT NULL
    CHECK (source_definition_sha256 ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY (generation_id, dataset_id),
  UNIQUE (generation_id, dataset_id, snapshot_id)
);

CREATE TABLE knowledge_generation_dataset_members (
  generation_id  text NOT NULL,
  dataset_id     text NOT NULL,
  snapshot_id    text NOT NULL,
  member_kind    text NOT NULL
    CHECK (
      member_kind IN (
        'ccct_query',
        'ccct_requirement',
        'ccct_source_record',
        'ccct_document',
        'hsk_leaf'
      )
    ),
  member_key     text NOT NULL CHECK (btrim(member_key) <> ''),
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY (generation_id, dataset_id, member_kind, member_key),
  FOREIGN KEY (generation_id, dataset_id, snapshot_id)
    REFERENCES knowledge_generation_datasets(generation_id, dataset_id, snapshot_id)
    ON DELETE CASCADE,
  CHECK (
    (dataset_id = 'ccct' AND member_kind LIKE 'ccct_%')
    OR (dataset_id = 'hsk' AND member_kind = 'hsk_leaf')
  )
);

CREATE TABLE knowledge_generation_store_attestations (
  generation_id              text NOT NULL
    REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
  store_name                 text NOT NULL
    CHECK (store_name IN ('postgresql', 'qdrant', 'neo4j')),
  article_count              bigint NOT NULL CHECK (article_count >= 0),
  article_id_set_sha256      text NOT NULL
    CHECK (article_id_set_sha256 ~ '^[0-9a-f]{64}$'),
  article_content_set_sha256 text NOT NULL
    CHECK (article_content_set_sha256 ~ '^[0-9a-f]{64}$'),
  projection_sha256          text NOT NULL
    CHECK (projection_sha256 ~ '^[0-9a-f]{64}$'),
  model_manifest_sha256      text NOT NULL
    CHECK (model_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  store_locator              text NOT NULL CHECK (btrim(store_locator) <> ''),
  store_revision             text NOT NULL CHECK (btrim(store_revision) <> ''),
  attestation                jsonb NOT NULL,
  attestation_sha256         text NOT NULL
    CHECK (attestation_sha256 ~ '^[0-9a-f]{64}$'),
  attested_at                timestamptz NOT NULL,
  PRIMARY KEY (generation_id, store_name)
);

-- Immutable article membership for PostgreSQL attestation.  It references
-- legacy law_articles without modifying the legacy table or its semantics.
CREATE TABLE knowledge_generation_articles (
  generation_id   text NOT NULL
    REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
  article_pk      bigint NOT NULL REFERENCES law_articles(id),
  law_id          text NOT NULL CHECK (law_id ~ '^[0-9]{6}$'),
  article_no      int NOT NULL,
  paragraph_no    int,
  item_no         int,
  title           text,
  version_mst     text NOT NULL CHECK (version_mst ~ '^[0-9]{1,20}$'),
  valid_from      date NOT NULL,
  content         text NOT NULL CHECK (btrim(content) <> ''),
  content_sha256  text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  PRIMARY KEY (generation_id, article_pk)
);

CREATE TABLE knowledge_generation_active_pointer (
  singleton               boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  generation_id           text NOT NULL REFERENCES knowledge_generations(generation_id),
  previous_generation_id  text REFERENCES knowledge_generations(generation_id),
  epoch                   bigint NOT NULL CHECK (epoch > 0),
  changed_at              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_generation_events (
  event_id            bigserial PRIMARY KEY,
  generation_id       text NOT NULL
    REFERENCES knowledge_generations(generation_id) ON DELETE CASCADE,
  event_type          text NOT NULL
    CHECK (
      event_type IN (
        'created',
        'aborted',
        'validated',
        'sealed',
        'promoted',
        'retired',
        'rolled_back',
        'reconciled'
      )
    ),
  from_state          text,
  to_state            text,
  active_generation_id text,
  event_payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at         timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION enforce_knowledge_generation_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'INSERT' AND NEW.state <> 'building' THEN
    RAISE EXCEPTION 'knowledge generation must start in building state';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    IF OLD.generation_id <> NEW.generation_id
       OR OLD.source_manifest IS DISTINCT FROM NEW.source_manifest
       OR OLD.source_manifest_sha256 <> NEW.source_manifest_sha256
       OR OLD.model_manifest IS DISTINCT FROM NEW.model_manifest
       OR OLD.model_manifest_sha256 <> NEW.model_manifest_sha256
       OR OLD.index_config IS DISTINCT FROM NEW.index_config
       OR OLD.index_config_sha256 <> NEW.index_config_sha256 THEN
      RAISE EXCEPTION 'knowledge generation identity is immutable';
    END IF;

    IF OLD.state <> NEW.state AND NOT (
      (OLD.state = 'building'  AND NEW.state = 'validated')
      OR (OLD.state = 'building' AND NEW.state = 'aborted')
      OR (OLD.state = 'validated' AND NEW.state = 'sealed')
      OR (OLD.state = 'sealed' AND NEW.state = 'active')
      OR (OLD.state = 'active' AND NEW.state = 'retired')
      OR (OLD.state = 'retired' AND NEW.state = 'active')
    ) THEN
      RAISE EXCEPTION 'invalid knowledge generation transition: % -> %',
        OLD.state, NEW.state;
    END IF;

    IF OLD.state IN ('sealed', 'active', 'retired')
       AND (
         OLD.evaluation_report IS DISTINCT FROM NEW.evaluation_report
         OR OLD.evaluation_report_sha256 IS DISTINCT FROM NEW.evaluation_report_sha256
         OR OLD.human_reviewed_gold <> NEW.human_reviewed_gold
         OR OLD.seal_attestation_sha256 IS DISTINCT FROM NEW.seal_attestation_sha256
       ) THEN
      RAISE EXCEPTION 'sealed knowledge generation evidence is immutable';
    END IF;

    IF OLD.state IN ('sealed', 'active', 'retired')
       AND OLD.state = NEW.state
       AND OLD IS DISTINCT FROM NEW THEN
      RAISE EXCEPTION 'sealed knowledge generation row cannot be mutated in place';
    END IF;
  END IF;

  IF TG_OP = 'DELETE' AND OLD.state IN ('sealed', 'active', 'retired') THEN
    RAISE EXCEPTION 'sealed knowledge generation cannot be deleted';
  END IF;
  RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER trg_knowledge_generation_transition
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generations
FOR EACH ROW EXECUTE FUNCTION enforce_knowledge_generation_transition();

CREATE OR REPLACE FUNCTION enforce_generation_state_preconditions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  law_count bigint;
  article_law_count bigint;
  dataset_count bigint;
  attestation_count bigint;
BEGIN
  IF OLD.state = NEW.state THEN
    RETURN NEW;
  END IF;

  IF NEW.state = 'validated' THEN
    SELECT count(*) INTO law_count
    FROM knowledge_generation_laws
    WHERE generation_id = NEW.generation_id;
    SELECT count(DISTINCT law_id) INTO article_law_count
    FROM knowledge_generation_articles
    WHERE generation_id = NEW.generation_id;
    SELECT count(*) INTO dataset_count
    FROM knowledge_generation_datasets
    WHERE generation_id = NEW.generation_id;
    IF law_count <> 58 OR article_law_count <> 58 OR dataset_count <> 2 THEN
      RAISE EXCEPTION
        'validated generation requires exact 58-law and 2-dataset snapshots';
    END IF;
    IF EXISTS (
      SELECT 1
      FROM knowledge_generation_laws gl
      LEFT JOIN (
        SELECT law_id, min(version_mst) AS min_mst,
               max(version_mst) AS max_mst, count(*) AS article_count
        FROM knowledge_generation_articles
        WHERE generation_id=NEW.generation_id
        GROUP BY law_id
      ) a USING (law_id)
      WHERE gl.generation_id=NEW.generation_id
        AND (
          a.law_id IS NULL
          OR a.min_mst <> gl.mst
          OR a.max_mst <> gl.mst
          OR a.article_count <> gl.current_article_count
        )
    ) THEN
      RAISE EXCEPTION
        'validated generation law MST/article snapshots do not match manifest';
    END IF;
    IF (
      SELECT count(*) FROM knowledge_generation_dataset_members
      WHERE generation_id=NEW.generation_id AND member_kind='ccct_query'
    ) <> 22654
       OR (
         SELECT count(*) FROM knowledge_generation_dataset_members
         WHERE generation_id=NEW.generation_id AND member_kind='ccct_requirement'
       ) <> 14726
       OR (
         SELECT count(*) FROM knowledge_generation_dataset_members
         WHERE generation_id=NEW.generation_id AND member_kind='ccct_source_record'
       ) <> 14726
       OR (
         SELECT count(*) FROM knowledge_generation_dataset_members
         WHERE generation_id=NEW.generation_id AND member_kind='ccct_document'
       ) <> 63
       OR (
         SELECT count(*) FROM knowledge_generation_dataset_members
         WHERE generation_id=NEW.generation_id AND member_kind='hsk_leaf'
       ) <> 11327 THEN
      RAISE EXCEPTION 'validated generation dataset membership is incomplete';
    END IF;
    IF EXISTS (
      (
        SELECT (q.hsk10::text || ':' || q.trade_type)
        FROM ccct_queries q
        JOIN knowledge_generation_datasets d
          ON d.generation_id=NEW.generation_id
         AND d.dataset_id='ccct'
         AND d.snapshot_id=q.snapshot_id
        EXCEPT
        SELECT m.member_key
        FROM knowledge_generation_dataset_members m
        WHERE m.generation_id=NEW.generation_id
          AND m.member_kind='ccct_query'
      )
      UNION ALL
      (
        SELECT m.member_key
        FROM knowledge_generation_dataset_members m
        WHERE m.generation_id=NEW.generation_id
          AND m.member_kind='ccct_query'
        EXCEPT
        SELECT (q.hsk10::text || ':' || q.trade_type)
        FROM ccct_queries q
        JOIN knowledge_generation_datasets d
          ON d.generation_id=NEW.generation_id
         AND d.dataset_id='ccct'
         AND d.snapshot_id=q.snapshot_id
      )
    ) THEN
      RAISE EXCEPTION 'CCCT query membership does not match linked snapshot';
    END IF;
    IF EXISTS (
      WITH expected(kind, member_key) AS (
        SELECT 'ccct_requirement', r.requirement_id
        FROM ccct_requirements r
        JOIN knowledge_generation_datasets d
          ON d.generation_id=NEW.generation_id
         AND d.dataset_id='ccct'
         AND d.snapshot_id=r.snapshot_id
        UNION ALL
        SELECT 'ccct_source_record', s.source_record_id
        FROM ccct_source_records s
        JOIN knowledge_generation_datasets d
          ON d.generation_id=NEW.generation_id
         AND d.dataset_id='ccct'
         AND d.snapshot_id=s.snapshot_id
        UNION
        SELECT 'ccct_document', doc.document_code
        FROM ccct_requirement_documents doc
        JOIN ccct_requirements r USING (document_code)
        JOIN knowledge_generation_datasets d
          ON d.generation_id=NEW.generation_id
         AND d.dataset_id='ccct'
         AND d.snapshot_id=r.snapshot_id
      ), actual(kind, member_key) AS (
        SELECT member_kind, member_key
        FROM knowledge_generation_dataset_members
        WHERE generation_id=NEW.generation_id
          AND member_kind IN (
            'ccct_requirement',
            'ccct_source_record',
            'ccct_document'
          )
      )
      (SELECT * FROM expected EXCEPT SELECT * FROM actual)
      UNION ALL
      (SELECT * FROM actual EXCEPT SELECT * FROM expected)
    ) THEN
      RAISE EXCEPTION 'CCCT row membership does not match linked snapshot';
    END IF;
  END IF;

  IF NEW.state = 'sealed' THEN
    SELECT count(*) INTO attestation_count
    FROM knowledge_generation_store_attestations
    WHERE generation_id = NEW.generation_id;
    IF attestation_count <> 3
       OR NEW.evaluation_report IS NULL
       OR NEW.evaluation_report_sha256 IS NULL
       OR NEW.human_reviewed_gold IS NOT TRUE
       OR NEW.seal_attestation_sha256 IS NULL THEN
      RAISE EXCEPTION 'sealed generation evidence is incomplete';
    END IF;
  END IF;

  IF NEW.state = 'active'
     AND (
       NEW.evaluation_report_sha256 IS NULL
       OR NEW.human_reviewed_gold IS NOT TRUE
       OR NEW.seal_attestation_sha256 IS NULL
     ) THEN
    RAISE EXCEPTION 'active generation must retain sealed release evidence';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER trg_generation_state_preconditions
BEFORE UPDATE ON knowledge_generations
FOR EACH ROW EXECUTE FUNCTION enforce_generation_state_preconditions();

CREATE OR REPLACE FUNCTION reject_sealed_generation_child_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  old_state text;
  new_state text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    SELECT state INTO old_state
    FROM knowledge_generations
    WHERE generation_id = OLD.generation_id;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    SELECT state INTO new_state
    FROM knowledge_generations
    WHERE generation_id = NEW.generation_id;
  END IF;

  IF old_state IN ('validated', 'sealed', 'active', 'retired', 'aborted')
     OR new_state IN ('validated', 'sealed', 'active', 'retired', 'aborted') THEN
    RAISE EXCEPTION 'non-building knowledge generation child records are immutable';
  END IF;
  RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER trg_generation_laws_immutable_after_seal
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_laws
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

CREATE TRIGGER trg_generation_datasets_immutable_after_validation
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_datasets
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

CREATE TRIGGER trg_generation_dataset_members_immutable_after_validation
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_dataset_members
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

CREATE OR REPLACE FUNCTION verify_generation_article_membership()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  ledger_record record;
BEGIN
  SELECT a.law_id, a.article_no, a.paragraph_no, a.item_no, a.title,
         a.version_mst, a.valid_from, a.content, a.content_hash
  INTO ledger_record
  FROM law_articles a
  WHERE a.id = NEW.article_pk
    AND a.is_current
    AND NOT EXISTS (
      SELECT 1 FROM law_articles child WHERE child.parent_article_pk = a.id
    );

  IF ledger_record IS NULL THEN
    RAISE EXCEPTION
      'generation article must reference a current indexable law article: %',
      NEW.article_pk;
  END IF;
  IF ROW(
       ledger_record.law_id,
       ledger_record.article_no,
       ledger_record.paragraph_no,
       ledger_record.item_no,
       ledger_record.title,
       ledger_record.version_mst,
       ledger_record.valid_from,
       ledger_record.content,
       ledger_record.content_hash
     ) IS DISTINCT FROM ROW(
       NEW.law_id,
       NEW.article_no,
       NEW.paragraph_no,
       NEW.item_no,
       NEW.title,
       NEW.version_mst,
       NEW.valid_from,
       NEW.content,
       NEW.content_sha256
     ) THEN
    RAISE EXCEPTION 'generation article snapshot does not match PostgreSQL ledger';
  END IF;
  RETURN NEW;
END
$$;

CREATE TRIGGER trg_generation_article_matches_ledger
BEFORE INSERT OR UPDATE ON knowledge_generation_articles
FOR EACH ROW EXECUTE FUNCTION verify_generation_article_membership();

CREATE TRIGGER trg_generation_articles_immutable_after_seal
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_articles
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

CREATE TRIGGER trg_generation_attestations_immutable_after_seal
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_store_attestations
FOR EACH ROW EXECUTE FUNCTION reject_sealed_generation_child_mutation();

CREATE OR REPLACE FUNCTION reject_generation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'knowledge generation events are append-only';
END
$$;

CREATE TRIGGER trg_generation_events_append_only
BEFORE UPDATE OR DELETE ON knowledge_generation_events
FOR EACH ROW EXECUTE FUNCTION reject_generation_event_mutation();

CREATE OR REPLACE FUNCTION reject_frozen_ccct_snapshot_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  old_snapshot text;
  new_snapshot text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    old_snapshot := OLD.snapshot_id;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    new_snapshot := NEW.snapshot_id;
  END IF;
  IF EXISTS (
       SELECT 1
       FROM knowledge_generation_datasets d
       JOIN knowledge_generations g USING (generation_id)
       WHERE d.dataset_id='ccct'
         AND d.snapshot_id IN (old_snapshot, new_snapshot)
         AND g.state IN ('validated', 'sealed', 'active', 'retired')
     ) THEN
    RAISE EXCEPTION 'CCCT snapshot linked to a release generation is immutable';
  END IF;
  RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER trg_frozen_ccct_snapshot
BEFORE UPDATE OR DELETE ON knowledge_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_frozen_ccct_snapshot_mutation();

CREATE TRIGGER trg_frozen_ccct_queries
BEFORE INSERT OR UPDATE OR DELETE ON ccct_queries
FOR EACH ROW EXECUTE FUNCTION reject_frozen_ccct_snapshot_mutation();

CREATE TRIGGER trg_frozen_ccct_requirements
BEFORE INSERT OR UPDATE OR DELETE ON ccct_requirements
FOR EACH ROW EXECUTE FUNCTION reject_frozen_ccct_snapshot_mutation();

CREATE TRIGGER trg_frozen_ccct_source_records
BEFORE INSERT OR UPDATE OR DELETE ON ccct_source_records
FOR EACH ROW EXECUTE FUNCTION reject_frozen_ccct_snapshot_mutation();

CREATE OR REPLACE FUNCTION reject_frozen_ccct_document_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  old_code text;
  new_code text;
BEGIN
  IF TG_OP IN ('UPDATE', 'DELETE') THEN
    old_code := OLD.document_code;
  END IF;
  IF TG_OP IN ('INSERT', 'UPDATE') THEN
    new_code := NEW.document_code;
  END IF;
  IF EXISTS (
       SELECT 1
       FROM ccct_requirements r
       JOIN knowledge_generation_datasets d
         ON d.dataset_id='ccct' AND d.snapshot_id=r.snapshot_id
       JOIN knowledge_generations g USING (generation_id)
       WHERE r.document_code IN (old_code, new_code)
         AND g.state IN ('validated', 'sealed', 'active', 'retired')
     ) THEN
    RAISE EXCEPTION 'CCCT document linked to a release generation is immutable';
  END IF;
  RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER trg_frozen_ccct_documents
BEFORE INSERT OR UPDATE OR DELETE ON ccct_requirement_documents
FOR EACH ROW EXECUTE FUNCTION reject_frozen_ccct_document_mutation();
