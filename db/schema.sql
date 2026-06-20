-- RobinHealth: Financial Assistance Policy (FAP) database schema
-- Postgres 14+

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- Health systems & facilities
-- ============================================================

CREATE TABLE health_systems (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    ein TEXT,
    is_nonprofit BOOLEAN NOT NULL DEFAULT TRUE,
    fap_url TEXT,
    mrf_url TEXT,                    -- machine-readable file URL (hospital price transparency)
    plain_language_summary_url TEXT,
    billing_collections_policy_url TEXT,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE facilities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    health_system_id UUID REFERENCES health_systems(id),
    name TEXT NOT NULL,
    npi TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip TEXT,
    tax_id TEXT,
    fap_id UUID, -- FK added below once financial_assistance_policies exists
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_facilities_npi ON facilities(npi);
CREATE INDEX idx_facilities_name_state ON facilities(name, state);

-- ============================================================
-- Financial Assistance Policies
-- ============================================================

CREATE TYPE fap_eligibility_basis AS ENUM (
    'fpl_percentage', 'flat_income', 'asset_test', 'combination'
);

CREATE TYPE fap_parsing_confidence AS ENUM (
    'high', 'medium', 'low', 'failed'
);

CREATE TYPE fap_document_quality AS ENUM (
    'well_structured', 'prose_with_data', 'vague_or_incomplete', 'not_found'
);

CREATE TABLE financial_assistance_policies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id UUID REFERENCES facilities(id),
    health_system_id UUID REFERENCES health_systems(id),
    effective_date DATE,
    source_url TEXT,
    plain_language_summary_url TEXT,
    billing_collections_policy_url TEXT,
    source_doc_hash TEXT,
    document_quality fap_document_quality NOT NULL DEFAULT 'not_found',
    eligibility_basis fap_eligibility_basis,
    parsed_at TIMESTAMPTZ,
    parsing_confidence fap_parsing_confidence,
    raw_text TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT fap_scope_check CHECK (
        facility_id IS NOT NULL OR health_system_id IS NOT NULL
    )
);

ALTER TABLE facilities
    ADD CONSTRAINT fk_facilities_fap
    FOREIGN KEY (fap_id) REFERENCES financial_assistance_policies(id);

CREATE INDEX idx_fap_facility ON financial_assistance_policies(facility_id);
CREATE INDEX idx_fap_health_system ON financial_assistance_policies(health_system_id);
CREATE INDEX idx_fap_active ON financial_assistance_policies(is_active) WHERE is_active;

-- ============================================================
-- Eligibility tiers (income -> discount mapping)
-- ============================================================

CREATE TYPE fap_discount_type AS ENUM (
    'full_charity_care', 'percentage_discount', 'sliding_scale', 'flat_cap'
);

CREATE TABLE fap_eligibility_tiers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fap_id UUID NOT NULL REFERENCES financial_assistance_policies(id) ON DELETE CASCADE,
    tier_order INT NOT NULL,
    fpl_min_pct INT,
    fpl_max_pct INT,
    discount_type fap_discount_type NOT NULL,
    discount_value NUMERIC, -- percent off, or dollar cap depending on discount_type
    household_size_adjustment JSONB, -- e.g. {"per_additional_member_fpl_add": 8000}
    notes TEXT,
    UNIQUE (fap_id, tier_order)
);

CREATE INDEX idx_fap_tiers_fap ON fap_eligibility_tiers(fap_id);

-- ============================================================
-- Service coverage exclusions/inclusions
-- ============================================================

CREATE TABLE fap_eligible_services (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fap_id UUID NOT NULL REFERENCES financial_assistance_policies(id) ON DELETE CASCADE,
    service_category TEXT NOT NULL, -- e.g. 'emergency', 'elective', 'cosmetic'
    is_covered BOOLEAN NOT NULL,
    notes TEXT
);

CREATE INDEX idx_fap_services_fap ON fap_eligible_services(fap_id);

-- ============================================================
-- Application / procedural requirements
-- ============================================================

CREATE TABLE fap_application_requirements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fap_id UUID NOT NULL REFERENCES financial_assistance_policies(id) ON DELETE CASCADE,
    application_deadline_days INT, -- from date of first post-discharge bill, per 501(r)
    required_documents JSONB, -- e.g. ["pay_stub", "tax_return", "bank_statement"]
    presumptive_eligibility_criteria JSONB, -- e.g. ["medicaid_enrolled", "snap_enrolled"]
    notification_method_required JSONB, -- e.g. ["on_bill", "in_ed", "posted_in_facility"]
    UNIQUE (fap_id) -- one row per FAP: EligibilityExtraction.application_requirements
                     -- is a single dict, not a list; nothing enforced that until now
);

CREATE INDEX idx_fap_requirements_fap ON fap_application_requirements(fap_id);

-- ============================================================
-- 501(r) compliance findings
-- ============================================================

CREATE TYPE fap_compliance_status AS ENUM (
    'present', 'vague', 'absent', 'contradicted'
);

CREATE TYPE fap_finding_severity AS ENUM (
    'procedural', 'material'
);

CREATE TABLE fap_compliance_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fap_id UUID NOT NULL REFERENCES financial_assistance_policies(id) ON DELETE CASCADE,
    requirement_code TEXT NOT NULL, -- canonical codes defined in compliance_checklist.py
    status fap_compliance_status NOT NULL,
    evidence_text TEXT,
    severity fap_finding_severity NOT NULL,
    argument_template TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_findings_fap ON fap_compliance_findings(fap_id);
CREATE INDEX idx_findings_severity ON fap_compliance_findings(severity);

-- ============================================================
-- Audit log / versioning
-- ============================================================

CREATE TABLE fap_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fap_id UUID NOT NULL REFERENCES financial_assistance_policies(id),
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_detected BOOLEAN NOT NULL DEFAULT FALSE,
    previous_version_id UUID REFERENCES financial_assistance_policies(id),
    notes TEXT
);

CREATE INDEX idx_audit_fap ON fap_audit_log(fap_id);
