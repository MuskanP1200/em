import yaml
from pathlib import Path

config = yaml.safe_load(open(Path(__file__).resolve().parent / "config.yaml"))

schema = config["tables"]["schema"]

# Staging (raw API output)
raw_table = config["tables"]["staging"]["est_raw"]
line_table = config["tables"]["staging"]["est_line"]
subtot_table = config["tables"]["staging"]["est_subtot"]

# Vehicle ID output
vi_est_results = config["tables"]["vi_output"]["folders_table"]
vi_image_results = config["tables"]["vi_output"]["images_table"]

# Estimate matching output
em_line_detail = config["tables"]["em_output"]["line_detail"]
em_subtot_detail = config["tables"]["em_output"]["subtot_detail"]
em_summary_table = config["tables"]["em_output"]["est_summary"]
em_overall_results = config["tables"]["em_output"]["overall_summary"]

# ── Incident list ────────────────────────────────────────────────
# sub_text is ar.vin for now — replace ar.vin with the real vehicle TODO
# name column when it becomes available, everything else stays the same. TODO

LIST_QUERY = f"""
SELECT es.est_id::bigint::text as est_id       --  TODO check if its claimid or est_id
        ,es.overall_match as overall_estimate_match   -- TODO currently its est_match shouldbe overall
        ,ar.vin                                   -- TODO change to make or model
        ,ar.claim_number                           -- TODO change to make or model
FROM {schema}.{em_overall_results} es
LEFT JOIN  {schema}.{raw_table} ar
    ON es.est_id::text = ar.est_id::text
ORDER BY es.update_timestamp DESC;
"""


# Fetches the folder path and the 3 best-match image filenames.

# IMAGES_QUERY = f"""
# SELECT
#     image_path,
#     CASE
#         WHEN COALESCE(best_match_vin_ocr,      best_match_vin_vlm)      IS NOT NULL THEN 'vin'
#         WHEN COALESCE(best_match_plate_ocr,    best_match_plate_vlm)    IS NOT NULL THEN 'plate'
#         WHEN COALESCE(best_match_odometer_ocr, best_match_odometer_vlm) IS NOT NULL THEN 'odo'
#         ELSE 'others'
#     END AS category
# FROM {schema}.{vi_image_results}
# WHERE folder_name = 'EST' || $1::text
#   AND image_path IS NOT NULL
#   AND image_path NOT ILIKE '%.tmp.jpeg'
# ORDER BY
#     CASE
#         WHEN COALESCE(best_match_vin_ocr,      best_match_vin_vlm)      IS NOT NULL THEN 1
#         WHEN COALESCE(best_match_plate_ocr,    best_match_plate_vlm)    IS NOT NULL THEN 2
#         WHEN COALESCE(best_match_odometer_ocr, best_match_odometer_vlm) IS NOT NULL THEN 3
#         ELSE 4
#     END,
#     image_path;
# """


IMAGES_QUERY = f"""
SELECT
    vi.image_path,
    CASE
        WHEN COALESCE(vi.best_match_vin_ocr,      vi.best_match_vin_vlm)      IS NOT NULL THEN 'vin'
        WHEN COALESCE(vi.best_match_plate_ocr,    vi.best_match_plate_vlm)    IS NOT NULL THEN 'plate'
        WHEN COALESCE(vi.best_match_odometer_ocr, vi.best_match_odometer_vlm) IS NOT NULL THEN 'odo'
        ELSE 'others'
    END AS category,
    CASE
        WHEN COALESCE(vi.best_match_vin_ocr,      vi.best_match_vin_vlm)      = vf.est_best_match_vin       THEN true
        WHEN COALESCE(vi.best_match_plate_ocr,    vi.best_match_plate_vlm)    = vf.est_best_match_plate     THEN true
        WHEN COALESCE(vi.best_match_odometer_ocr, vi.best_match_odometer_vlm) = vf.est_best_match_odometer  THEN true
        ELSE false
    END AS is_best_match
FROM {schema}.{vi_image_results} vi
LEFT JOIN {schema}.{vi_est_results} vf
       ON vi.folder_name = vf.folder_name
WHERE vi.folder_name = 'EST' || $1::text
  AND vi.image_path IS NOT NULL
  AND vi.image_path NOT ILIKE '%.tmp.jpeg'
ORDER BY
    CASE
        WHEN COALESCE(vi.best_match_vin_ocr,      vi.best_match_vin_vlm)      IS NOT NULL THEN 1
        WHEN COALESCE(vi.best_match_plate_ocr,    vi.best_match_plate_vlm)    IS NOT NULL THEN 2
        WHEN COALESCE(vi.best_match_odometer_ocr, vi.best_match_odometer_vlm) IS NOT NULL THEN 3
        ELSE 4
    END,
    is_best_match DESC,        -- ← best match first within each category
    vi.image_path;
"""

