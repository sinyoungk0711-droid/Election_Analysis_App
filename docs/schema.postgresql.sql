-- Generated initial schema. Protection triggers are installed by app.db.initialize.


CREATE TABLE api_idempotency (
	actor_id BIGINT NOT NULL, 
	key VARCHAR(80) NOT NULL, 
	method_path TEXT NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	response JSON NOT NULL, 
	http_status INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (actor_id, key, method_path)
)

;


CREATE TABLE app_user (
	user_id BIGSERIAL NOT NULL, 
	login_subject VARCHAR(200) NOT NULL, 
	display_name VARCHAR(80) NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revision_token INTEGER NOT NULL, 
	PRIMARY KEY (user_id), 
	UNIQUE (login_subject)
)

;


CREATE TABLE election (
	election_id BIGSERIAL NOT NULL, 
	election_no INTEGER NOT NULL, 
	election_year INTEGER NOT NULL, 
	election_date DATE NOT NULL, 
	title VARCHAR(120) NOT NULL, 
	PRIMARY KEY (election_id), 
	UNIQUE (election_no), 
	UNIQUE (election_year)
)

;


CREATE TABLE fact_check (
	fc_id BIGSERIAL NOT NULL, 
	fc_code VARCHAR(4) NOT NULL, 
	question TEXT NOT NULL, 
	replacement_note TEXT, 
	revision_token INTEGER NOT NULL, 
	PRIMARY KEY (fc_id), 
	UNIQUE (fc_code)
)

;


CREATE TABLE metric_definition (
	metric_id BIGSERIAL NOT NULL, 
	metric_code VARCHAR(40) NOT NULL, 
	definition_version INTEGER NOT NULL, 
	unit VARCHAR(15) NOT NULL, 
	formula TEXT NOT NULL, 
	null_policy TEXT NOT NULL, 
	PRIMARY KEY (metric_id), 
	UNIQUE (metric_code, definition_version)
)

;


CREATE TABLE region (
	region_id BIGSERIAL NOT NULL, 
	region_code VARCHAR(30) NOT NULL, 
	parent_id BIGINT, 
	name VARCHAR(100) NOT NULL, 
	region_level VARCHAR(15) NOT NULL, 
	PRIMARY KEY (region_id), 
	UNIQUE (region_code), 
	FOREIGN KEY(parent_id) REFERENCES region (region_id) ON DELETE RESTRICT
)

;


CREATE TABLE schema_version (
	version SERIAL NOT NULL, 
	PRIMARY KEY (version)
)

;


CREATE TABLE source_document (
	source_id BIGSERIAL NOT NULL, 
	title TEXT NOT NULL, 
	publisher VARCHAR(200) NOT NULL, 
	published_on DATE, 
	source_url TEXT, 
	usage_terms TEXT NOT NULL, 
	PRIMARY KEY (source_id)
)

;


CREATE TABLE access_token (
	token_hash VARCHAR(64) NOT NULL, 
	user_id BIGINT NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (token_hash), 
	FOREIGN KEY(user_id) REFERENCES app_user (user_id)
)

;


CREATE TABLE api_job (
	job_id VARCHAR(36) NOT NULL, 
	actor_id BIGINT NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	payload JSON NOT NULL, 
	result JSON, 
	error JSON, 
	progress INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	lease_owner VARCHAR(36), 
	attempts INTEGER NOT NULL, 
	PRIMARY KEY (job_id), 
	FOREIGN KEY(actor_id) REFERENCES app_user (user_id)
)

;


