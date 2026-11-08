# Clinical safety case (prototype)

This document follows the shape of **DCB0129** (the NHS clinical risk management
standard for manufacturers of health IT). It is a working hazard log for a
student/portfolio project, not a signed-off safety case. Before any real patient
use, a registered Clinical Safety Officer must review and own it.

## Intended use

HandyDoctor gives adults in the UK general, signposting-only guidance about where
to get help for a symptom: self-care, a pharmacist, a GP, same-day help, or 999/A&E.
It does not diagnose, name likely conditions, or recommend prescription treatment.

## Safety architecture

| Layer | What it does | Where |
|---|---|---|
| Crisis check | Suicide or self-harm wording returns a fixed response (Samaritans, SHOUT, 999) before any AI runs | `app/chat/ai/pipeline.py` |
| Red-flag rules | Deterministic NHS-derived rules over the user's text and interview answers set a **minimum** care level | `app/chat/ai/redflags.py` |
| Guided interview | Asks who it's for, age, duration, severity and up to five symptom-specific red-flag questions | `app/chat/ai/interview.py` |
| Rule floor | The AI's level can raise the result, never lower it below the rules. A "no" answer never lowers anything | `pipeline.run_pipeline` |
| Response classifier | Ignores conditional safety-netting ("call 999 if…") so it reflects the actual instruction | `app/chat/ai/triage.py` |
| Child dose filter | Removes adult-dose sentences from answers about under-16s | `pipeline.child_safe` |
| Follow-up check-in | "Worse" moves the advice up a level; red flags in the note apply | `service.checkin` |
| Benchmark gate | 45 vignettes run in CI; any missed emergency fails the build | `benchmark/` |

## Hazard log

| ID | Hazard | Cause | Controls | Residual risk |
|---|---|---|---|---|
| H1 | Emergency advised as non-urgent | User omits key detail; LLM wording varies | Interview red-flag questions; rule floor; emergency keyword pre-check; CI benchmark | Medium: rules only cover listed red flags |
| H2 | Crisis not recognised | Unusual phrasing | Phrase list plus regex; mood question in interview | Medium |
| H3 | Over-triage floods services | Conservative rules | Benchmark tracks over-triage; conditional sentences ignored | Low |
| H4 | Adult advice given for a child | Retrieved text written for adults | Who/age questions; child-specific questions; dose filter | Medium: non-dose adult advice may remain |
| H5 | User reads advice as a diagnosis | Condition names in generated text | Prompt forbids naming conditions; UI says "not a diagnosis"; demo copy is signposting-only | Medium: LLM may still name conditions |
| H6 | Negated symptom triggers a rule ("no chest pain") | Keyword matching | Negation window check | Low: unusual negation forms may slip |
| H7 | Negated symptom suppresses a rule | Over-broad negation | Negation only looks 40 characters back | Low |
| H8 | Stale advice when symptoms change | One-off check | Follow-up check-in emails and re-triage | Medium: depends on user responding |
| H9 | Out-of-area numbers | UK numbers only | Urgent help sheet notes 112 for Europe | Medium |
| H10 | Service unavailable during emergency | API outage | Urgent help sheet works offline (tel: links); sidebar shows 999/111 | Low |

## Known limitations

* The vignettes were written by the developer from NHS condition pages and **have not
  been clinically validated or independently reviewed**. A good score shows the rules
  do what they were designed to do, not that they are clinically complete.
* The rules were tuned while the benchmark was being written, so the score may
  overstate performance on unseen cases. A held-out set written by a clinician is the
  next step.
* An Oxford randomised study (2025, n=1,298) found that good benchmark scores don't
  carry over to real users. Usability testing with real people is required before any
  claim of safety.
