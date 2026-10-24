## Drug exposure metrics

### CP_ENALAPRIL_NGML (**canonical for instantaneous enalapril plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of enalapril (an angiotensin-converting-enzyme inhibitor) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = emax * CP_ENALAPRIL_NGML / (ic50 + CP_ENALAPRIL_NGML)` subtracted inside the TPR production term (Snelder 2013 Equations 4 and 6). Set to 0 outside the exposure window and for every subject not receiving enalapril. Reference values observed: the fitted IC50 in conscious spontaneously hypertensive rats is 2410 ng/mL (Snelder 2013 Table 6) after 30 mg/kg p.o. once daily.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6, "the drug concentration in plasma, which varies with time"; values in ng/mL because Table 6 reports IC50 in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate in the drug-independent CVS model; Emax fixed at 1 and IC50 = 2410 ng/mL), `Snelder_2014_cardiovascular_rat.R` (effect-compartment-delayed Emax inhibition of BOTH TPR and SV production with one shared EC50 = 1200 ng/mL).
- **Notes:** Specific scope; enalapril-specific. Follows the established `CP_<drug>_<units>` precedent (`CP_OXY_NGML`, `CP_FBX_NGML`, `CP_LSN_NGML`, `CP_MORPH_NGML`, `CP_BPN_NGML`, `CP_EIDD_NGML`, `CP_RIF_UM`, `CP_GDC_UM`). Snelder 2013 Table 3 obtained the enalapril concentration-time profile from a two-compartment model with Michaelis-Menten elimination refitted in NONMEM to data read off Lin et al. 1988 (Sprague-Dawley rats), and publishes NO PK parameter values for it; no enalapril PK model exists in the nlmixr2lib registry either, so downstream users must supply this column externally. Snelder 2013 Table 4 Assumption 4 states that the PK was assumed not to differ between rat strains.

## Drug exposure metrics

### CP_ENALAPRIL_NGML (**canonical for instantaneous enalapril plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of enalapril (an angiotensin-converting-enzyme inhibitor) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of a hemodynamic production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- in `Snelder_2013_cardiovascular_rat.R` it enters an Emax inhibition of the TPR production term directly (`EFF = emax * CP_ENALAPRIL_NGML / (ic50 + CP_ENALAPRIL_NGML)`, Snelder 2013 Equations 4 and 6). In `Snelder_2014_cardiovascular_rat.R` it instead drives an **effect compartment** (`dCe/dt = ke0 * (CP_ENALAPRIL_NGML - Ce)`, ke0 = 0.163 1/h, Snelder 2014 Equation 8), and the Emax inhibition of BOTH the TPR and the SV production rates is a function of that effect-site concentration rather than of the column itself. Set to 0 outside the exposure window and for every animal not receiving enalapril. Reference values observed: IC50 = 2410 ng/mL (Snelder 2013 Table 6) and EC50 = 1200 ng/mL (Snelder 2014 Table 5).
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6 and Snelder 2014 Equations 6-8; values in ng/mL because both parameter tables report potency in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate in the drug-independent CVS model; Emax fixed at 1 and IC50 = 2410 ng/mL), `Snelder_2014_cardiovascular_rat.R` (effect-compartment-delayed Emax inhibition of BOTH TPR and SV production with one shared EC50 = 1200 ng/mL).
- **Notes:** Specific scope; enalapril-specific. Follows the established `CP_<drug>_<units>` precedent. Both Snelder papers obtained the enalapril concentration-time profile from a two-compartment model with Michaelis-Menten elimination refitted in NONMEM to data read off Lin et al. 1988 (and, in 2014, also Li et al. 2007) in Sprague-Dawley rats, and publish NO CL / V / Vmax / Km value for it; no enalapril PK model exists in the nlmixr2lib registry either, so downstream users must supply this column externally. Both papers state that the PK was assumed not to differ between rat strains. **Note the modelling difference between the two example models:** Snelder 2013 measured no cardiac output and so resolved only the primary TPR effect, whereas Snelder 2014 measured CO and resolved a second, SV-mediated effect with the same EC50 (the separately estimated values had overlapping confidence intervals) plus a 4.3 h effect-compartment delay attributed to the renin-angiotensin-aldosterone cascade. A dataset column feeding the 2014 model is therefore consumed through one more state than the same column feeding the 2013 model.

## Drug exposure metrics

