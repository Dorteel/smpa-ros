# DOLCE: DOLCEbasic 3.5

Unmodified RDF/XML OWL module published by the ISTC-CNR Laboratory for
Applied Ontology, engineered by Laure Vieu and Daniele Porello with the
contributors named in the ontology. This is DOLCEbasic, not DUL or the complete
first-order DOLCE axiomatization. The original `OWL/DOLCEbasic.owl` path is kept.

- Distribution: https://github.com/appliedontolab/DOLCE
- Maintainer documentation: https://www.loa.istc.cnr.it/index.php/dolce/
- License: Creative Commons Attribution 4.0 International (CC BY 4.0).
  The upstream `LICENSE` and embedded attribution are preserved.
- Limitations: upstream labels 3.5 beta; DOLCEbasic omits n-ary relations
  (n > 2). Other optional modules, use cases and historical versions are not
  included. OWL does not express the complete first-order DOLCE theory.

`config.yaml` pins the source commit and byte checksum; `main` is relative to
this directory. Local RDF/XML parsing succeeds and declares no `owl:imports`;
there are no unresolved imports. Parsing is not OWL profile, consistency or
reasoning validation. No loading or runtime integration is implemented here.
