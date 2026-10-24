### cl_time_max (**canonical maximum magnitude of a sigmoidal-in-time clearance change**)
- **Type:** bare-pk
- **Role:** Magnitude of the log-clearance change approached as `t >> cl_t50`, in the
  sigmoidal-in-time clearance form
  `td_cl <- exp(cl_time_max_i * t^cl_time_hill / (cl_t50^cl_time_hill + t^cl_time_hill))`.
  Log form `lcl_time_max`; the per-individual value after IIV is written `cl_time_max_i`.
- **Source aliases:**
  - `cl_hill_max` / `lcl_hill_max` -- prior name (pre-2026-08-31 rename; see Notes).
- **Example models:** `Kuchimanchi_2024_dostarlimab.R`, `Masters_2022_avelumab.R`, `PK_2cmt_tdcl_des.R`.
  STRUCTURE (time-varying clearance, `cl_time_*`) rather than only the shape of the curve.
  `hill` now appears solely on the shape coefficient `cl_time_hill`, where it is meaningful.
  Ties the family to the registered `cl_time` / `lcl_time` stem. The trio was used in ~20
  shipped models for a long time without a register entry; documented here as part of that
  rename. Distinct from maturation Hill coefficients such as `e_page_cl_hill`
  (`Tikiso_2021_abacavir.R`), which are covariate effects on CL maturation, not time.

### ec50_t50 (**canonical half-time of a sigmoidal-in-time EC50 change**)
- **Type:** paper-named-param
- **Role:** Time at which half of [[ec50_time_max]] has been reached, in the same sigmoidal
  form. Log form `lec50_t50`. Units are whatever time unit the source equation uses, which
  need not be the model's time unit -- convert inside `model()` and say so.
- **Source aliases:** none.
- **Example models:** `Zhang_2016_burosumab.R` (fixed at 32 weeks, a constant printed only
  inside Zhang 2016 equation 10 and absent from Table 3; the model converts the solver's days
  to weeks before evaluating the sigmoid).
- **Notes:** Named without a `time` infix because `t50` already denotes a time, matching
  [[cl_t50]].