### CP_FASUDIL_NGML (**canonical for instantaneous fasudil plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of fasudil (a Rho-kinase inhibitor) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = emax * CP_FASUDIL_NGML / (ic50 + CP_FASUDIL_NGML)` subtracted inside the TPR production term (Snelder 2013 Equations 4 and 6). Set to 0 outside the exposure window. Reference values observed: the fitted IC50 in conscious spontaneously hypertensive rats is 321 ng/mL (Snelder 2013 Table 6) after 30 mg/kg p.o. once daily.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and IC50 = 321 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and EC50 = 0.172 ng/mL).
- **Notes:** Specific scope; fasudil-specific. Follows the established `CP_<drug>_<units>` precedent. Snelder 2013 Table 3 built the fasudil profile from a one-compartment model whose absorption rate constant and lag time were back-derived in Berkeley Madonna from the half-life, AUC and Cmax reported by Ikegaki et al. 2001 (Wistar-Kyoto rats); no PK parameter values are published in Snelder 2013 and no fasudil PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally.

## Drug exposure metrics

### CP_FASUDIL_NGML (**canonical for instantaneous fasudil plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of fasudil (a Rho-kinase inhibitor) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take vasodilator exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = -emax * CP_FASUDIL_NGML / (ec50 + CP_FASUDIL_NGML)` inside the TPR production term (Snelder 2013 Equations 4 and 6; Snelder 2014 Equations 6 and 7). Set to 0 outside the exposure window. Reference values observed: **the two example models disagree by roughly 1900-fold** -- IC50 = 321 ng/mL in Snelder 2013 Table 6 versus EC50 = 0.172 ng/mL in Snelder 2014 Table 5, over doses of 3-30 mg/kg p.o. in both.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6 and Snelder 2014 Equations 6 and 7; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and IC50 = 321 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and EC50 = 0.172 ng/mL).
- **Notes:** Specific scope; fasudil-specific. Follows the established `CP_<drug>_<units>` precedent. Both Snelder papers built the fasudil profile from a one-compartment model whose absorption rate constant and lag time were back-derived in Berkeley Madonna from the half-life, AUC and Cmax reported by Ikegaki et al. 2001 (Wistar-Kyoto rats); neither publishes PK parameter values and no fasudil PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. **Scale warning:** the ~1900-fold potency discrepancy between the two example models is a genuine between-publication difference, not a transcription artefact -- each value is internally consistent with its own reported RSE and 95% confidence interval (321 with CV 40.5% in 2013; 0.172 with RSE 18.4% and CI 0.110-0.234 in 2014) -- and neither paper reconciles it. Because the driver is a user-supplied column rather than a coupled PK model, a fasudil concentration-time profile constructed to be consistent with one paper's potency scale must not be reused with the other's.

## Drug exposure metrics

### CP_AMLODIPINE_NGML (**canonical for instantaneous amlodipine plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of amlodipine (a dihydropyridine calcium-channel blocker) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = emax * CP_AMLODIPINE_NGML / (ic50 + CP_AMLODIPINE_NGML)` subtracted inside the TPR production term (Snelder 2013 Equations 4 and 6). Set to 0 outside the exposure window. Reference values observed: the fitted IC50 in conscious spontaneously hypertensive rats is 185 ng/mL (Snelder 2013 Table 6), over doses of 0.3-10 mg/kg p.o.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and IC50 = 185 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and EC50 = 82.8 ng/mL).
- **Notes:** Specific scope; amlodipine-specific. Follows the established `CP_<drug>_<units>` precedent. Snelder 2013 Table 3 built the amlodipine profile from a one-compartment model whose absorption rate constant was back-derived in Berkeley Madonna from the half-life, Vd, F and Tmax reported by Stopher et al. 1988 (Sprague-Dawley rats); no PK parameter values are published in Snelder 2013 and no amlodipine PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. Amlodipine is the one compound in the Snelder 2013 training set whose omission materially changes a system parameter (FB1 moves from 0.00379 to 0.00454 1/mmHg; Figure 5), so an amlodipine arm is load-bearing for any re-fit of that model.

## Drug exposure metrics

