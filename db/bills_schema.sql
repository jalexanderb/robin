-- RobinHealth: bill ingestion schema
-- Depends on schema.sql (specifically the `facilities` table) being applied first
-- Postgres 14+

-- ============================================================
-- Patients
-- ============================================================

CREATE TABLE patients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    household_income NUMERIC,
    household_size INT,
    state TEXT, -- two-letter state code; used for FPL lookup (AK/HI differ)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- Cases
-- ============================================================

CREATE TYPE case_status AS ENUM (
    'intake', 'reviewing', 'awaiting_user_input', 'ready_for_action',
    'negotiating', 'resolved'
);

CREATE TABLE cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES patients(id),
    status case_status NOT NULL DEFAULT 'intake',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_cases_patient ON cases(patient_id);

-- ============================================================
-- Bills
-- ============================================================

CREATE TYPE bill_parsing_confidence AS ENUM ('high', 'medium', 'low', 'failed');

CREATE TYPE bill_facility_match_status AS ENUM (
    'matched', 'new_facility_created', 'unmatched'
);

CREATE TABLE bills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL, -- reference to the uploaded file in object storage
    facility_id UUID REFERENCES facilities(id),
    facility_match_status bill_facility_match_status,
    facility_match_confidence NUMERIC, -- 0-1, from name-similarity scoring
    provider_name_raw TEXT,
    provider_npi_raw TEXT,
    provider_address_raw TEXT,
    account_number TEXT,
    date_of_service DATE,
    total_billed_amount NUMERIC,
    parsed_at TIMESTAMPTZ,
    parsing_confidence bill_parsing_confidence,
    raw_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_bills_case ON bills(case_id);
CREATE INDEX idx_bills_facility ON bills(facility_id);

-- ============================================================
-- Bill line items
-- ============================================================

CREATE TYPE bill_code_type AS ENUM ('cpt', 'hcpcs', 'revenue_code', 'ndc', 'unknown');

CREATE TABLE bill_line_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id UUID NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    line_number INT NOT NULL,
    description TEXT NOT NULL,
    procedure_code TEXT,
    code_type bill_code_type,
    units NUMERIC,
    billed_amount NUMERIC NOT NULL,
    allowed_amount NUMERIC, -- from an EOB, if/when one is uploaded (future EOB pipeline)
    patient_responsibility NUMERIC, -- from an EOB, if/when one is uploaded
    UNIQUE (bill_id, line_number)
);

CREATE INDEX idx_line_items_bill ON bill_line_items(bill_id);
CREATE INDEX idx_line_items_code ON bill_line_items(procedure_code);

-- ============================================================
-- Explanation of Benefits (EOB)
-- ============================================================
-- An EOB is the document an insurer sends after processing a claim.
-- It shows what the insurer allowed (contract rate), what they paid,
-- and what the patient owes. This is the single most important document
-- for negotiation: "your insurer contracted at $X; please extend the
-- same rate to me as an uninsured/underinsured patient."
--
-- One bill may have multiple EOBs (claims split, resubmitted, appealed).
-- One EOB may partially cover a bill (insurer only processed some dates).

CREATE TYPE eob_parsing_confidence AS ENUM ('high', 'medium', 'low', 'failed');

CREATE TYPE eob_line_match_status AS ENUM (
    'matched',        -- confidently linked to a bill_line_items row
    'partial_match',  -- likely the same service but code/amount diverges
    'unmatched'       -- no corresponding bill line found
);

CREATE TABLE eobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    bill_id UUID NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    storage_key TEXT NOT NULL,
    insurer_name TEXT,
    member_id TEXT,
    claim_number TEXT,
    date_processed DATE,
    total_billed_amount NUMERIC,
    total_allowed_amount NUMERIC,
    total_insurance_paid NUMERIC,
    total_patient_responsibility NUMERIC,
    parsed_at TIMESTAMPTZ,
    parsing_confidence eob_parsing_confidence,
    raw_text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_eobs_bill ON eobs(bill_id);

CREATE TABLE eob_line_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    eob_id UUID NOT NULL REFERENCES eobs(id) ON DELETE CASCADE,
    bill_line_item_id UUID REFERENCES bill_line_items(id) ON DELETE SET NULL,
    line_number INT NOT NULL,
    date_of_service DATE,
    description TEXT,
    procedure_code TEXT,
    code_type bill_code_type,
    units NUMERIC,
    billed_amount NUMERIC,
    allowed_amount NUMERIC,
    insurance_paid NUMERIC,
    patient_responsibility NUMERIC,
    match_status eob_line_match_status NOT NULL DEFAULT 'unmatched',
    UNIQUE (eob_id, line_number)
);

