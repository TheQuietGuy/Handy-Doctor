# Is HandyDoctor a medical device?

*Not legal advice. A summary of MHRA guidance to inform design decisions.*

The MHRA's guidance on stand-alone software (including its symptom-checker appendix)
says:

* Software that **only signposts** ("see your GP", "go to A&E") is **unlikely** to be a
  medical device.
* Software that lists **conditions ranked by likelihood**, flags seriousness, or
  recommends **treatment** is likely to be one. Symptom checkers are usually Class I,
  or Class IIa if they allow a direct or indicative diagnosis.
* Words such as *triage*, *self-assessment* and *differential diagnosis* in marketing
  point towards device status.

## Design choices made because of this

* Answers end with **where to get help** (five care levels), not a condition name.
* The system prompt forbids naming a likely condition, and the homepage demo was
  rewritten to signpost only.
* Medicine advice is limited to over-the-counter self-care; adult doses are removed
  for children; no prescription recommendations.
* The red-flag rules and the 999/111 numbers are fixed, not generated.

## Before real-world launch

1. Get a formal regulatory opinion. Because HandyDoctor flags seriousness, MHRA
   registration as Class I software may well be required.
2. Appoint a Clinical Safety Officer and complete DCB0129 (see `CLINICAL_SAFETY.md`).
3. Complete a DPIA under UK GDPR (health data is special category data).
4. Have the vignette set reviewed and extended by clinicians, and run usability
   testing with real users.

Source: MHRA, *Medical device stand-alone software including apps* and its symptom
checker appendix (gov.uk).
