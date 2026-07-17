-- Field-medical reference data for the Databricks agent (FICTIONAL).
-- Generated from shared/sample_data.py — edit that file, not this one.
-- Default target: main.field_medical. Run on a SQL warehouse.

CREATE SCHEMA IF NOT EXISTS main.field_medical COMMENT 'Field-medical demo reference data';

CREATE TABLE IF NOT EXISTS main.field_medical.products (product_name STRING, generic_name STRING, drug_class STRING, standard_dose STRING, renal_guidance STRING, hepatic_guidance STRING, pregnancy_guidance STRING) USING DELTA;

CREATE TABLE IF NOT EXISTS main.field_medical.adverse_events (product_name STRING, adverse_event STRING, frequency STRING, seriousness STRING) USING DELTA;

TRUNCATE TABLE main.field_medical.products;

INSERT INTO main.field_medical.products VALUES
('Cardizafen','cardizafen hydrochloride','calcium-channel blocker (antihypertensive)','180 mg once daily, titratable to 360 mg/day','In severe renal impairment (CrCl < 30 mL/min), initiate at 90 mg once daily and titrate slowly with close BP and renal monitoring. No dose adjustment required in mild-to-moderate impairment.','Reduce starting dose in moderate-to-severe hepatic impairment; monitor LFTs.','Category C: use only if potential benefit justifies potential risk to the fetus. Limited human data.'),
('Neuroliximab','neuroliximab','monoclonal antibody (anti-CGRP, migraine prophylaxis)','240 mg subcutaneous loading dose, then 120 mg monthly','No dose adjustment required in renal impairment; not studied in severe (CrCl < 15).','No dose adjustment required in hepatic impairment.','Insufficient data; discontinue if pregnancy is confirmed unless clearly needed.'),
('Glucoravir','glucoravir sodium','SGLT2 inhibitor (type 2 diabetes)','10 mg once daily, may increase to 25 mg once daily','Do not initiate if eGFR < 30 mL/min/1.73m2; discontinue if eGFR falls persistently below 45.','Not recommended in severe hepatic impairment.','Not recommended during the second and third trimesters.');

TRUNCATE TABLE main.field_medical.adverse_events;

INSERT INTO main.field_medical.adverse_events VALUES
('Cardizafen','peripheral edema','common','non-serious'),
('Cardizafen','headache','common','non-serious'),
('Cardizafen','bradycardia','uncommon','serious'),
('Cardizafen','palpitations','common','non-serious'),
('Cardizafen','dizziness','common','non-serious'),
('Neuroliximab','injection-site reaction','common','non-serious'),
('Neuroliximab','constipation','common','non-serious'),
('Neuroliximab','hypersensitivity reaction','rare','serious'),
('Glucoravir','genital mycotic infection','common','non-serious'),
('Glucoravir','volume depletion','uncommon','non-serious'),
('Glucoravir','diabetic ketoacidosis','rare','serious');

