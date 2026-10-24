## Drug exposure metrics

### AUC_RIF (**canonical for rifampicin plasma AUC over a 24 h dosing interval**)
- **Description:** Per-subject (time-fixed) rifampicin plasma AUC over a 0-24 h once-daily dosing interval, used as an exposure covariate in exposure-response models. In Svensson 2020 it is the individual AUC0-24 on day 2 +/- 1 of treatment (before autoinduction is complete), predicted from the empirical Bayes estimates of the companion popPK model, and it reduces the hazard of death through an Emax function.
- **Units:** `mg*h/L`
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters through an Emax function `1 - AUC_RIF / (AUC50 + AUC_RIF)` with no centring value.
- **Source aliases:**
  - `AUC` -- NONMEM `$INPUT` name in the Svensson 2020 survival control stream.
- **Example models:** `Svensson_2020_rifampicin_survival.R` (Emax reduction of the tuberculous-meningitis mortality hazard with AUC50 = 171 mg*h/L; compute the value from `modellib('Svensson_2020_rifampicin')` at `OCC = 1` over the second daily dose).
- **Notes:** Member of the `AUC_<DRUG>` family (`AUC_RTV`, `AUC_LCM`, `AUC_GCV`, ...). Specific scope because the sampling day matters for rifampicin: autoinduction raises clearance over the first 1-2 weeks, so a day-2 AUC0-24 and a steady-state AUC0-24 for the same dose differ materially. A model that uses a steady-state or induced-state rifampicin AUC should record that in `covariateData[[AUC_RIF]]$notes`, or register a sibling canonical if both appear in one model.

## Drug exposure metrics

### AUC_RIF (**canonical for steady-state rifampicin AUC over the 24 h dosing interval**)
- **Description:** Per-subject steady-state area under the plasma rifampicin concentration-time curve from 0 to 24 h under once-daily first-line tuberculosis therapy. Supplied as a static (time-fixed) per-subject exposure column to a pharmacodynamic model that consumes an upstream population PK analysis without instantiating a PK ODE; in the founding model it is the individual empirical Bayes AUC0-24 from the companion rifampicin popPK model of the same cohort.
- **Units:** `umol*h/L` in the founding model (the Kloprogge 2020 PK data were molar). Convert from mg*h/L with the rifampicin molecular weight 822.94 g/mol (`AUC_umol = AUC_mg * 1000 / 822.94`). Document per-model via `covariateData[[AUC_RIF]]$units`.
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- used as an exponential effect centred on a cohort median, `exp(theta * (AUC_RIF - median))`. Reference value observed: 35.36 umol*h/L (Kloprogge 2020 PK-cohort median 29.10 mg*h/L).
- **Source aliases:**
  - `AUCrif` -- Kloprogge 2020 Supplementary S2 Table row `T12_AUCrif` ('effect of AUC rifampicin on start time of delayed bacillary clearance'); same orientation, no value transformation.
- **Example models:** `Kloprogge_2020_hrze_bacterialload.R` (exponential effect on the half-time T1/2 of the fall in sputum bacterial kill rate, coefficient 0.0335 per umol*h/L).
- **Notes:** Member of the `AUC_<DRUG>` family; sibling of `AUC_INH` from the same model. The upstream model that generates this column is `modellib('Kloprogge_2020_rifampicin')` (dose / CL at steady state). Distinct from `CONMED_RIFAMPICIN_SD` / `CONMED_RIFAMPICIN_MD`, which flag rifampicin co-administration as a perpetrator rather than carrying its exposure.
