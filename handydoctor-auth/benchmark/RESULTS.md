# Triage benchmark results

Generated 2026-11-07T15:29:26+00:00, mode: offline (mock generator), 45 vignettes.

| Metric | Result |
|---|---|
| Exact care level | 95.6% |
| Within one level | 97.8% |
| Under-triage (unsafe) | 0.0% |
| Over-triage (cautious) | 4.4% |
| Emergencies missed | 0 of 15 |

| Expected level | Cases | Exact |
|---|---|---|
| Look after yourself at home | 9 | 9 |
| Ask a pharmacist | 6 | 6 |
| Contact your GP | 6 | 5 |
| Get help today | 9 | 8 |
| Call 999 or go to A&E | 15 | 15 |

## Every case

| ID | Expected | Got | Rules fired |
|---|---|---|---|
| E01 | emergency | emergency | heart_attack, q:chest_spread, q:sweaty_breathless |
| E02 | emergency | emergency | stroke |
| E03 | emergency | emergency | anaphylaxis |
| E04 | emergency | emergency | meningitis_rash |
| E05 | emergency | emergency | q:sudden_severe |
| E06 | emergency | emergency | meningism, eye, q:stiff_neck |
| E07 | emergency | emergency | unconscious |
| E08 | emergency | emergency | bleeding |
| E09 | emergency | emergency | head_injury, q:head_injury |
| E10 | emergency | emergency | vision_loss |
| E11 | emergency | emergency | testicle |
| E12 | emergency | emergency | floppy_child, q:child_drowsy |
| E13 | emergency | emergency | breathing, q:blue_lips |
| E14 | emergency | emergency | kidney, q:numb_legs |
| E15 | emergency | emergency | crisis |
| U01 | urgent | urgent | appendix, q:lower_right |
| U02 | urgent | urgent | kidney, pharmacy_uti, q:back_pain_fever |
| U03 | urgent | urgent | pregnant_bleed, q:pregnant |
| U04 | urgent | urgent | q:child_fluids |
| U05 | urgent | urgent | eye, q:vision |
| U06 | urgent | urgent | cellulitis, q:rash_spreading |
| U07 | urgent | urgent | kidney, q:fever, infant_fever |
| U08 | urgent | emergency | q:cant_keep_fluids |
| U09 | urgent | urgent | q:fever, q:very_unwell |
| G01 | gp | gp | duration |
| G02 | gp | emergency | blood_in_pee_poo, q:blood_pee |
| G03 | gp | gp | duration |
| G04 | gp | gp | lump |
| G05 | gp | gp | duration |
| G06 | gp | gp | moderate_long |
| P01 | pharmacy | pharmacy | pharmacy_throat |
| P02 | pharmacy | pharmacy | pharmacy_sinus |
| P03 | pharmacy | pharmacy | pharmacy_ear |
| P04 | pharmacy | pharmacy | pharmacy_bite |
| P05 | pharmacy | pharmacy | pharmacy_bite, pharmacy_uti |
| P06 | pharmacy | pharmacy | pharmacy_skin |
| S01 | self_care | self_care | - |
| S02 | self_care | self_care | - |
| S03 | self_care | self_care | - |
| S04 | self_care | self_care | - |
| S05 | self_care | self_care | - |
| S06 | self_care | self_care | - |
| S07 | self_care | self_care | - |
| S08 | self_care | self_care | - |
| S09 | self_care | self_care | - |

The vignettes were written by the HandyDoctor team from NHS condition pages and have not been
clinically validated. They test the safety floor, not diagnostic accuracy. Real-world performance
with real users can differ from vignette results; see docs/CLINICAL_SAFETY.md.