### CP_AMLODIPINE_NGML (**canonical for instantaneous amlodipine plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of amlodipine (a dihydropyridine calcium-channel blocker) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = -emax * CP_AMLODIPINE_NGML / (ec50 + CP_AMLODIPINE_NGML)` inside the TPR production term (Snelder 2013 Equations 4 and 6; Snelder 2014 Equations 6 and 7). Set to 0 outside the exposure window. Reference values observed: the fitted potency in conscious spontaneously hypertensive rats is IC50 = 185 ng/mL in the Snelder 2013 basic CVS model and EC50 = 82.8 ng/mL in the Snelder 2014 extended CVS model, over doses of 0.3-10 mg/kg p.o.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6 and Snelder 2014 Equations 6 and 7; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and IC50 = 185 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax fixed at 1 and EC50 = 82.8 ng/mL).
- **Notes:** Specific scope; amlodipine-specific. Follows the established `CP_<drug>_<units>` precedent. Both Snelder papers built the amlodipine profile from a one-compartment model whose absorption rate constant was back-derived in Berkeley Madonna from the half-life, Vd, F and Tmax reported by Stopher et al. 1988 (Sprague-Dawley rats); neither publishes PK parameter values and no amlodipine PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. Amlodipine is load-bearing in both papers: in Snelder 2013 it is the one compound whose omission materially changes a system parameter (FB1 moves from 0.00379 to 0.00454 1/mmHg; Figure 5), and in Snelder 2014 it is the paradigm compound for the HR-and-MAP-only site-of-action analysis, where re-fitting to HR and MAP alone with the system parameters fixed recovered EC50 = 84.9 ng/mL (95% CI 75.4-94.4), indistinguishable from the full-data value.

## Drug exposure metrics

### CP_PRAZOSIN_NGML (**canonical for instantaneous prazosin plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of prazosin (a selective alpha-1 adrenoceptor antagonist) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = emax * CP_PRAZOSIN_NGML / (ic50 + CP_PRAZOSIN_NGML)` subtracted inside the TPR production term (Snelder 2013 Equations 4 and 6). Set to 0 outside the exposure window. Reference values observed: the fitted IC50 in conscious spontaneously hypertensive rats is 0.133 ng/mL (Snelder 2013 Table 6), over doses of 0.04-5 mg/kg p.o.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax and IC50 both estimated, 0.213 and 0.133 ng/mL), `Snelder_2014_cardiovascular_rat.R` (power-model inhibition of the TPR production rate; SL = 0.328, POW = 0.0910).
- **Notes:** Specific scope; prazosin-specific. Follows the established `CP_<drug>_<units>` precedent. Snelder 2013 Table 3 built the prazosin profile from a one-compartment model whose CL and Vd were allometrically scaled to the rat from the New Zealand white rabbit model of Hamilton et al. 1985, with the absorption rate constant estimated simultaneously with the PD; no PK parameter values are published in Snelder 2013 and no prazosin PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. Prazosin carries the only imprecise drug-specific estimate in Snelder 2013 (IC50 CV 110%, 95% CI -0.15 to 0.4 ng/mL), a direct consequence of estimating Emax and IC50 jointly rather than fixing Emax at 1; treat the potency of this arm as poorly identified.

## Drug exposure metrics

### CP_PRAZOSIN_NGML (**canonical for instantaneous prazosin plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of prazosin (a selective alpha-1 adrenoceptor antagonist) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an inhibition of the total-peripheral-resistance production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- in `Snelder_2013_cardiovascular_rat.R` it enters an Emax inhibition of the TPR production term (`EFF = emax * CP_PRAZOSIN_NGML / (ic50 + CP_PRAZOSIN_NGML)`, Equations 4 and 6). In `Snelder_2014_cardiovascular_rat.R` it instead enters a **power** model (`EFF = -SL * CP_PRAZOSIN_NGML^POW`, Equation 7 with SL = 0.328 and POW = 0.0910), the only non-Emax concentration-effect form in that paper. Set to 0 outside the exposure window. Reference values observed: IC50 = 0.133 ng/mL (Snelder 2013 Table 6), over doses of 0.04-5 mg/kg p.o. in both papers.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6 and Snelder 2014 Equations 6 and 7; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the TPR production rate; Emax and IC50 both estimated, 0.213 and 0.133 ng/mL), `Snelder_2014_cardiovascular_rat.R` (power-model inhibition of the TPR production rate; SL = 0.328, POW = 0.0910).
- **Notes:** Specific scope; prazosin-specific. Follows the established `CP_<drug>_<units>` precedent. Both Snelder papers built the prazosin profile from a one-compartment model whose CL and Vd were allometrically scaled to the rat from the New Zealand white rabbit model of Hamilton et al. 1985 -- the only cross-species PK extrapolation in either analysis (Snelder 2014 Table 3 Assumption 3). Snelder 2013 estimated the absorption rate constant simultaneously with the PD; Snelder 2014 found it very high and imprecise and FIXED it to 99 1/h, a near-instantaneous-absorption idealisation adopted to remove correlations between drug-specific parameters and shorten run times. Neither publishes PK parameter values and no prazosin PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. **Prazosin is the poorly-identified potency in both papers, for different reasons:** in Snelder 2013 the IC50 has CV 110% (95% CI -0.15 to 0.4 ng/mL) because Emax and IC50 were estimated jointly, and in Snelder 2014 the power exponent of 0.0910 means the maximum effect was never reached, so the concentration-effect curve is nearly flat and the absolute scale of this column matters less than for any other driver in the family. Note also that the Snelder 2014 power form is unbounded above, unlike an Emax form, and must not be extrapolated far beyond the studied dose range.