CREATE TABLE article (
	article_id BIGSERIAL NOT NULL, 
	slug VARCHAR(160) NOT NULL, 
	created_by BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revision_token INTEGER NOT NULL, 
	PRIMARY KEY (article_id), 
	UNIQUE (slug), 
	FOREIGN KEY(created_by) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE audit_event (
	audit_id BIGSERIAL NOT NULL, 
	actor_id BIGINT, 
	entity_type VARCHAR(60) NOT NULL, 
	entity_key TEXT NOT NULL, 
	action VARCHAR(25) NOT NULL, 
	before_after JSON NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (audit_id), 
	FOREIGN KEY(actor_id) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE chart (
	chart_id BIGSERIAL NOT NULL, 
	chart_key VARCHAR(60) NOT NULL, 
	version_no INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	chart_spec JSON NOT NULL, 
	created_by BIGINT NOT NULL, 
	PRIMARY KEY (chart_id), 
	FOREIGN KEY(created_by) REFERENCES app_user (user_id) ON DELETE RESTRICT, 
	UNIQUE (chart_key, version_no)
)

;


CREATE TABLE dataset_version (
	dataset_id BIGSERIAL NOT NULL, 
	version_no INTEGER NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	created_by BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	frozen_at TIMESTAMP WITH TIME ZONE, 
	revision_token INTEGER NOT NULL, 
	PRIMARY KEY (dataset_id), 
	UNIQUE (version_no), 
	FOREIGN KEY(created_by) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE publication (
	publication_id BIGSERIAL NOT NULL, 
	version_no INTEGER NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	report_uri TEXT, 
	journal_uri TEXT, 
	created_by BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	published_at TIMESTAMP WITH TIME ZONE, 
	correction_note TEXT, 
	revision_token INTEGER NOT NULL, 
	report_file_id BIGINT, 
	journal_file_id BIGINT, 
	PRIMARY KEY (publication_id), 
	UNIQUE (version_no), 
	FOREIGN KEY(created_by) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE search_log (
	search_id BIGSERIAL NOT NULL, 
	actor_id BIGINT NOT NULL, 
	query TEXT NOT NULL, 
	filters JSON NOT NULL, 
	result_refs JSON NOT NULL, 
	elapsed_ms INTEGER NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	usage JSON NOT NULL, 
	searched_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (search_id), 
	FOREIGN KEY(actor_id) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE source_file (
	file_id BIGSERIAL NOT NULL, 
	source_id BIGINT NOT NULL, 
	storage_uri TEXT NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	collected_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	collection_scope TEXT NOT NULL, 
	PRIMARY KEY (file_id), 
	FOREIGN KEY(source_id) REFERENCES source_document (source_id) ON DELETE RESTRICT
)

;


CREATE TABLE user_role (
	user_id BIGINT NOT NULL, 
	role_code VARCHAR(20) NOT NULL, 
	granted_by BIGINT NOT NULL, 
	granted_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (user_id, role_code), 
	FOREIGN KEY(user_id) REFERENCES app_user (user_id) ON DELETE RESTRICT, 
	FOREIGN KEY(granted_by) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE analysis_run (
	run_id BIGSERIAL NOT NULL, 
	dataset_id BIGINT NOT NULL, 
	code_ref TEXT NOT NULL, 
	parameters JSON NOT NULL, 
	executed_by BIGINT NOT NULL, 
	executed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	PRIMARY KEY (run_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(executed_by) REFERENCES app_user (user_id) ON DELETE RESTRICT
)

;


CREATE TABLE article_revision (
	revision_id BIGSERIAL NOT NULL, 
	article_id BIGINT NOT NULL, 
	version_no INTEGER NOT NULL, 
	title TEXT NOT NULL, 
	body TEXT NOT NULL, 
	created_by BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	change_note TEXT NOT NULL, 
	PRIMARY KEY (revision_id), 
	FOREIGN KEY(article_id) REFERENCES article (article_id) ON DELETE RESTRICT, 
	FOREIGN KEY(created_by) REFERENCES app_user (user_id) ON DELETE RESTRICT, 
	UNIQUE (article_id, version_no)
)

;


CREATE TABLE comparison_member (
	dataset_id BIGINT NOT NULL, 
	region_id BIGINT NOT NULL, 
	is_included BOOLEAN NOT NULL, 
	reason TEXT NOT NULL, 
	PRIMARY KEY (dataset_id, region_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(region_id) REFERENCES region (region_id) ON DELETE RESTRICT
)

;


CREATE TABLE evidence_chunk (
	evidence_id BIGSERIAL NOT NULL, 
	file_id BIGINT NOT NULL, 
	locator TEXT NOT NULL, 
	content TEXT NOT NULL, 
	is_sample BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (evidence_id), 
	FOREIGN KEY(file_id) REFERENCES source_file (file_id) ON DELETE RESTRICT
)

;


CREATE TABLE handover_file (
	handover_file_id BIGSERIAL NOT NULL, 
	publication_id BIGINT NOT NULL, 
	category VARCHAR(20) NOT NULL, 
	storage_uri TEXT NOT NULL, 
	sha256 VARCHAR(64) NOT NULL, 
	access_scope VARCHAR(15) NOT NULL, 
	verified_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (handover_file_id), 
	FOREIGN KEY(publication_id) REFERENCES publication (publication_id) ON DELETE RESTRICT, 
	UNIQUE (publication_id, storage_uri)
)

;


CREATE TABLE quality_issue (
	issue_id BIGSERIAL NOT NULL, 
	dataset_id BIGINT NOT NULL, 
	file_id BIGINT, 
	locator TEXT NOT NULL, 
	rule_code VARCHAR(40) NOT NULL, 
	details JSON NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	resolution TEXT, 
	PRIMARY KEY (issue_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(file_id) REFERENCES source_file (file_id) ON DELETE RESTRICT
)

;


CREATE TABLE region_mapping (
	mapping_id BIGSERIAL NOT NULL, 
	dataset_id BIGINT NOT NULL, 
	election_id BIGINT NOT NULL, 
	file_id BIGINT NOT NULL, 
	source_row_key TEXT NOT NULL, 
	original_region TEXT NOT NULL, 
	region_id BIGINT, 
	handling_note TEXT NOT NULL, 
	PRIMARY KEY (mapping_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(election_id) REFERENCES election (election_id) ON DELETE RESTRICT, 
	FOREIGN KEY(file_id) REFERENCES source_file (file_id) ON DELETE RESTRICT, 
	UNIQUE (dataset_id, election_id, file_id, source_row_key)
)

;


CREATE TABLE turnout_observation (
	observation_id BIGSERIAL NOT NULL, 
	dataset_id BIGINT NOT NULL, 
	election_id BIGINT NOT NULL, 
	region_id BIGINT NOT NULL, 
	eligible_voters BIGINT NOT NULL, 
	voters BIGINT NOT NULL, 
	official_turnout NUMERIC(9, 6), 
	official_decimals INTEGER, 
	calculated_turnout NUMERIC(12, 8) NOT NULL, 
	PRIMARY KEY (observation_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(election_id) REFERENCES election (election_id) ON DELETE RESTRICT, 
	FOREIGN KEY(region_id) REFERENCES region (region_id) ON DELETE RESTRICT, 
	UNIQUE (dataset_id, election_id, region_id), 
	CHECK (eligible_voters > 0 AND voters >= 0 AND voters <= eligible_voters), 
	CHECK (official_turnout IS NULL OR (official_turnout >= 0 AND official_turnout <= 100))
)

;


CREATE TABLE analysis_result (
	result_id BIGSERIAL NOT NULL, 
	run_id BIGINT NOT NULL, 
	metric_id BIGINT NOT NULL, 
	dimension_key TEXT NOT NULL, 
	election_id BIGINT, 
	region_id BIGINT, 
	value NUMERIC(20, 8), 
	context JSON NOT NULL, 
	PRIMARY KEY (result_id), 
	FOREIGN KEY(run_id) REFERENCES analysis_run (run_id) ON DELETE RESTRICT, 
	FOREIGN KEY(metric_id) REFERENCES metric_definition (metric_id) ON DELETE RESTRICT, 
	UNIQUE (run_id, metric_id, dimension_key)
)

;


CREATE TABLE claim (
	claim_id BIGSERIAL NOT NULL, 
	revision_id BIGINT NOT NULL, 
	claim_no INTEGER NOT NULL, 
	text TEXT NOT NULL, 
	body_locator TEXT NOT NULL, 
	is_core BOOLEAN NOT NULL, 
	is_numeric BOOLEAN NOT NULL, 
	PRIMARY KEY (claim_id), 
	FOREIGN KEY(revision_id) REFERENCES article_revision (revision_id) ON DELETE RESTRICT, 
	UNIQUE (revision_id, claim_no)
)

;


CREATE TABLE observation_lineage (
	observation_id BIGINT NOT NULL, 
	mapping_id BIGINT NOT NULL, 
	contribution_note TEXT NOT NULL, 
	PRIMARY KEY (observation_id, mapping_id), 
	FOREIGN KEY(observation_id) REFERENCES turnout_observation (observation_id) ON DELETE RESTRICT, 
	FOREIGN KEY(mapping_id) REFERENCES region_mapping (mapping_id) ON DELETE RESTRICT, 
	UNIQUE (mapping_id)
)

;


CREATE TABLE publication_item (
	publication_item_id BIGSERIAL NOT NULL, 
	publication_id BIGINT NOT NULL, 
	revision_id BIGINT, 
	chart_id BIGINT, 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (publication_item_id), 
	FOREIGN KEY(publication_id) REFERENCES publication (publication_id) ON DELETE RESTRICT, 
	FOREIGN KEY(revision_id) REFERENCES article_revision (revision_id) ON DELETE RESTRICT, 
	FOREIGN KEY(chart_id) REFERENCES chart (chart_id) ON DELETE RESTRICT, 
	UNIQUE (publication_id, display_order), 
	CHECK (CASE WHEN revision_id IS NULL THEN 0 ELSE 1 END + CASE WHEN chart_id IS NULL THEN 0 ELSE 1 END = 1)
)

;


CREATE TABLE rule_check (
	check_id BIGSERIAL NOT NULL, 
	revision_id BIGINT NOT NULL, 
	rule_version VARCHAR(60) NOT NULL, 
	category VARCHAR(15) NOT NULL, 
	body_locator TEXT NOT NULL, 
	status VARCHAR(15) NOT NULL, 
	details JSON NOT NULL, 
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (check_id), 
	FOREIGN KEY(revision_id) REFERENCES article_revision (revision_id) ON DELETE RESTRICT
)

;


CREATE TABLE chart_result (
	chart_id BIGINT NOT NULL, 
	result_id BIGINT NOT NULL, 
	display_order INTEGER NOT NULL, 
	PRIMARY KEY (chart_id, result_id), 
	FOREIGN KEY(chart_id) REFERENCES chart (chart_id) ON DELETE RESTRICT, 
	FOREIGN KEY(result_id) REFERENCES analysis_result (result_id) ON DELETE RESTRICT, 
	UNIQUE (chart_id, display_order)
)

;


CREATE TABLE claim_evidence (
	claim_evidence_id BIGSERIAL NOT NULL, 
	claim_id BIGINT NOT NULL, 
	evidence_id BIGINT, 
	result_id BIGINT, 
	relation_type VARCHAR(15) NOT NULL, 
	note TEXT NOT NULL, 
	PRIMARY KEY (claim_evidence_id), 
	FOREIGN KEY(claim_id) REFERENCES claim (claim_id) ON DELETE RESTRICT, 
	FOREIGN KEY(evidence_id) REFERENCES evidence_chunk (evidence_id) ON DELETE RESTRICT, 
	FOREIGN KEY(result_id) REFERENCES analysis_result (result_id) ON DELETE RESTRICT, 
	CHECK (CASE WHEN evidence_id IS NULL THEN 0 ELSE 1 END + CASE WHEN result_id IS NULL THEN 0 ELSE 1 END = 1)
)

;


CREATE TABLE fact_check_revision (
	fc_revision_id BIGSERIAL NOT NULL, 
	fc_id BIGINT NOT NULL, 
	version_no INTEGER NOT NULL, 
	claim_id BIGINT NOT NULL, 
	method TEXT NOT NULL, 
	finding TEXT NOT NULL, 
	verdict VARCHAR(20) NOT NULL, 
	checked_by BIGINT NOT NULL, 
	checked_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (fc_revision_id), 
	FOREIGN KEY(fc_id) REFERENCES fact_check (fc_id) ON DELETE RESTRICT, 
	FOREIGN KEY(claim_id) REFERENCES claim (claim_id) ON DELETE RESTRICT, 
	FOREIGN KEY(checked_by) REFERENCES app_user (user_id) ON DELETE RESTRICT, 
	UNIQUE (fc_id, version_no)
)

;


CREATE TABLE approval (
	approval_id BIGSERIAL NOT NULL, 
	dataset_id BIGINT, 
	run_id BIGINT, 
	fc_revision_id BIGINT, 
	revision_id BIGINT, 
	publication_id BIGINT, 
	reviewer_id BIGINT NOT NULL, 
	decision VARCHAR(15) NOT NULL, 
	review_note TEXT NOT NULL, 
	decided_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	snapshot_token INTEGER NOT NULL, 
	PRIMARY KEY (approval_id), 
	FOREIGN KEY(dataset_id) REFERENCES dataset_version (dataset_id) ON DELETE RESTRICT, 
	FOREIGN KEY(run_id) REFERENCES analysis_run (run_id) ON DELETE RESTRICT, 
	FOREIGN KEY(fc_revision_id) REFERENCES fact_check_revision (fc_revision_id) ON DELETE RESTRICT, 
	FOREIGN KEY(revision_id) REFERENCES article_revision (revision_id) ON DELETE RESTRICT, 
	FOREIGN KEY(publication_id) REFERENCES publication (publication_id) ON DELETE RESTRICT, 
	FOREIGN KEY(reviewer_id) REFERENCES app_user (user_id) ON DELETE RESTRICT, 
	CHECK (CASE WHEN dataset_id IS NULL THEN 0 ELSE 1 END + CASE WHEN run_id IS NULL THEN 0 ELSE 1 END + CASE WHEN fc_revision_id IS NULL THEN 0 ELSE 1 END + CASE WHEN revision_id IS NULL THEN 0 ELSE 1 END + CASE WHEN publication_id IS NULL THEN 0 ELSE 1 END = 1)
)

;