CREATE INDEX idx_eob_lines_eob ON eob_line_items(eob_id);
CREATE INDEX idx_eob_lines_bill_line ON eob_line_items(bill_line_item_id);

CREATE TABLE eob_adjustment_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    eob_line_item_id UUID NOT NULL REFERENCES eob_line_items(id) ON DELETE CASCADE,
    code_type TEXT NOT NULL CHECK (code_type IN ('CARC', 'RARC')),
    code TEXT NOT NULL,
    amount NUMERIC,
    description TEXT
);

-- ============================================================
-- MRF (Machine-Readable File) findings
-- ============================================================
-- Results of fetching a hospital's CMS price-transparency MRF
-- for specific procedure codes. Stored per-facility so repeated
-- bill uploads from the same hospital reuse the cached result
-- until last_checked_at expires.
--
-- mrf_status values:
--   rates_found            -- MRF fetched, codes matched, real dollar amounts present
--   codes_not_in_mrf       -- MRF fetched successfully but the specific codes aren't listed
--   mrf_unpopulated        -- MRF present but rates are blank/zero/placeholder for these codes
--   mrf_unreachable        -- URL exists but fetch failed (network error, 4xx/5xx)
--   mrf_url_unknown        -- No MRF URL on file for this facility's health system

CREATE TYPE mrf_status AS ENUM (
    'rates_found',
    'codes_not_in_mrf',
    'mrf_unpopulated',
    'mrf_unreachable',
    'mrf_url_unknown'
);

CREATE TABLE mrf_findings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    facility_id UUID NOT NULL REFERENCES facilities(id) ON DELETE CASCADE,
    mrf_url TEXT,               -- the URL that was (or would have been) fetched
    mrf_status mrf_status NOT NULL,
    status_detail TEXT,         -- human-readable explanation for the status
    codes_queried JSONB,        -- ["99213", "71046"] -- what we looked for
    rates JSONB,                -- {"99213": {"gross": 500, "cash": 280, "min_negotiated": 120, "max_negotiated": 350}}
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_mrf_findings_facility ON mrf_findings(facility_id);
CREATE INDEX idx_mrf_findings_status ON mrf_findings(mrf_status);

-- ============================================================
-- Negotiation tracking & outcome recording
-- ============================================================
-- Captures the full lifecycle of a negotiation: from first outreach
-- through provider response to final settlement. This is the data that
-- makes the "20% of savings" business model auditable -- every dollar
-- of savings claimed must trace back to a recorded outcome here.

CREATE TYPE negotiation_status AS ENUM (
    'pending',          -- letter/contact ready but not yet sent
    'contacted',        -- at least one outreach attempt made
    'provider_replied', -- provider has responded (any response)
    'counter_offer',    -- provider offered a reduced but not agreed amount
    'agreed',           -- both parties have agreed on a final amount
    'paid',             -- patient has paid the agreed amount
    'rejected',         -- provider refused any reduction
    'withdrawn'         -- patient decided not to pursue
);

CREATE TYPE contact_channel AS ENUM (
    'letter_mail',    -- physical letter sent by mail
    'letter_fax',     -- letter sent by fax
    'letter_email',   -- letter sent by email
    'phone_call',     -- phone call to billing department
    'patient_portal', -- submitted via provider's online portal
    'in_person'       -- visited billing office directly
);

CREATE TABLE negotiations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    -- The amounts we're working with
    original_billed_amount NUMERIC NOT NULL,  -- from bills.total_billed_amount
    target_amount NUMERIC,       -- what we're asking for (from synthesis estimate)
    -- Current negotiation state
    status negotiation_status NOT NULL DEFAULT 'pending',
    provider_response_text TEXT, -- free-text of what provider said/wrote
    counter_offer_amount NUMERIC, -- provider's counter if they didn't accept target
    -- Final outcome (populated when status = 'agreed' or 'paid')
    agreed_amount NUMERIC,
    amount_saved NUMERIC GENERATED ALWAYS AS (
        CASE WHEN agreed_amount IS NOT NULL
             THEN original_billed_amount - agreed_amount
             ELSE NULL
        END
    ) STORED,
    robinhealth_fee NUMERIC GENERATED ALWAYS AS (
        -- 20% of savings, the business model fee
        CASE WHEN agreed_amount IS NOT NULL
             THEN ROUND((original_billed_amount - agreed_amount) * 0.20, 2)
             ELSE NULL
        END
    ) STORED,
    patient_net_savings NUMERIC GENERATED ALWAYS AS (
        -- What the patient actually saves after paying RobinHealth's fee
        CASE WHEN agreed_amount IS NOT NULL
             THEN ROUND((original_billed_amount - agreed_amount) * 0.80, 2)
             ELSE NULL
        END
    ) STORED,
    -- Timing
    first_contacted_at TIMESTAMPTZ,
    agreed_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id)  -- one active negotiation per case
);