## Drug exposure metrics

### CP_HCTZ_NGML (**canonical for instantaneous hydrochlorothiazide plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of hydrochlorothiazide (HCTZ, a thiazide diuretic) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take antihypertensive exposure as an external input to an Emax inhibition of the cardiac-output production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = emax * CP_HCTZ_NGML / (ic50 + CP_HCTZ_NGML)` subtracted inside the CO production term (Snelder 2013 Equations 4 and 6). Set to 0 outside the exposure window. Reference values observed: the fitted IC50 in conscious spontaneously hypertensive rats is 12 300 ng/mL (Snelder 2013 Table 6), over doses of 0.1-3 mg/kg p.o.
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the CO production rate; Emax fixed at 1 and IC50 = 12 300 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the SV production rate; Emax fixed at 1 and EC50 = 28 900 ng/mL).
- **Notes:** Specific scope; hydrochlorothiazide-specific. The abbreviated stem `HCTZ` is the source paper's own abbreviation and is the universally recognised short form. Follows the established `CP_<drug>_<units>` precedent. Snelder 2013 Table 3 built the HCTZ profile from a one-compartment model taking Ke, Ka and Vd from Asdaq & Inamdar 2009 (Wistar-Kyoto rats) with bioavailability back-calculated from the reported AUC; no PK parameter values are published in Snelder 2013 and no hydrochlorothiazide PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. Snelder 2013 reports that the 1 mg/kg HCTZ response was under-predicted and attributes this to the Emax = 1 constraint (the dose range did not span no-effect to maximal effect) rather than to the system model.

## Drug exposure metrics

### CP_HCTZ_NGML (**canonical for instantaneous hydrochlorothiazide plasma concentration as a time-varying PD driver**)
- **Description:** Instantaneous plasma concentration of hydrochlorothiazide (HCTZ, a thiazide diuretic) supplied directly as a time-varying covariate column rather than computed from a coupled PK model. Used in cardiovascular-systems (CVS) hemodynamic models that take diuretic exposure as an external input to an Emax inhibition of a cardiac-output or stroke-volume production rate.
- **Units:** ng/mL
- **Type:** continuous
- **Scope:** specific
- **Reference category:** n/a -- enters as the Hill term of an Emax inhibition expression `EFF = -emax * CP_HCTZ_NGML / (ec50 + CP_HCTZ_NGML)` inside the CO production term in Snelder 2013 (Equations 4 and 6) and inside the SV production term in Snelder 2014 (Equations 6 and 7), which is the same site of action expressed at the finer resolution the 2014 model provides. Set to 0 outside the exposure window. Reference values observed: IC50 = 12 300 ng/mL (Snelder 2013 Table 6) and EC50 = 28 900 ng/mL (Snelder 2014 Table 5).
- **Source aliases:**
  - `C` (Snelder 2013 Equation 6 and Snelder 2014 Equations 6 and 7; values in ng/mL).
- **Example models:** `Snelder_2013_cardiovascular_rat.R` (Emax inhibition of the CO production rate; Emax fixed at 1 and IC50 = 12 300 ng/mL), `Snelder_2014_cardiovascular_rat.R` (Emax inhibition of the SV production rate; Emax fixed at 1 and EC50 = 28 900 ng/mL).
- **Notes:** Specific scope; hydrochlorothiazide-specific. The abbreviated stem `HCTZ` is the source papers' own abbreviation and is the universally recognised short form. Follows the established `CP_<drug>_<units>` precedent. Both Snelder papers built the HCTZ profile from a one-compartment model taking Ke, Ka and Vd from Asdaq & Inamdar 2009 (Wistar-Kyoto rats) with the bioavailability term back-calculated from the reported AUC; neither publishes PK parameter values and no hydrochlorothiazide PK model exists in the nlmixr2lib registry, so downstream users must supply this column externally. Snelder 2013 reported that the 1 mg/kg HCTZ response was under-predicted and attributed it to the Emax = 1 constraint (the 0.1-3 mg/kg range had not spanned no-effect to maximal effect) rather than to the system model; Snelder 2014 addressed this directly by adding a second occasion at 10 and 30 mg/kg, which is why the two EC50 estimates differ by rather more than the other shared compounds.
