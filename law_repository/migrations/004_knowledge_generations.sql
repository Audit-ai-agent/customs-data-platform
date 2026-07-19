-- Immutable cross-store knowledge generation ledger.
--
-- This migration is additive.  Existing law, HSK, CCCT, Qdrant, and Neo4j
-- data remains untouched until an explicitly validated generation is
-- promoted through the generation coordinator.

CREATE TABLE knowledge_generations (
  generation_id              text PRIMARY KEY
    CHECK (generation_id ~ '^[0-9a-f]{64}$'),
  state                      text NOT NULL DEFAULT 'building'
    CHECK (state IN ('building', 'validated', 'sealed', 'active', 'retired')),
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

  IF old_state IN ('sealed', 'active', 'retired')
     OR new_state IN ('sealed', 'active', 'retired') THEN
    RAISE EXCEPTION 'sealed knowledge generation child records are immutable';
  END IF;
  RETURN COALESCE(NEW, OLD);
END
$$;

CREATE TRIGGER trg_generation_laws_immutable_after_seal
BEFORE INSERT OR UPDATE OR DELETE ON knowledge_generation_laws
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
