## Coagulation / hemostasis biomarkers

### FVIII_BL (**canonical for lowest-ever measured endogenous FVIII:C activity**)
- **Description:** The lowest plasma factor VIII coagulant activity (FVIII:C) ever measured in the patient, taken as the patient's endogenous baseline. Added to the predicted exogenous FVIII in the observation equation of an FVIII-concentrate model. Time-fixed per subject.
- **Units:** IU/mL (1 IU/mL = 100 IU/dL = 100% of pooled normal plasma FVIII activity).
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- an additive offset with no reference value.
- **Source aliases:**
  - `Baseline FVIII level`, `baseline FVIII:C`, "FVIII activity levels (lowest levels ever measured in the patient)" -- de Jager 2020 Methods 2.1, Table 1 and Supplement 1 Eq. 2; used in `deJager_2020_factorviii.R`.
  - `FVIII-lowest` -- Schütte 2018 (tested, not retained).
- **Example models:** `deJager_2020_factorviii.R` (observation `Cc = (central + virtual) / vc / 1000 + FVIII_BL`; Supplement 1 Eq. 2). Set to 0 to simulate the drug-attributable FVIII only.
- **Notes:** Distinct from `FVIIIRECENT`, the most recent untreated FVIII:C. The two differ in practice: in de Jager 2020 the pre-dose FVIII was often higher than the lowest-ever value (Methods 2.2), and the model fits a virtual dose to carry the difference. Use `FVIIIRECENT` when the paper's baseline is the latest pre-treatment measurement, and this column when it is the historical minimum.

## Coagulation / hemostasis biomarkers

### FVIII_BL (**canonical for lowest-ever measured endogenous FVIII:C activity**)
- **Description:** The lowest plasma factor VIII coagulant activity (FVIII:C) ever measured in the patient, taken as the patient's endogenous baseline. Added to the predicted exogenous FVIII in the observation equation of an FVIII-concentrate model, and in PK-PD models also to the FVIII activity that drives the pharmacodynamic response. Time-fixed per subject.
- **Units:** IU/mL (1 IU/mL = 100 IU/dL = 100% of pooled normal plasma FVIII activity). A model working in IU/dL multiplies by 100 inside `model()`.
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- an additive offset with no reference value.
- **Source aliases:**
  - `Baseline FVIII level`, `baseline FVIII:C`, "FVIII activity levels (lowest levels ever measured in the patient)" -- de Jager 2020 Methods 2.1, Table 1 and Supplement 1 Eq. 2.
  - `baseline FVIII_i`, "the lowest FVIII activity level ever measured was regarded as the endogenous baseline" -- Bukkems 2022 Supplementary Methods Eq. 2 and Results 3.1; used in `Bukkems_2022_factorviii.R` and its three PD siblings.
  - `FVIII-lowest` -- Schütte 2018 (tested, not retained).
- **Example models:**
  - `Bukkems_2022_factorviii.R` (observation `Cc = central / vc * 1.20^(1 - ASSAY_OSA) + 100 * FVIII_BL`, IU/dL; Supplementary Methods Eq. 2). `Bukkems_2022_factorviii_thrombinPeak.R`, `Bukkems_2022_factorviii_thrombinPotential.R` and `Bukkems_2022_factorviii_plasminPeak.R` also drive their Emax / Imax functions with `central / vc + 100 * FVIII_BL` (Bukkems 2022 Equation 1: C is "the FVIII activity level (including endogenous baseline)"). Set to 0 to simulate the drug-attributable FVIII only.
- **Notes:** Distinct from `FVIIIRECENT`, the most recent untreated FVIII:C. The two differ in practice: in de Jager 2020 the pre-dose FVIII was often higher than the lowest-ever value (Methods 2.2), and that model fits a virtual dose to carry the difference. Use `FVIIIRECENT` when the paper's baseline is the latest pre-treatment measurement, and this column when it is the historical minimum. Hemophilia A severity bands (severe < 0.01, moderate 0.01-0.05, mild > 0.05-0.40 IU/mL) bound the plausible range; see `DIS_HEMOPHILIA_A_MILD`.
