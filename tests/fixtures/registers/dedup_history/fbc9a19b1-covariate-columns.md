## Receptor-binding ligand selection and pharmacological-chain inputs

### AUC_DAPA (**canonical for steady-state 24 h AUC of dapagliflozin**)
- **Description:** Dapagliflozin area under the plasma concentration-time curve over the once-daily 24 h dosing interval at steady state, supplied as a static drug-exposure column to PD / disease-endpoint models that consume an upstream population PK analysis without instantiating a PK ODE. Study-arm-level in model-based meta-analyses (one value per published trial arm); per-subject in individual-level exposure-response analyses.
- **Units:** `ng*h/mL`. Document per-model via `covariateData[[AUC_DAPA]]$units`. Must be on the same scale as the EC50 the consuming model carries.
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters via an Emax form `Emax * AUC_DAPA / (EC50 + AUC_DAPA)`. Set to 0 for placebo arms and for arms treated with a different SGLT2 inhibitor; the drug term then vanishes exactly, leaving the placebo / disease-progression trajectory. Reference value observed: EC50 = 56.6 ng*h/mL for the change in urinary glucose excretion normalised by baseline FPG (Yao 2023 Table 2); the 10 mg label dose gives about 513 ng*h/mL from the companion popPK (1000 * 10 mg / 19.5 L/h).
- **Source aliases:**
  - `AUC0-24h` -- the printed name in Yao 2023 Equation 2, Figure 2 and the PK/PD model section. Same units and orientation as the canonical, so no value transformation.
- **Example models:** `Yao_2023_sglt2_endpoints_mbma.R` (study-arm-level steady-state 24 h AUC driving the shared-Emax / drug-specific-EC50 model of the dUGEc translational biomarker, which then lowers FPG linearly and HbA1c through an indirect-response turnover model).
- **Notes:** Specific scope because the column meaning is tied to dapagliflozin and to the once-daily steady-state 24 h AUC convention. Member of the `AUC_<DRUG>` family; sibling of `AUC_EMPA` and `AUC_CANA`, the other two SGLT2-inhibitor members, which the same source paper uses side by side on mutually exclusive study arms. Compute it from `modellib('Yao_2023_dapagliflozin_mbma')` as total daily dose divided by CL/F -- exact for a linear model at steady state. A future dapagliflozin model using a different exposure metric (Cmax, Ctrough, a cumulative rather than per-interval AUC) should register a parallel canonical rather than overload this one.

## Receptor-binding ligand selection and pharmacological-chain inputs

### AUC_DAPA (**canonical for per-subject steady-state 24 h AUC of dapagliflozin**)
- **Description:** Per-subject (time-fixed) steady-state AUC of dapagliflozin over the q24h dosing interval, supplied as a static drug-exposure covariate to exposure-response models that consume an upstream population PK analysis without instantiating a PK sub-model. Sokolov 2023 Table 1 records the values as "taken from a previously performed population PK analysis (Melin et al., 2022)" and passes them to the PD model as a Monolix regressor.
- **Units:** `ng*h/mL` (document per-model via `covariateData[[AUC_DAPA]]$units` if a different exposure unit is reported).
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters via inhibitory Imax forms `1 - imax * AUC_DAPA / (auc50 + AUC_DAPA)` on the basal insulin dose, average daily glucose and HbA1c ratios. Set AUC_DAPA = 0 to recover the placebo arm exactly (every Imax term vanishes). Reference values observed: IAUC50 = 38.8 ng*h/mL (basal insulin dose) and 67.4 ng*h/mL (glucose, and the direct HbA1c effect fixed to the same value) per Sokolov 2023 Table 3. Mean AUC by dose (Sokolov 2023 Figure 10 caption): 51.4, 130.6, 294.5 and 594.3 ng*h/mL at 1, 2.5, 5 and 10 mg once daily.
- **Source aliases:**
  - `AUC` -- the printed variable name in Sokolov 2023 Supplementary Equations 1-3 and the Table 1 measurement row "Dapagliflozin AUC".
- **Example models:** `Sokolov_2023_dapagliflozin.R` (semi-mechanistic exposure-response model on the basal insulin dose, CGM glucose and HbA1c ratios in adults with type 1 diabetes; AUC_DAPA drives all three Imax terms).
- **Notes:** Specific scope because the column meaning is tied to dapagliflozin and to a q24h steady-state AUC convention. Member of the `AUC_<DRUG>` family (`AUC_CARBO`, `AUC_GEM`, `AUC_GCV`, `AUC_PAZO`, `AUC_RTV`, `AUC_EMPA`, `AUC_HTBZ`, `AUC_LEN`); `AUC_EMPA` is the closest structural analogue -- likewise a per-subject steady-state SGLT2-inhibitor AUC carried out of an upstream popPK into a downstream type 1 diabetes exposure-response model. Distinct from `CINH`, the time-varying plasma SGLT-inhibitor *concentration* regressor used in renal-glucose-reabsorption QSP models, and from `CONMED_SGLT2I`, the binary concomitant-SGLT2-inhibitor indicator. A future dapagliflozin model driven by a different exposure metric (trough concentration, Cmax, dose-normalised AUC) should register a parallel canonical rather than overload `AUC_DAPA`.
