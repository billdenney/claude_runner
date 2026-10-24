## Drug exposure metrics

### PSA_BL (**canonical for baseline serum prostate-specific antigen concentration**)
- **Description:** Subject's serum prostate-specific antigen (PSA) concentration at the baseline / randomisation visit, before study treatment, carried into a model as a time-fixed per-subject covariate. Used in benign prostatic hyperplasia (BPH) disease-progression and outcome models, where baseline PSA is a surrogate for prostate size and a known risk factor for acute urinary retention and BPH-related surgery.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** general
- **Reference category:** n/a -- enters as a centred term. Reference value observed: 3.4 ng/mL (D'Agate 2021 pooled-population median).
- **Source aliases:**
  - `B_PSA` -- D'Agate 2021 Supporting Information Figure S3 label ('PSA concentration at baseline').
- **Example models:** `DAgate_2021_bph_aurs_mbma.R` (founding example; log-linear effect on the hazard of first acute urinary retention or BPH-related surgery, `exp(log(1.08) * (PSA_BL - 3.4))`).
- **Notes:** Member of the `<X>_BL` per-subject-baseline family (`INS_BL`, `FERRITIN_BL`, `D25OH_BL`). The measured, unadjusted pre-treatment value: 5-alpha-reductase inhibitors such as dutasteride roughly halve PSA on treatment, and a model of on-treatment PSA as a time course must use a separate time-varying column or an output state, never this one. Distinct from PSA as a modelled tumour-marker output in prostate-cancer models (e.g. the `PSA` state of `vanHasselt_2015_eribulin.R`).

## Oncology

### PSA_BL (**canonical for observed baseline serum prostate-specific antigen**)
- **Description:** Observed per-subject baseline serum prostate-specific antigen (PSA) concentration, time-fixed per subject -- the last PSA measured before treatment start. Used both as a per-subject regressor that seeds a PSA disease-progression state (initial condition) and as a covariate on a disease-progression parameter.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** general
- **Reference category:** n/a -- used with power scaling `(PSA_BL / ref)^exponent` when it enters a parameter; the observed value itself when it seeds a state.
- **Source aliases:**
  - `BAS` -- used in `Zou_2020_leuprorelin.R` (Zou 2020 Eqs 2-3 and 17, 'BAS is the baseline PSA').
- **Example models:** `Zou_2020_leuprorelin.R` (seeds the Stein-type sub-states `growth(0) = R * PSA_BL`, `shrink(0) = (1 - R) * PSA_BL`, and is a power covariate on the kill rate `kse`, centered at the cohort median 8.5 ng/mL with exponent 0.174).
- **Notes:** Follows the majority `<X>_BL` baseline-analyte pattern (`HGB_BL`, `INS_BL`, `FERRITIN_BL`). Named `PSA_BL` rather than `PSA_BASE` because the `<X>_BASE` pattern is reserved for a baseline paired with a same-analyte time-varying covariate column, whereas here PSA is the model's observable, not a covariate. A future time-varying PSA covariate (e.g. PSA as a driver in a survival model) should be registered separately as `PSA`.