# ── Incident Level information ────────────────────────────────────────────────
CORE_QUERY = f"""
WITH est_base AS (
    -- Master estimate record (raw staging table)
    SELECT
        ar.est_id,
        ar.repr_incident_id,
        ar.claim_number,
        ar.created_date,
        ar.vendor_id,
        ar.vendor_name,
        ar.vin,
        ar.licplte_nbr,
        ar.odmtr_nbr,
        ar.veh_make,
        ar.veh_year,
        ar.veh_model,
        ar.veh_color,
        ar.folder_prefix,
        -- ar.est_total_amt,
        -- ar.est_stat_typ_id,
        -- ar.est_stat_typ_cde,
        -- ar.est_stat_typ_dsc,
        ar.primary_adjuster_user_id,
        ar.primary_adjuster_first_name,
        ar.primary_adjuster_last_name,
        ar.est_received_dt_str,
        ar.est_received_dt
        -- ar.managed_tow_followup_status,
        -- ar.manual_estimate_ind,
        -- ar.note_to_shop
    FROM {schema}.{raw_table} ar
    WHERE ar.est_id::bigint::text = $1
),
damage_info AS (
    SELECT DISTINCT ON (est_id)
        est_id,
        dmg_dsc
    FROM {schema}.{line_table}
    WHERE est_id::bigint::text = $1
      AND dmg_dsc IS NOT NULL
    ORDER BY est_id
),
est_rates AS (
    SELECT DISTINCT ON (est_id)
        est_id,
        bdy_lbr_rate, mchncl_lbr_rate, frm_lbr_rate, pnt_mtrl_rate, almn_lbr_rate,
        dmstc_part_disc_amt, frn_part_disc_amt, kyls_disc_amt,
        anti_crsn_dsc, car_cvr_dsc, hzrd_wst_dsc, postscn, clbrtn,
        specl_instruct_txt, grp_note_txt
    FROM {schema}.{line_table}
    WHERE est_id::bigint::text = $1
),

line_items_agg AS (
    -- One JSON object per line item.
    -- NULL placeholders for op_code / labor_rate / paint_hrs — gone from new dd_est_line.
    SELECT
        el.est_id,
        COALESCE(
            JSON_AGG(
                JSON_BUILD_OBJECT(
                    'line_nbr',            el.line_nbr,
                    'op_code',             NULL,
                    'line_dsc',            el.line_dsc,
                                    --    cieca_part_dtl_line_id,
                    'part_type',           el.cieca_part_typ_dsc,
                    'part_num',            el.dtl_part_nbr,
                    'part_price',          el.dtl_tot_part_price_amt,
                                    --    dtl_tot_part_price_amt,
                    'part_qty',           el.dtl_part_nbr_qty,
                                    --    cieca_line_adj_amt,
                                    --    cieca_lbr_dtl_line_id,
                    'lbr_type',           cieca_lbr_typ_dsc,
                    'lbr_amt',             dtl_lbr_tot_amt,
                    'lbr_hrs',             dtl_lbr_hr_qty,
                                    -- for others
                                    -- cieca_othr_chrg_dtl_line_id,
                    'other_chrg_desc',     cieca_othr_chrg_typ_dsc,
                    'other_chrg_amt',      dtl_othr_chrg_price_amt,
                    'other_chrg_qty',      dtl_othr_chrg_qty,
                                        --- Matches
                    'part_match',          ess_parts.parts_net_match,
                    'lbr_match',           ess_lbr.overall_lbr_match,
                    'other_chrg_match',    CASE
                                                WHEN cieca_othr_chrg_typ_dsc IS NOT NULL           -- # TODO  'Pending'
                                                        THEN 'Pending'
                                                        ELSE NULL
                                                END       
                ) ORDER BY el.line_nbr
            ) FILTER (WHERE el.line_nbr IS NOT NULL),
            '[]'::json
        ) AS line_items_json
        -- SUM(COALESCE(el.dtl_tot_part_price_amt::numeric, 0))         AS parts_total,
        -- SUM(COALESCE(NULLIF(el.dtl_lbr_tot_amt, '')::numeric, 0))    AS labor_total
    FROM {schema}.{line_table} el
    LEFT JOIN {schema}.{em_subtot_detail} ess_parts
           ON el.est_id::bigint::text = ess_parts.est_id::bigint::text
          AND el.cieca_part_typ_dsc::text = ess_parts.cieca_tot_typ_dsc::text
    LEFT JOIN {schema}.{em_subtot_detail} ess_lbr
           ON el.est_id::bigint::text = ess_lbr.est_id::bigint::text
          AND el.cieca_lbr_typ_dsc::text = ess_lbr.cieca_tot_typ_dsc::text
    WHERE el.est_id::bigint::text = $1
    GROUP BY el.est_id
),

parts_findings_agg AS (
    -- Deduplicate + concatenate all findings into one string per estimate
    SELECT
        eld.est_id,
        STRING_AGG(DISTINCT eld.finding, ' | ') AS parts_findings
    FROM {schema}.{em_line_detail} eld
    WHERE eld.est_id::bigint::text = $1
      AND eld.finding IS NOT NULL
      AND eld.finding <> ''
    GROUP BY eld.est_id
),

labor_findings_agg AS (
    SELECT
        emsd.est_id,
        STRING_AGG(DISTINCT emsd.lbr_mismatch_reason, ' | ') AS labor_findings
    FROM {schema}.{em_subtot_detail} emsd
    WHERE emsd.est_id::bigint::text = $1
      AND emsd.lbr_mismatch_reason IS NOT NULL
      AND emsd.lbr_mismatch_reason <> ''
    GROUP BY emsd.est_id
),

overall_results AS (
    -- Grand total and overall match status from the EM engine
    SELECT
        est_id            AS est_id,
        est_tot_amt       AS est_total_amount,
        overall_match     AS overall_match
    FROM {schema}.{em_overall_results}
    WHERE est_id::bigint::text = $1
),
em_subtot_agg AS (
    -- Amounts come from dd_est_subtot (staging).
    -- Labor match status comes from dd_em_est_subtot_dtl_r (EM, labor-only for now).
    -- LEFT JOIN ensures parts/materials/misc rows still appear with NULL match.
    SELECT
        s.est_id        AS est_id,
        COALESCE(
            JSON_AGG(
                JSON_BUILD_OBJECT(
                    -- Derive subtot_type from category text if EM table doesn't supply it
                    'subtot_type', CASE
                        -- WHEN emsd.subtot_type IS NOT NULL          THEN emsd.subtot_type
                        WHEN s.tot_typ_cde ILIKE '%Labor%'    THEN 'labor'
                        WHEN s.tot_typ_cde ILIKE '%Parts%'    THEN 'parts'
                        WHEN s.tot_typ_cde ILIKE '%Other%'
                            OR s.tot_typ_cde ILIKE '%Material' THEN 'materials_misc'
                        ELSE                                       'total'
                    END,
                    'category',          s.cieca_tot_typ_dsc,
                    -- Parts: actual / expected pairs, all from EM table
                    'actual_gross_amt',     s.gross_amt,             -- actual subtotal
                    'expected_gross_amt',   emsd.line_tot_part_amt,     -- AI gross
                    'actual_adj_amt',       s.adj_tot_amt,           -- actual adjustment
                    'expected_adj_amt',     emsd.line_adj_amt,          -- AI adjustment
                    'actual_net_amt',       s.tot_amt,               -- actual net
                    'expected_net_amt',     emsd.line_net_amt,          -- AI net
                    'actual_adj_pct',       emsd.adj_pct,               -- actual %
                    'expected_adj_pct',     emsd.expected_adj_pct,      -- AI %
                    'parts_gross_match',    emsd.parts_gross_match,     -- subtotal row status
                    'adj_match',            emsd.adj_match,             -- adjustment row status
                    'overall_parts_match',  emsd.overall_parts_subtot_match,
                    -- Labor matching (EM),
                    'actual_hrs',          emsd.actual_hrs,
                    'expected_hrs',         emsd.expected_hrs,
                    'actual_rate',         emsd.actual_lbr_rate,
                    'expected_rate',       emsd.expected_lbr_rate,
                    'actual_lbr_amt',      emsd.actual_lbr_amt,        
                    'expected_lbr_amt',    emsd.expected_lbr_amt,
                    'overall_lbr_match',   emsd.overall_lbr_match,
                    -- Others (Materials & Miscellaneous)
                    'others_match',     'Work in progress'                       --TODO Manually coded

                ) ORDER BY s.cieca_tot_typ_dsc
            ) FILTER (WHERE s.cieca_tot_typ_dsc IS NOT NULL),
            '[]'::json
        ) AS em_subtot_json
    FROM {schema}.{subtot_table} s
    LEFT JOIN {schema}.{em_subtot_detail} emsd
           ON s.est_id::bigint::text = emsd.est_id::bigint::text
          AND s.cieca_tot_typ_dsc    = emsd.cieca_tot_typ_dsc
    WHERE s.est_id::bigint::text = $1
    GROUP BY s.est_id
),
ai_results AS (
    -- AI image verification: VIN / plate / odometer match status
    SELECT
        vf.folder_name,
        vf.vin_status,
        vf.plate_status,
        vf.odometer_status,
        vf.est_best_match_vin,
        vf.est_best_match_plate,
        vf.est_best_match_odometer
    FROM {schema}.{vi_est_results} vf
    WHERE vf.folder_name = 'EST' || $1::text
)
SELECT
    eb.*,
    di.dmg_dsc,
    -- Labor rates + discount amounts
    er.bdy_lbr_rate,
    er.mchncl_lbr_rate,
    er.frm_lbr_rate,
    er.pnt_mtrl_rate,
    er.almn_lbr_rate,
    er.dmstc_part_disc_amt,
    er.frn_part_disc_amt,
    er.kyls_disc_amt,
    -- Sublet rates + text fields
    er.anti_crsn_dsc,
    er.car_cvr_dsc,
    er.hzrd_wst_dsc,
    er.postscn,
    er.clbrtn,
    er.specl_instruct_txt,
    er.grp_note_txt,
    -- Line items + parts/labor totals
    li.line_items_json,
    -- Parts findings
    pfa.parts_findings,
    -- Labor findings
    lfa.labor_findings,
    -- Subtotal breakdown payload
    esa.em_subtot_json,
    -- AI image verification
    ai.vin_status,
    ai.plate_status,
    ai.odometer_status,
    ai.est_best_match_vin,
    ai.est_best_match_plate,
    ai.est_best_match_odometer,
    -- Overall results
    ovr.est_total_amount,
    ovr.overall_match
    
FROM est_base                eb
LEFT JOIN damage_info        di  ON eb.est_id::bigint::text = di.est_id::bigint::text
LEFT JOIN est_rates          er  ON eb.est_id::bigint::text = er.est_id::bigint::text
LEFT JOIN line_items_agg     li  ON eb.est_id::bigint::text = li.est_id::bigint::text
LEFT JOIN em_subtot_agg      esa ON eb.est_id::bigint::text = esa.est_id::bigint::text
LEFT JOIN parts_findings_agg pfa ON eb.est_id::bigint::text = pfa.est_id::bigint::text
LEFT JOIN labor_findings_agg lfa ON eb.est_id::bigint::text = lfa.est_id::bigint::text   
LEFT JOIN overall_results    ovr ON eb.est_id::bigint::text = ovr.est_id::bigint::text
LEFT JOIN ai_results         ai  ON TRUE;
"""


FEEDBACK_INSERT_QUERY = f"""
INSERT INTO {schema}.cdr_assistant_feedback
    (incident_id, section, rating, comment, user_id)
VALUES ($1, $2, $3, $4, $5)
RETURNING id, created_at;
"""


FEEDBACK_TABLE_DDL = f"""
CREATE TABLE IF NOT EXISTS {schema}.cdr_assistant_feedback (
    id           BIGSERIAL PRIMARY KEY,
    incident_id  TEXT        NOT NULL,
    section      TEXT        NOT NULL,
    rating       TEXT,
    comment      TEXT,
    user_id      TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
