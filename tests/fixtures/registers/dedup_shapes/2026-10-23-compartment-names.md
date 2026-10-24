### vp_tumor (**canonical PBPK tumour vascular concentration**)
- **Type:** compartment
- **Role:** Vascular (plasma) space of a tumour carried as an additional
  perfused tissue in a whole-body PBPK model, alongside the standard
  anatomical organs. Completes the membrane-limited triad with `eu_tumor`
  and `is_tumor`, which the `pbpkSubCompartmentRegex` already admits
  (`tumor` is a registered organ of that pattern). The tumour receives
  arterial supply from the lung vascular space and returns to central
  plasma exactly like any other organ.
- **Source aliases:**
  - `C_V_TUMOUR` -- Fiandaca 2025 deposit notation (British spelling).
- **Example models:** `Fiandaca_2025_mRNABiTE_scm.R`,
`Fiandaca_2025_mRNABiTE_lcm.R`.
- **Notes:** Uses the US spelling `tumor` to match the registered organ token
  in `pbpkSubCompartmentRegex` and the existing `tumor` / `tumor_size` /
  `int_tumor` entries, even when the source paper spells it "tumour".
  Distinct from the bare `tumor` state, which holds a tumour *size* in a TGI
  model rather than a drug amount. A tumour-bearing PBPK model that does not
  resolve the vascular space separately should use `is_tumor` alone (or
  `is_tumor` + `int_tumor` for a permeability-limited target tissue) rather
  than introducing `vp_tumor`.