CREATE TABLE negotiation_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    negotiation_id UUID NOT NULL REFERENCES negotiations(id) ON DELETE CASCADE,
    channel contact_channel NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- What was sent / said
    letter_storage_key TEXT,   -- storage key of the letter file, if applicable
    notes TEXT,                 -- free-form notes (e.g. "spoke to Janet in billing")
    -- Provider response to this specific contact (may come later)
    provider_responded_at TIMESTAMPTZ,
    provider_response TEXT
);

CREATE INDEX idx_negotiations_case ON negotiations(case_id);
CREATE INDEX idx_negotiations_status ON negotiations(status);
CREATE INDEX idx_contacts_negotiation ON negotiation_contacts(negotiation_id);

-- Provider response types for structured follow-up routing.
-- Added to negotiation_contacts to capture what the provider actually said,
-- not just a free-text blob -- this is what drives the follow-up action.
CREATE TYPE provider_response_type AS ENUM (
    'reduced_offer',        -- provider offered less than billed, more than target
    'accepted_target',      -- provider accepted our requested amount
    'denied_eligibility',   -- "patient doesn't qualify for charity care"
    'requested_more_info',  -- "please provide income documentation"
    'referred_to_collections', -- account sent to collections agency
    'claimed_no_fap',       -- "we don't have a financial assistance program"
    'billing_error',        -- provider acknowledges billing was incorrect
    'insurance_issue',      -- "this should have been covered by your insurance"
    'no_response',          -- no reply after response_deadline_days
    'other'                 -- anything else; use notes for details
);

ALTER TABLE negotiation_contacts
    ADD COLUMN IF NOT EXISTS response_type provider_response_type,
    ADD COLUMN IF NOT EXISTS response_data JSONB;
-- response_data holds structured details per type, e.g.:
--   reduced_offer:       {"offered_amount": 1800.0}
--   denied_eligibility:  {"reason_cited": "income above 200% FPL"}
--   requested_more_info: {"documents_requested": ["tax_return", "pay_stub"]}
--   referred_to_collections: {"agency_name": "Acme Collections", "account_ref": "AC-123"}

-- Track which round of the negotiation we're in (1 = first letter, 2 = follow-up, etc.)
ALTER TABLE negotiation_contacts
    ADD COLUMN IF NOT EXISTS round_number INT NOT NULL DEFAULT 1;

-- negotiation_rounds view for easy querying of "what's the latest round?"
CREATE OR REPLACE VIEW latest_negotiation_round AS
SELECT DISTINCT ON (negotiation_id)
    negotiation_id,
    round_number,
    channel,
    response_type,
    response_data,
    provider_responded_at,
    provider_response,
    sent_at
FROM negotiation_contacts
ORDER BY negotiation_id, round_number DESC, sent_at DESC;

-- Fee agreement: stored on the patient, not the case. A patient who has
-- agreed once doesn't need to re-agree for every bill they upload.
-- The exact terms text is stored so the agreement remains auditable even
-- if the fee percentage or terms change in the future.
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS fee_agreement_accepted BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS fee_agreement_accepted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS fee_agreement_terms_version TEXT,
    ADD COLUMN IF NOT EXISTS fee_agreement_terms_text TEXT;
-- fee_agreement_terms_version: e.g. "v1.0" -- lets us track which version
-- of the terms they agreed to if the fee structure ever changes
-- fee_agreement_terms_text: the exact text shown to them at agreement time

-- Billing plan the patient chose. 'contingency' = 20% of savings, capped at
-- $1,000, nothing if we save nothing. 'membership' = $50/month flat and we
-- take 0% of savings. The patient picks whichever costs them less; the fee
-- RobinHealth takes out of savings is computed in outcome_pipeline by plan.
ALTER TABLE patients
    ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'contingency';

-- Persisted synthesis (savings estimate + reasons) for a case, so the analysis
-- can be restored when a patient resumes -- synthesis is otherwise computed
-- in-memory at intake and never stored. Serialized SynthesisResult as JSONB.
ALTER TABLE cases
    ADD COLUMN IF NOT EXISTS synthesis_json JSONB;
